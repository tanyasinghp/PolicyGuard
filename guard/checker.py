"""Deterministic policy checker: the trusted core of PolicyGuard.

Given a compiled :class:`~policy.compile_graph.PolicyGraph` and a
:class:`~guard.models.MappedContext`, the checker produces a
:class:`~guard.models.GuardDecision` with a complete derivation trace.

The checker is a pure function of (graph, context): it performs no I/O,
calls no model, and holds no mutable state. Everything fuzzy about the
world is upstream in the mapper; everything here is auditable.

Decision semantics (in order):
    1. Applicability: a rule applies iff it governs the action type, is
       in force on the context date, matches the jurisdiction, and every
       rule condition equals the corresponding context attribute. A
       condition key absent from the context is a non-match (the rule
       does not apply) -- mapper omissions therefore surface as potential
       under-blocking, which the evaluation measures explicitly.
    2. Defeat: explicit edges always defeat (``excepts_from`` reported
       as "exception", ``overrides`` as "explicit-override"). Numeric
       priority defeats only between *conflicting* rules (prohibition
       vs. permission); ties resolve for the prohibition
       ("conservative-tie-break"). Priority never beats an explicit edge.
    3. Verdict, over surviving rules:
       a. any prohibition            -> BLOCK
       b. any unmet requires_prior   -> BLOCK (obligation unmet)
       c. any requires_approval      -> ESCALATE
       d. any permission/obligation  -> ALLOW
       e. nothing governs            -> the configured fail mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from guard.models import (
    DeonticStatus,
    Derivation,
    GuardDecision,
    MappedContext,
    ProposedAction,
    ResolutionStep,
    Rule,
    Verdict,
)
from policy.compile_graph import PolicyGraph

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Resolution:
    """Internal result of defeasible conflict resolution."""

    surviving: tuple[Rule, ...]
    steps: tuple[ResolutionStep, ...]


class Checker:
    """Deterministic constraint checker over a compiled policy graph.

    Args:
        graph: Compiled policy graph to enforce.
        fail_mode: Verdict when no rule governs the proposed action.
            Defaults to ``Verdict.BLOCK`` (fail-closed): an agent may
            only take actions the policy explicitly contemplates. Set to
            ``Verdict.ALLOW`` (fail-open) to permit unregulated actions;
            the difference is a measured experimental condition, not an
            implementation detail.
    """

    def __init__(self, graph: PolicyGraph, fail_mode: Verdict = Verdict.BLOCK) -> None:
        if fail_mode not in (Verdict.BLOCK, Verdict.ALLOW):
            raise ValueError("fail_mode must be BLOCK or ALLOW")
        self._graph = graph
        self._fail_mode = fail_mode

    def check(self, action: ProposedAction, context: MappedContext) -> GuardDecision:
        """Evaluate a proposed action against the policy graph.

        Args:
            action: The raw tool call the agent proposed.
            context: Typed facts produced by the mapper for this action.

        Returns:
            A GuardDecision carrying the verdict, the governing rule (if
            any), and a machine-readable derivation trace.
        """
        applicable = self._applicable_rules(context)
        if not applicable:
            logger.debug(
                "no applicable rules for %s; fail mode %s",
                context.action_type,
                self._fail_mode,
            )
            return GuardDecision(
                verdict=self._fail_mode,
                derivation=Derivation(),
                mapped_context=context,
                proposed_action=action,
            )

        resolution = self._resolve(applicable)
        unmet = self._unmet_obligations(resolution.surviving, context)
        verdict, governing = self._verdict(resolution.surviving, unmet)
        derivation = Derivation(
            applicable_rules=tuple(rule.id for rule in applicable),
            resolution=resolution.steps,
            unmet_obligations=unmet,
            provenance=tuple(
                dict.fromkeys(rule.derived_from for rule in resolution.surviving)
            ),
        )
        return GuardDecision(
            verdict=verdict,
            governing_rule=governing.id if governing else None,
            derivation=derivation,
            mapped_context=context,
            proposed_action=action,
        )

    def _applicable_rules(self, context: MappedContext) -> list[Rule]:
        """Rules in scope for this context, sorted by descending priority."""
        candidates = self._graph.candidate_rules(
            context.action_type, context.timestamp, context.jurisdiction
        )
        applicable = [
            rule for rule in candidates if self._conditions_match(rule, context)
        ]
        return sorted(applicable, key=lambda rule: rule.priority, reverse=True)

    @staticmethod
    def _conditions_match(rule: Rule, context: MappedContext) -> bool:
        """True iff every rule condition equals the context attribute."""
        return all(
            key in context.attributes and context.attributes[key] == expected
            for key, expected in rule.conditions.items()
        )

    def _resolve(self, applicable: list[Rule]) -> _Resolution:
        """Apply defeat semantics; return surviving rules and the trace."""
        defeated: dict[str, ResolutionStep] = {}
        for winner in applicable:
            for loser in applicable:
                if winner.id == loser.id or loser.id in defeated:
                    continue
                ground = self._defeat_ground(winner, loser)
                if ground is not None:
                    defeated[loser.id] = ResolutionStep(
                        winner=winner.id, loser=loser.id, because=ground
                    )
        surviving = tuple(rule for rule in applicable if rule.id not in defeated)
        return _Resolution(surviving=surviving, steps=tuple(defeated.values()))

    def _defeat_ground(self, winner: Rule, loser: Rule) -> str | None:
        """Return the ground on which ``winner`` defeats ``loser``, if any."""
        explicit = self._graph.defeats(winner.id, loser.id)
        if explicit is not None:
            return explicit
        if not self._conflicting(winner, loser):
            return None
        if winner.priority > loser.priority:
            return "priority"
        if (
            winner.priority == loser.priority
            and winner.deontic is DeonticStatus.PROHIBITION
            and loser.deontic is not DeonticStatus.PROHIBITION
        ):
            return "conservative-tie-break"
        return None

    @staticmethod
    def _conflicting(a: Rule, b: Rule) -> bool:
        """Prohibition vs. permission on the same action genuinely conflict."""
        return {a.deontic, b.deontic} == {
            DeonticStatus.PROHIBITION,
            DeonticStatus.PERMISSION,
        }

    @staticmethod
    def _unmet_obligations(
        surviving: tuple[Rule, ...], context: MappedContext
    ) -> tuple[str, ...]:
        """Sequencing obligations whose prerequisite actions have not occurred."""
        done = set(context.prior_actions)
        unmet: list[str] = []
        for rule in surviving:
            for prior in rule.requires_prior:
                if prior not in done:
                    unmet.append(f"{prior} must precede {rule.action} ({rule.id})")
        return tuple(unmet)

    def _verdict(
        self, surviving: tuple[Rule, ...], unmet: tuple[str, ...]
    ) -> tuple[Verdict, Rule | None]:
        """Apply the fixed verdict ordering over surviving rules."""
        prohibitions = [r for r in surviving if r.deontic is DeonticStatus.PROHIBITION]
        if prohibitions:
            return Verdict.BLOCK, prohibitions[0]
        if unmet:
            governing = next((r for r in surviving if r.requires_prior), None)
            return Verdict.BLOCK, governing
        approvals = [r for r in surviving if r.requires_approval is not None]
        if approvals:
            return Verdict.ESCALATE, approvals[0]
        if surviving:
            permissions = [
                r for r in surviving if r.deontic is DeonticStatus.PERMISSION
            ]
            return Verdict.ALLOW, permissions[0] if permissions else surviving[0]
        return self._fail_mode, None