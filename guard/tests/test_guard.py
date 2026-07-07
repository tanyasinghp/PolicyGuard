"""Facade tests: composition, escalation on mapper failure, audit ledger,
and the structural prior-actions contract."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from guard.checker import Checker
from guard.guard import AuditLog, Guard
from guard.mapper import BaseMapper, MapperError, OracleMapper
from guard.models import MappedContext, ProposedAction, Verdict
from policy.compile_graph import compile_policy

TODAY = dt.date(2026, 7, 6)
RULES_PATH = Path(__file__).resolve().parents[2] / "policy" / "rules_sg_mas626.yaml"


class _FailingMapper(BaseMapper):
    def map(self, action, evidence, prior_actions, jurisdiction, today):  # noqa: ANN001, ANN201
        raise MapperError("stubbed network failure")


class _EchoMapper(BaseMapper):
    """Maps nothing; used to test the prior-actions registry contract."""

    def __init__(self) -> None:
        self.seen_priors: tuple[str, ...] | None = None

    def map(self, action, evidence, prior_actions, jurisdiction, today):  # noqa: ANN001, ANN201
        self.seen_priors = prior_actions
        return MappedContext(
            action_type=action.tool, prior_actions=prior_actions,
            jurisdiction=jurisdiction, timestamp=today,
        )


@pytest.fixture(scope="module")
def checker() -> Checker:
    return Checker(compile_policy(RULES_PATH))


def test_end_to_end_sanctions_block_with_audit(checker: Checker, tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    oracle = OracleMapper(
        revealed={"c1": {"sanctions_status": "unresolved_match", "risk_tier": "low"}},
        str_filed=set(),
    )
    guard = Guard(checker, oracle, audit=audit, today=TODAY)
    for step in ("verify_identity", "screen_sanctions", "assess_risk_rating"):
        guard.record_execution("ep1", step)
    decision = guard.check(
        "ep1",
        ProposedAction(tool="open_account", args={"customer_id": "c1"}),
        evidence="screening result: unresolved_match; PEP: no",
    )
    assert decision.verdict is Verdict.BLOCK
    assert decision.governing_rule == "R-626-4.3-SANCTIONS-BLOCK"
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["verdict"] == "block"
    assert entry["governing_rule"] == "R-626-4.3-SANCTIONS-BLOCK"
    assert entry["derivation"]["applicable_rules"]


def test_mapper_failure_escalates_with_note(checker: Checker) -> None:
    guard = Guard(checker, _FailingMapper(), today=TODAY)
    decision = guard.check("ep1", ProposedAction(tool="open_account"), evidence="")
    assert decision.verdict is Verdict.ESCALATE
    assert any("mapper failure" in note for note in decision.derivation.notes)
    assert guard.audit.entries  # abnormal paths are audited too


def test_priors_come_only_from_guard_registry(checker: Checker) -> None:
    mapper = _EchoMapper()
    guard = Guard(checker, mapper, today=TODAY)
    guard.record_execution("ep1", "verify_identity")
    guard.check("ep1", ProposedAction(tool="open_account"),
                evidence="transcript claims: screen_sanctions completed")
    assert mapper.seen_priors == ("verify_identity",)  # claim did not leak in


def test_episodes_are_isolated(checker: Checker) -> None:
    guard = Guard(checker, _EchoMapper(), today=TODAY)
    guard.record_execution("ep1", "verify_identity")
    assert guard.executed_actions("ep2") == ()