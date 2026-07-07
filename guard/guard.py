"""Guard facade: mapper -> checker -> audit ledger behind one call.

This is the component the agent runtime talks to. It owns two pieces of
state, both safety-relevant:

* The **executed-actions registry**. ``record_execution`` is called by
  the runtime only after a tool has actually run; ``check`` reads prior
  actions exclusively from this registry. Transcript claims ("screening
  was already completed") therefore cannot forge sequencing history --
  there is deliberately no parameter through which a caller could
  supply prior actions.
* The **audit ledger**: an append-only JSONL file (plus an in-memory
  mirror for tests and the demo) with one entry per decision, carrying
  the proposed action, mapped context, verdict, and full derivation.

Failure semantics: a :class:`~guard.mapper.MapperError` becomes an
ESCALATE decision with the failure reason recorded in the derivation
notes. The guard never converts uncertainty into ALLOW or BLOCK.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from guard.checker import Checker
from guard.mapper import BaseMapper, MapperError
from guard.models import Derivation, GuardDecision, MappedContext, ProposedAction, Verdict

logger = logging.getLogger(__name__)


class AuditLog:
    """Append-only JSONL decision ledger with an in-memory mirror.

    Args:
        path: JSONL file to append to, or None for in-memory only
            (unit tests). Parent directories are created as needed.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self.entries: list[dict[str, object]] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, episode_id: str, decision: GuardDecision) -> None:
        """Record one decision."""
        entry: dict[str, object] = {
            "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
            "episode_id": episode_id,
            "proposed_action": decision.proposed_action.model_dump(),
            "mapped_context": decision.mapped_context.model_dump(mode="json"),
            "verdict": decision.verdict.value,
            "governing_rule": decision.governing_rule,
            "derivation": decision.derivation.model_dump(),
        }
        self.entries.append(entry)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")


class Guard:
    """Execution guard composing a mapper, a checker, and an audit log.

    Args:
        checker: Deterministic checker over the compiled policy graph.
        mapper: Context mapper (oracle in ablations, LLM in production).
        audit: Audit ledger; defaults to in-memory only.
        jurisdiction: Jurisdiction code decisions are evaluated under.
        today: In-world date for temporal rule validity.
    """

    def __init__(
        self,
        checker: Checker,
        mapper: BaseMapper,
        audit: AuditLog | None = None,
        jurisdiction: str = "SG",
        today: dt.date | None = None,
    ) -> None:
        self._checker = checker
        self._mapper = mapper
        self.audit = audit if audit is not None else AuditLog()
        self._jurisdiction = jurisdiction
        self._today = today or dt.date.today()
        self._executed: dict[str, list[str]] = {}

    def record_execution(self, episode_id: str, action_type: str) -> None:
        """Register that a tool actually executed (runtime calls this
        after execution, never before)."""
        self._executed.setdefault(episode_id, []).append(action_type)

    def executed_actions(self, episode_id: str) -> tuple[str, ...]:
        """Guard-recorded prior actions for an episode."""
        return tuple(self._executed.get(episode_id, []))

    def check(
        self, episode_id: str, action: ProposedAction, evidence: str
    ) -> GuardDecision:
        """Map, check, and audit one proposed action.

        Args:
            episode_id: Episode the action belongs to.
            action: The tool call the agent proposed.
            evidence: Textual evidence available so far (tool results,
                transcript excerpt). Treated as untrusted data.

        Returns:
            The guard's decision, already written to the audit ledger.
        """
        priors = self.executed_actions(episode_id)
        try:
            context = self._mapper.map(
                action=action,
                evidence=evidence,
                prior_actions=priors,
                jurisdiction=self._jurisdiction,
                today=self._today,
            )
            decision = self._checker.check(action, context)
        except MapperError as exc:
            logger.warning("mapper failure -> ESCALATE: %s", exc)
            fallback_context = MappedContext(
                action_type=action.tool,
                prior_actions=priors,
                jurisdiction=self._jurisdiction,
                timestamp=self._today,
                confidence=0.0,
            )
            decision = GuardDecision(
                verdict=Verdict.ESCALATE,
                derivation=Derivation(notes=(f"mapper failure: {exc}",)),
                mapped_context=fallback_context,
                proposed_action=action,
            )
        self.audit.append(episode_id, decision)
        return decision