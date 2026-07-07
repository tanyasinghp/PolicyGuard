"""Core domain models for PolicyGuard.

This module defines the shared type vocabulary for the entire system:
policy rules, proposed agent actions, mapped contexts, guard verdicts,
and derivation traces. Every other module (compiler, checker, mapper,
guard, evaluation harness, API) imports its types from here.

Design decisions of research relevance:

* ``Verdict`` is three-valued (ALLOW / BLOCK / ESCALATE) rather than a
  boolean, because approval-required actions are a distinct human-
  oversight category, not a soft "block".
* ``Derivation`` is a structured object rather than a prose string, so
  that audit reconstructability can be measured, serialized, and
  rendered independently of any UI.
* ``ConditionValue`` deliberately restricts rule-condition values to
  scalars, keeping scope matching decidable. Expressiveness beyond this
  boundary is, by design, the LLM mapper's problem — this is where the
  project's "semantic gap" measurement lives.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

ConditionValue = str | bool | int
"""Scalar values allowed in rule conditions and mapped-context attributes."""


class DeonticStatus(StrEnum):
    """Normative status a rule assigns to an action."""

    OBLIGATION = "obligation"
    PERMISSION = "permission"
    PROHIBITION = "prohibition"


class Verdict(StrEnum):
    """Guard decision for a proposed action."""

    ALLOW = "allow"
    BLOCK = "block"
    ESCALATE = "escalate"


class Rule(BaseModel):
    """A single normative rule compiled from a policy document.

    Attributes:
        id: Stable identifier, e.g. ``R-626-4.2-SANCTIONS-BLOCK``.
        derived_from: Citation of the source provision (provenance).
        deontic: Whether the rule obliges, permits, or prohibits.
        action: The action type this rule governs, e.g. ``open_account``.
        conditions: Scope of applicability. The rule applies only when
            every key here matches the mapped context exactly.
        requires_prior: Action types that must have occurred earlier in
            the episode for the governed action to proceed (sequencing
            obligations such as "screen before opening").
        requires_approval: Role whose sign-off is needed; a matching
            rule with this set yields ESCALATE rather than BLOCK.
        overrides: IDs of rules this rule defeats when both apply.
        excepts_from: IDs of rules this rule carves an exception out of
            (treated as an override with 'exception' rationale).
        priority: Defeasible priority band; higher wins when no explicit
            override edge resolves a conflict.
        valid_from: First date (inclusive) the rule version is in force.
        valid_to: Last date (inclusive), or None if still in force.
        jurisdiction: Jurisdiction code, e.g. ``SG``.
        rationale: One-line human-readable justification, used verbatim
            in derivation traces.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    derived_from: str
    deontic: DeonticStatus
    action: str
    conditions: dict[str, ConditionValue] = Field(default_factory=dict)
    requires_prior: tuple[str, ...] = ()
    requires_approval: str | None = None
    overrides: tuple[str, ...] = ()
    excepts_from: tuple[str, ...] = ()
    priority: int = 0
    valid_from: dt.date
    valid_to: dt.date | None = None
    jurisdiction: str = "SG"
    rationale: str

    @field_validator("id", "action", "jurisdiction")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        """Reject empty identifiers early, at parse time."""
        if not value.strip():
            raise ValueError("identifier fields must be non-empty")
        return value

    def in_force_on(self, day: dt.date) -> bool:
        """Return True if this rule version is valid on ``day``."""
        if day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to


class ProposedAction(BaseModel):
    """A tool call the agent wants to execute, prior to guard review."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class MappedContext(BaseModel):
    """Typed facts the mapper extracted for one proposed action.

    This is the checker's entire view of the world: the checker never
    sees raw conversation text, only these attributes. Mapper errors
    therefore propagate here and nowhere else — which is exactly what
    makes the mapping step ablatable and measurable.

    Attributes:
        action_type: Canonical action node, e.g. ``open_account``.
        attributes: Scalar facts about the situation, keyed to match
            rule condition keys (e.g. ``is_pep``, ``sanctions_status``).
        prior_actions: Action types already executed in this episode.
        jurisdiction: Jurisdiction the episode is evaluated under.
        timestamp: Decision date, used for temporal rule validity.
        confidence: Mapper self-reported confidence in [0, 1]; recorded
            for analysis, never used to change the verdict.
    """

    action_type: str
    attributes: dict[str, ConditionValue] = Field(default_factory=dict)
    prior_actions: tuple[str, ...] = ()
    jurisdiction: str = "SG"
    timestamp: dt.date
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ResolutionStep(BaseModel):
    """One conflict-resolution event inside a derivation."""

    winner: str
    loser: str
    because: str
    """Resolution ground: 'explicit-override', 'exception', or 'priority'."""


class Derivation(BaseModel):
    """Machine-readable audit trace for a single guard decision."""

    applicable_rules: tuple[str, ...] = ()
    resolution: tuple[ResolutionStep, ...] = ()
    unmet_obligations: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    """Abnormal-path annotations, e.g. mapper failure reasons."""

    def to_text(self) -> str:
        """Render a compact human-readable derivation for logs and UI."""
        lines: list[str] = []
        if self.applicable_rules:
            lines.append("applicable: " + ", ".join(self.applicable_rules))
        for step in self.resolution:
            lines.append(f"{step.winner} beats {step.loser} ({step.because})")
        for obligation in self.unmet_obligations:
            lines.append(f"unmet obligation: {obligation}")
        for note in self.notes:
            lines.append(f"note: {note}")
        if self.provenance:
            lines.append("sources: " + "; ".join(self.provenance))
        return "\n".join(lines) if lines else "no applicable rules"


class GuardDecision(BaseModel):
    """The guard's complete answer for one proposed action."""

    verdict: Verdict
    governing_rule: str | None = None
    derivation: Derivation = Field(default_factory=Derivation)
    mapped_context: MappedContext
    proposed_action: ProposedAction