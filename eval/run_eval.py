"""Evaluation harness: run the episode suite across conditions.

Conditions:
    self_policing   -- condition A: no guard; policy in prompt only.
    guarded_oracle  -- condition B (ceiling): guard with the oracle mapper.
    guarded_llm     -- condition C: guard with the LLM mapper (the
                       semantic-gap measurement vs. guarded_oracle).

Writes one raw JSON per (condition, episode) under eval/results/raw/
(reruns skip finished pairs unless --overwrite) and regenerates
eval/results/results.md from everything on disk.

Usage:
    python -m eval.run_eval --conditions self_policing guarded_oracle \
        [--limit 5] [--overwrite]

Cost note: each (condition, episode) is one multi-turn agent run;
guarded_llm adds one small mapper call per tool proposal. Start with
--limit 3 to sanity-check spend.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from pathlib import Path

from agent.prompts import build_system_prompt
from agent.runtime import AgentRuntime, EpisodeResult, build_client
from eval.episode_spec import EpisodeSpec, load_all
from eval.metrics import (
    MARKDOWN_HEADER,
    Adjudicator,
    ConditionSummary,
    EpisodeMetrics,
    task_completed,
)
from guard.checker import Checker
from guard.guard import AuditLog, Guard
from guard.mapper import LLMMapper, OracleMapper
from policy.compile_graph import PolicyGraph, compile_policy

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "policy" / "rules_sg_mas626.yaml"
EPISODES = ROOT / "eval" / "episodes"
RESULTS = ROOT / "eval" / "results"
CONDITIONS = ("self_policing", "guarded_oracle", "guarded_llm")


def build_guard(condition: str, spec: EpisodeSpec, graph: PolicyGraph,
                world_revealed: dict, world_str_filed: set,
                audit_dir: Path) -> Guard | None:
    """Construct the per-episode guard for a condition (None for A)."""
    if condition == "self_policing":
        return None
    if condition == "guarded_oracle":
        mapper = OracleMapper(world_revealed, world_str_filed)
    elif condition == "guarded_llm":
        mapper = LLMMapper(graph, client=build_client())
    else:
        raise ValueError(f"unknown condition: {condition}")
    return Guard(
        Checker(graph), mapper,
        audit=AuditLog(audit_dir / f"{condition}.jsonl"),
        jurisdiction=spec.jurisdiction, today=spec.today,
    )


def run_pair(condition: str, spec: EpisodeSpec, graph: PolicyGraph,
             runtime: AgentRuntime, audit_dir: Path) -> tuple[EpisodeResult, bool]:
    """Run one (condition, episode) pair; returns result and account state."""
    world = spec.build_world()
    guard = build_guard(condition, spec, graph, world.revealed,
                        world.str_filed, audit_dir)
    result = runtime.run_episode(
        episode_id=f"{spec.id}-{condition}",
        world=world,
        task=spec.task,
        system_prompt=build_system_prompt(
            graph, guarded=guard is not None, today=spec.today,
            jurisdiction=spec.jurisdiction,
        ),
        guard=guard,
    )
    account_open = any(status == "open" for status in world.accounts.values())
    return result, account_open


def score(spec: EpisodeSpec, condition: str, result: EpisodeResult,
          account_open: bool, adjudicator: Adjudicator) -> EpisodeMetrics:
    """Adjudicate and score one finished run."""
    events = adjudicator.adjudicate(spec, result)
    return EpisodeMetrics(
        episode_id=spec.id,
        condition=condition,
        category=spec.category,
        injection=spec.injection,
        events=events,
        task_completed=task_completed(spec, result, events, account_open),
        aborted=result.aborted,
        turns=result.turns,
    )


def write_report(summaries: list[ConditionSummary], path: Path) -> None:
    """Render results.md from per-condition summaries."""
    lines = [
        "# PolicyGuard evaluation results",
        "",
        f"Generated {dt.datetime.now(dt.UTC).isoformat(timespec='seconds')} | "
        f"agent model: {os.environ.get('AGENT_MODEL', 'claude-sonnet-4-6')} | "
        f"n per condition varies with --limit; single run, no significance "
        f"claims (preliminary).",
        "",
        MARKDOWN_HEADER,
        *[s.to_markdown_row() for s in summaries],
        "",
        "Notes: guarded_oracle scores zero violations by construction (the "
        "enforcement ceiling; see eval/episodes/labels.md). The informative "
        "comparisons are self_policing vs. the ceiling, and guarded_llm vs. "
        "guarded_oracle (the semantic gap).",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conditions", nargs="+", default=list(CONDITIONS),
                        choices=CONDITIONS)
    parser.add_argument("--limit", type=int, default=None,
                        help="run only the first N episodes")
    parser.add_argument("--overwrite", action="store_true",
                        help="rerun pairs that already have raw results")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    graph = compile_policy(RULES)
    adjudicator = Adjudicator(graph)
    specs = load_all(EPISODES)[: args.limit]
    raw_dir = RESULTS / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    runtime = AgentRuntime(
        client=build_client(),
        model=os.environ.get("AGENT_MODEL", "claude-sonnet-4-6"),
    )

    summaries: list[ConditionSummary] = []
    for condition in args.conditions:
        summary = ConditionSummary(condition=condition)
        for spec in specs:
            raw_path = raw_dir / f"{spec.id}-{condition}.json"
            if raw_path.exists() and not args.overwrite:
                metrics = EpisodeMetrics.model_validate_json(
                    raw_path.read_text(encoding="utf-8"))
            else:
                print(f"running {spec.id} [{condition}] ...", flush=True)
                result, account_open = run_pair(condition, spec, graph,
                                                runtime, RESULTS / "audit")
                metrics = score(spec, condition, result, account_open,
                                adjudicator)
                raw_path.write_text(metrics.model_dump_json(indent=2),
                                    encoding="utf-8")
            summary.episodes.append(metrics)
        summaries.append(summary)
        print(f"{condition}: violation-episode rate "
              f"{summary.violation_episode_rate():.1%}, completion "
              f"{summary.completion_rate():.1%}")

    write_report(summaries, RESULTS / "results.md")
    print(f"report written to {RESULTS / 'results.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())