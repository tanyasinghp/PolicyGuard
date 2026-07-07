"""Compile YAML policy rules into a validated, queryable policy graph.

The YAML rule files under ``policy/`` are the single source of truth.
This module parses them into :class:`guard.models.Rule` objects,
validates referential integrity (override/exception targets exist, IDs
are unique, defeat edges are acyclic), and assembles a
:class:`PolicyGraph` — a thin domain wrapper around a
``networkx.MultiDiGraph`` that the checker queries at decision time.

Graph shape:
    * Rule nodes (``kind="rule"``) carry the full Rule object.
    * Action nodes (``kind="action"``) exist for every governed action.
    * Edges: ``governs`` (rule -> action), ``overrides`` and
      ``excepts_from`` (rule -> rule), ``requires_prior``
      (rule -> action).

Run directly for a compile check and summary::

    python -m policy.compile_graph policy/rules_sg_mas626.yaml
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

import networkx as nx
import yaml
from pydantic import ValidationError

from guard.models import Rule

logger = logging.getLogger(__name__)


class PolicyCompileError(Exception):
    """Raised when a rule file is syntactically valid but semantically broken."""


class PolicyGraph:
    """Queryable policy graph over a compiled rule set.

    The checker's only entry points are :meth:`candidate_rules` (scope
    pre-filtering by action, date, and jurisdiction) and
    :meth:`defeats` (explicit defeat edges). Condition matching and
    priority resolution are checker concerns, keeping graph structure
    and decision logic separated.
    """

    def __init__(self, rules: list[Rule]) -> None:
        """Build and validate the graph from parsed rules.

        Args:
            rules: Parsed rule objects, typically from :func:`load_rules`.

        Raises:
            PolicyCompileError: On duplicate IDs, dangling defeat-edge
                targets, or cyclic defeat relations.
        """
        self._rules: dict[str, Rule] = {}
        self.graph = nx.MultiDiGraph()
        for rule in rules:
            if rule.id in self._rules:
                raise PolicyCompileError(f"duplicate rule id: {rule.id}")
            self._rules[rule.id] = rule
            self.graph.add_node(rule.id, kind="rule", rule=rule)
            self.graph.add_node(rule.action, kind="action")
            self.graph.add_edge(rule.id, rule.action, key="governs")
            for prior in rule.requires_prior:
                self.graph.add_node(prior, kind="action")
                self.graph.add_edge(rule.id, prior, key="requires_prior")
        self._add_defeat_edges()
        self._check_defeat_acyclicity()
        logger.info(
            "compiled policy graph: %d rules, %d actions",
            len(self._rules),
            sum(1 for _, d in self.graph.nodes(data=True) if d.get("kind") == "action"),
        )

    def _add_defeat_edges(self) -> None:
        """Add overrides/excepts_from edges, validating targets exist."""
        for rule in self._rules.values():
            for kind, targets in (
                ("overrides", rule.overrides),
                ("excepts_from", rule.excepts_from),
            ):
                for target in targets:
                    if target not in self._rules:
                        raise PolicyCompileError(
                            f"rule {rule.id}: {kind} target {target!r} does not exist"
                        )
                    self.graph.add_edge(rule.id, target, key=kind)

    def _check_defeat_acyclicity(self) -> None:
        """Reject rule sets whose defeat relation contains a cycle."""
        defeat_edges = [
            (u, v)
            for u, v, k in self.graph.edges(keys=True)
            if k in ("overrides", "excepts_from")
        ]
        defeat_graph = nx.DiGraph(defeat_edges)
        try:
            cycle = nx.find_cycle(defeat_graph)
        except nx.NetworkXNoCycle:
            return
        raise PolicyCompileError(f"cyclic defeat relation: {cycle}")

    @property
    def rules(self) -> dict[str, Rule]:
        """All rules keyed by ID (read-only view by convention)."""
        return self._rules

    def get_rule(self, rule_id: str) -> Rule:
        """Return the rule with ``rule_id``.

        Raises:
            KeyError: If no such rule exists.
        """
        return self._rules[rule_id]

    def candidate_rules(
        self, action_type: str, on: dt.date, jurisdiction: str
    ) -> list[Rule]:
        """Return rules governing ``action_type``, in force on ``on``.

        This is scope *pre-filtering* only — condition matching against
        the mapped context happens in the checker.
        """
        return [
            rule
            for rule in self._rules.values()
            if rule.action == action_type
            and rule.jurisdiction == jurisdiction
            and rule.in_force_on(on)
        ]

    def defeats(self, winner_id: str, loser_id: str) -> str | None:
        """Return the defeat ground if ``winner_id`` explicitly defeats ``loser_id``.

        Returns:
            ``"explicit-override"``, ``"exception"``, or None.
        """
        if not self.graph.has_edge(winner_id, loser_id):
            return None
        keys = self.graph[winner_id][loser_id].keys()
        if "excepts_from" in keys:
            return "exception"
        if "overrides" in keys:
            return "explicit-override"
        return None

    def export_graphml(self, path: Path) -> None:
        """Write a GraphML export (rule objects flattened) for visualization."""
        export = nx.MultiDiGraph()
        for node, data in self.graph.nodes(data=True):
            attrs = {"kind": data.get("kind", "")}
            rule = data.get("rule")
            if rule is not None:
                attrs.update(
                    deontic=rule.deontic.value,
                    priority=rule.priority,
                    rationale=rule.rationale,
                )
            export.add_node(node, **attrs)
        for u, v, k in self.graph.edges(keys=True):
            export.add_edge(u, v, key=k, relation=k)
        nx.write_graphml(export, path)


def load_rules(path: Path) -> list[Rule]:
    """Parse a YAML rule file into validated Rule objects.

    Args:
        path: Path to a YAML file containing a list of rule mappings.

    Raises:
        PolicyCompileError: On YAML syntax errors, non-list top level,
            or Pydantic validation failures (with rule index context).
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyCompileError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, list):
        raise PolicyCompileError(f"{path}: expected a top-level list of rules")
    rules: list[Rule] = []
    for index, item in enumerate(raw):
        try:
            rules.append(Rule.model_validate(item))
        except ValidationError as exc:
            rule_id = item.get("id", f"<index {index}>") if isinstance(item, dict) else index
            raise PolicyCompileError(f"{path}: rule {rule_id}: {exc}") from exc
    return rules


def compile_policy(path: Path) -> PolicyGraph:
    """Load, validate, and compile a rule file into a PolicyGraph."""
    return PolicyGraph(load_rules(path))


def _main(argv: list[str]) -> int:
    """CLI entry point: compile a rule file and print a summary."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rule_path = Path(argv[1]) if len(argv) > 1 else Path("policy/rules_sg_mas626.yaml")
    graph = compile_policy(rule_path)
    today = dt.date.today()
    print(f"compiled {len(graph.rules)} rules from {rule_path}")
    actions = sorted(
        n for n, d in graph.graph.nodes(data=True) if d.get("kind") == "action"
    )
    for action in actions:
        in_force = graph.candidate_rules(action, today, "SG")
        print(f"  {action}: {len(in_force)} rule(s) in force today")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))