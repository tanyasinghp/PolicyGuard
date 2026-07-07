"""PolicyGuard demo dashboard (Streamlit).

Reads ONLY from disk -- eval/results/raw/*.json (adjudicated metrics),
eval/results/audit/*.jsonl (guard derivations), and eval/episodes/
(specs) -- so the demo never depends on live API calls during a
recording. Degrades gracefully when results do not exist yet.

Run:  streamlit run demo/demo.py     (requires the [demo] extra)
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.episode_spec import EpisodeSpec, load_all  # noqa: E402
from eval.metrics import ConditionSummary, EpisodeMetrics, EventClass  # noqa: E402
from policy.compile_graph import PolicyGraph, compile_policy  # noqa: E402

RAW = ROOT / "eval" / "results" / "raw"
AUDIT = ROOT / "eval" / "results" / "audit"
RULES = ROOT / "policy" / "rules_sg_mas626.yaml"

CONDITION_LABELS = {
    "self_policing": "A — self-policing",
    "guarded_oracle": "B — guard (oracle mapper)",
    "guarded_llm": "C — guard (LLM mapper)",
}
VERDICT_ICONS = {"unguarded": "▶", "allow": "✅", "block": "⛔", "escalate": "🧑‍⚖️"}
CLASS_BADGES = {
    EventClass.VIOLATION: ("VIOLATION", "red"),
    EventClass.UNAPPROVED: ("UNAPPROVED EXEC", "orange"),
    EventClass.OVER_REFUSAL: ("OVER-REFUSAL", "orange"),
    EventClass.OVER_ESCALATION: ("OVER-ESCALATION", "orange"),
    EventClass.CORRECT_INTERVENTION: ("correct intervention", "green"),
    EventClass.OK: ("ok", "gray"),
}


@st.cache_data
def load_metrics() -> dict[str, dict[str, EpisodeMetrics]]:
    """condition -> episode_id -> metrics, from raw result files."""
    table: dict[str, dict[str, EpisodeMetrics]] = defaultdict(dict)
    for path in sorted(RAW.glob("*.json")) if RAW.exists() else []:
        metrics = EpisodeMetrics.model_validate_json(path.read_text())
        table[metrics.condition][metrics.episode_id] = metrics
    return dict(table)


@st.cache_data
def load_specs() -> dict[str, EpisodeSpec]:
    episodes_dir = ROOT / "eval" / "episodes"
    if not episodes_dir.exists():
        return {}
    return {spec.id: spec for spec in load_all(episodes_dir)}


@st.cache_data
def load_audit() -> dict[str, list[dict]]:
    """'{episode_id}-{condition}' -> decision entries."""
    entries: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(AUDIT.glob("*.jsonl")) if AUDIT.exists() else []:
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            entries[entry["episode_id"]].append(entry)
    return dict(entries)


@st.cache_resource
def load_graph() -> PolicyGraph:
    return compile_policy(RULES)


def render_condition_column(metrics: EpisodeMetrics | None, label: str) -> None:
    st.subheader(label)
    if metrics is None:
        st.info("No result for this condition yet.")
        return
    violated = metrics.violated
    st.markdown(
        f"**Outcome:** "
        f"{':red[Violation]' if violated else ':green[No violation]'} · "
        f"task {'✅ completed' if metrics.task_completed else '❌ not completed'}"
        f"{' · aborted' if metrics.aborted else ''}"
    )
    for event in metrics.events:
        badge, color = CLASS_BADGES[event.classification]
        icon = VERDICT_ICONS.get(event.runtime_verdict, "?")
        rule = f" · `{event.governing_rule}`" if event.governing_rule else ""
        st.markdown(
            f"{icon} `{event.tool}` — {event.runtime_verdict} "
            f"→ :{color}[{badge}]{rule}"
        )


def render_derivation_panel(episode_id: str, condition: str,
                            audit: dict[str, list[dict]]) -> None:
    entries = audit.get(f"{episode_id}-{condition}", [])
    if not entries:
        st.caption("No audit entries for this run (unguarded, or not run yet).")
        return
    interventions = [e for e in entries if e["verdict"] != "allow"]
    pick = st.selectbox(
        "Guard decision to inspect",
        options=list(range(len(entries))),
        index=entries.index(interventions[0]) if interventions else 0,
        format_func=lambda i: (
            f"{i + 1}. {entries[i]['proposed_action']['tool']} "
            f"→ {entries[i]['verdict'].upper()}"
        ),
    )
    entry = entries[pick]
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Verdict:** `{entry['verdict'].upper()}` · "
                    f"**Governing rule:** `{entry['governing_rule']}`")
        st.json(entry["derivation"])
    with right:
        st.markdown("**Mapped context (what the checker saw):**")
        st.json(entry["mapped_context"])


def render_metrics_row(summaries: list[ConditionSummary]) -> None:
    columns = st.columns(max(len(summaries), 1) * 2)
    i = 0
    for summary in summaries:
        columns[i].metric(f"{summary.condition} violation-episode rate",
                          f"{summary.violation_episode_rate():.0%}")
        columns[i + 1].metric(
            f"{summary.condition} over-refusals / completion",
            f"{summary.total(EventClass.OVER_REFUSAL)} / "
            f"{summary.completion_rate():.0%}",
        )
        i += 2


def policy_graph_dot(graph: PolicyGraph, action_filter: str | None) -> str:
    """Render the rule graph as DOT (deontic color, defeat edges bold)."""
    colors = {"obligation": "#4477AA", "permission": "#44AA77",
              "prohibition": "#CC4444"}
    lines = ["digraph policy {", '  rankdir=LR; node [style=filled, '
             'fontcolor=white, fontsize=10];']
    rules = [r for r in graph.rules.values()
             if action_filter in (None, r.action)]
    ids = {r.id for r in rules}
    for rule in rules:
        lines.append(
            f'  "{rule.id}" [fillcolor="{colors[rule.deontic.value]}"];'
        )
        lines.append(f'  "{rule.action}" [shape=box, fillcolor="#666666"];')
        lines.append(f'  "{rule.id}" -> "{rule.action}" '
                     f'[color="#BBBBBB", arrowsize=0.6];')
    for rule in rules:
        for targets, style in ((rule.overrides, "overrides"),
                               (rule.excepts_from, "exception")):
            for target in targets:
                if target in ids:
                    lines.append(
                        f'  "{rule.id}" -> "{target}" [color="#CC4444", '
                        f'penwidth=2, label="{style}", fontsize=9];'
                    )
    lines.append("}")
    return "\n".join(lines)


st.set_page_config(page_title="PolicyGuard", layout="wide", page_icon="🛡️")
st.title("🛡️ PolicyGuard — policy-graph execution guard for LLM agents")

metrics_by_condition = load_metrics()
specs = load_specs()
audit_entries = load_audit()
graph = load_graph()

results_tab, graph_tab = st.tabs(["Episode results", "Policy graph"])

with results_tab:
    if not metrics_by_condition:
        st.warning(
            "No evaluation results found. Run e.g.\n\n"
            "```\npython -m eval.run_eval --conditions self_policing "
            "guarded_oracle --limit 3\n```\n\nthen refresh this page."
        )
    else:
        summaries = [
            ConditionSummary(condition=c, episodes=list(m.values()))
            for c, m in metrics_by_condition.items()
        ]
        render_metrics_row(summaries)
        st.divider()
        episode_ids = sorted(
            {eid for m in metrics_by_condition.values() for eid in m}
        )
        chosen = st.selectbox(
            "Episode", episode_ids,
            format_func=lambda eid: (
                f"{eid} — {specs[eid].description}" if eid in specs else eid
            ),
        )
        if chosen in specs and specs[chosen].injection:
            payload = specs[chosen].customers[0].documents[-1]
            st.error(f"**Adversarial content in customer documents:** "
                     f"“{payload}”")
        columns = st.columns(len(metrics_by_condition))
        for column, (condition, table) in zip(
            columns, metrics_by_condition.items(), strict=False
        ):
            with column:
                render_condition_column(
                    table.get(chosen),
                    CONDITION_LABELS.get(condition, condition),
                )
        st.divider()
        st.subheader("Verdict derivation (audit trail)")
        guarded_conditions = [c for c in metrics_by_condition
                              if c != "self_policing"]
        if guarded_conditions:
            condition = st.radio("Condition", guarded_conditions,
                                 horizontal=True,
                                 format_func=lambda c: CONDITION_LABELS.get(c, c))
            render_derivation_panel(chosen, condition, audit_entries)

with graph_tab:
    st.markdown(
        f"**{len(graph.rules)} rules** compiled from "
        f"`policy/rules_sg_mas626.yaml`. Colors: "
        f":blue[obligation] · :green[permission] · :red[prohibition]. "
        f"Bold red edges are defeat relations (override / exception)."
    )
    action = st.selectbox(
        "Filter by governed action",
        [None] + sorted({r.action for r in graph.rules.values()}),
        format_func=lambda a: a or "all actions",
    )
    st.graphviz_chart(policy_graph_dot(graph, action), use_container_width=True)