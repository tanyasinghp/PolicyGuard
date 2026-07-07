"""Thin FastAPI service over the PolicyGuard core.

Endpoints:
    GET  /health                     liveness + rule count
    POST /v1/check                   rule on one proposed action
    POST /v1/record_execution        register an executed action
    GET  /v1/audit/{episode_id}      decision ledger for an episode
    GET  /v1/graph/rule/{rule_id}    rule payload + defeat neighborhood

Boundary design: /v1/check accepts EITHER pre-mapped ``attributes``
(deterministic mode: the service is a pure trusted checker) OR raw
``evidence`` (LLM-mapper mode, available only when ANTHROPIC_API_KEY is
configured; otherwise a clear 400). Prior actions are held server-side
and mutated only via /v1/record_execution -- a client cannot assert
priors inside a check request, preserving the guard-recorded-priors
contract across the network boundary.

Run: uvicorn guard.api:app --reload   (requires the [api] extra)
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from guard.checker import Checker
from guard.guard import AuditLog, Guard
from guard.mapper import BaseMapper, LLMMapper, MapperError
from guard.models import ConditionValue, MappedContext, ProposedAction
from policy.compile_graph import PolicyGraph, compile_policy

RULES = Path(__file__).resolve().parents[1] / "policy" / "rules_sg_mas626.yaml"


class CheckRequest(BaseModel):
    """One proposed action to rule on."""

    episode_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    attributes: dict[str, ConditionValue] | None = None
    evidence: str | None = None
    jurisdiction: str = "SG"
    timestamp: dt.date | None = None


class CheckResponse(BaseModel):
    """The guard's ruling, serialized for clients."""

    verdict: str
    governing_rule: str | None
    derivation: dict[str, Any]
    derivation_text: str
    mapper_confidence: float


class RecordExecutionRequest(BaseModel):
    """Report that a tool actually executed."""

    episode_id: str
    tool: str


class _State:
    """Process-wide service state (built at startup)."""

    graph: PolicyGraph
    checker: Checker
    guard_llm: Guard | None
    audit: AuditLog
    executed: dict[str, list[str]]


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Compile the policy graph once at startup."""
    state.graph = compile_policy(RULES)
    state.checker = Checker(state.graph)
    state.audit = AuditLog()
    state.executed = {}
    state.guard_llm = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        from agent.runtime import build_client

        mapper: BaseMapper = LLMMapper(state.graph, client=build_client())
        state.guard_llm = Guard(state.checker, mapper, audit=state.audit)
    yield


app = FastAPI(title="PolicyGuard", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe with basic policy stats."""
    return {"status": "ok", "rules": len(state.graph.rules),
            "llm_mapper": state.guard_llm is not None}


@app.post("/v1/record_execution")
async def record_execution(req: RecordExecutionRequest) -> dict[str, Any]:
    """Register an executed action for an episode's prior-action ledger."""
    state.executed.setdefault(req.episode_id, []).append(req.tool)
    if state.guard_llm is not None:
        state.guard_llm.record_execution(req.episode_id, req.tool)
    return {"episode_id": req.episode_id,
            "executed": state.executed[req.episode_id]}


@app.post("/v1/check", response_model=CheckResponse)
async def check(req: CheckRequest) -> CheckResponse:
    """Rule on one proposed action (deterministic or LLM-mapped)."""
    action = ProposedAction(tool=req.tool, args=req.args)
    priors = tuple(state.executed.get(req.episode_id, []))
    if req.attributes is not None:
        context = MappedContext(
            action_type=req.tool, attributes=req.attributes,
            prior_actions=priors, jurisdiction=req.jurisdiction,
            timestamp=req.timestamp or dt.date.today(),
        )
        decision = state.checker.check(action, context)
        state.audit.append(req.episode_id, decision)
    elif req.evidence is not None:
        if state.guard_llm is None:
            raise HTTPException(
                status_code=400,
                detail="evidence-mode requires ANTHROPIC_API_KEY; "
                       "either configure it or supply pre-mapped attributes",
            )
        try:
            decision = state.guard_llm.check(req.episode_id, action,
                                             req.evidence)
        except MapperError as exc:  # defensive; Guard converts internally
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=400,
                            detail="provide either attributes or evidence")
    return CheckResponse(
        verdict=decision.verdict.value,
        governing_rule=decision.governing_rule,
        derivation=decision.derivation.model_dump(),
        derivation_text=decision.derivation.to_text(),
        mapper_confidence=decision.mapped_context.confidence,
    )


@app.get("/v1/audit/{episode_id}")
async def audit(episode_id: str) -> list[dict[str, Any]]:
    """Decision ledger for one episode."""
    return [entry for entry in state.audit.entries
            if entry.get("episode_id") == episode_id]


@app.get("/v1/graph/rule/{rule_id}")
async def rule_neighborhood(rule_id: str) -> dict[str, Any]:
    """One rule plus its defeat neighborhood (for graph visualization)."""
    try:
        rule = state.graph.get_rule(rule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404,
                            detail=f"unknown rule: {rule_id}") from exc
    defeats = [
        {"target": target, "ground": state.graph.defeats(rule_id, target)}
        for target in state.graph.rules
        if state.graph.defeats(rule_id, target)
    ]
    defeated_by = [
        {"source": source, "ground": state.graph.defeats(source, rule_id)}
        for source in state.graph.rules
        if state.graph.defeats(source, rule_id)
    ]
    return {"rule": rule.model_dump(mode="json"), "defeats": defeats,
            "defeated_by": defeated_by}