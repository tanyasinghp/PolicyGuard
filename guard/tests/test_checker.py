"""Unit tests for the deterministic checker.

Two fixture tiers:
    * ``policy_graph`` -- the real compiled MAS-626-derived rule set, so
      tests double as executable documentation of the policy semantics
      and as ground truth for the eval episode labels (item 8).
    * Synthetic in-test rule sets for edge cases the real policy does
      not exercise (priority ties, fail modes).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from guard.checker import Checker
from guard.models import (
    DeonticStatus,
    MappedContext,
    ProposedAction,
    Rule,
    Verdict,
)
from policy.compile_graph import PolicyGraph, compile_policy

TODAY = dt.date(2026, 7, 6)
RULES_PATH = Path(__file__).resolve().parents[2] / "policy" / "rules_sg_mas626.yaml"

ALL_PRIORS = ("verify_identity", "screen_sanctions", "assess_risk_rating")


@pytest.fixture(scope="module")
def policy_graph() -> PolicyGraph:
    """Compile the real rule set once per module."""
    return compile_policy(RULES_PATH)


@pytest.fixture()
def checker(policy_graph: PolicyGraph) -> Checker:
    """Fail-closed checker over the real policy (the default config)."""
    return Checker(policy_graph)


def _ctx(action: str, priors: tuple[str, ...] = (), **attributes: object) -> MappedContext:
    """Shorthand for building a MappedContext for TODAY in SG."""
    return MappedContext(
        action_type=action,
        attributes=attributes,  # type: ignore[arg-type]
        prior_actions=priors,
        jurisdiction="SG",
        timestamp=TODAY,
    )


ACTION = ProposedAction(tool="open_account", args={"customer_id": "c1"})


class TestProhibitions:
    def test_unresolved_sanctions_match_blocks(self, checker: Checker) -> None:
        decision = checker.check(
            ACTION,
            _ctx("open_account", ALL_PRIORS, risk_tier="low",
                 sanctions_status="unresolved_match"),
        )
        assert decision.verdict is Verdict.BLOCK
        assert decision.governing_rule == "R-626-4.3-SANCTIONS-BLOCK"

    def test_tipping_off_blocked_but_routine_email_allowed(self, checker: Checker) -> None:
        blocked = checker.check(
            ProposedAction(tool="send_customer_email"),
            _ctx("send_customer_email", discloses_str=True),
        )
        allowed = checker.check(
            ProposedAction(tool="send_customer_email"),
            _ctx("send_customer_email", discloses_str=False),
        )
        assert blocked.verdict is Verdict.BLOCK
        assert blocked.governing_rule == "R-626-7.2-NO-TIPPING-OFF"
        assert allowed.verdict is Verdict.ALLOW
        assert allowed.governing_rule == "R-OPS-1-DEFAULT-COMMS"


class TestSequencingObligations:
    def test_missing_screening_blocks_with_unmet_obligation(self, checker: Checker) -> None:
        decision = checker.check(
            ACTION,
            _ctx("open_account", ("verify_identity", "assess_risk_rating"),
                 risk_tier="low"),
        )
        assert decision.verdict is Verdict.BLOCK
        assert any(
            "screen_sanctions" in item
            for item in decision.derivation.unmet_obligations
        )

    def test_all_priors_met_low_risk_allows_via_sdd(self, checker: Checker) -> None:
        decision = checker.check(ACTION, _ctx("open_account", ALL_PRIORS, risk_tier="low"))
        assert decision.verdict is Verdict.ALLOW
        assert decision.governing_rule == "R-626-5.3-SDD-LOWRISK"


class TestDefeasibleResolution:
    def test_suspicion_exception_defeats_sdd_permission(self, checker: Checker) -> None:
        decision = checker.check(
            ACTION,
            _ctx("open_account", ALL_PRIORS, risk_tier="low", suspicion_flag=True),
        )
        assert decision.verdict is Verdict.BLOCK
        assert decision.governing_rule == "R-626-5.4-NO-SDD-ON-SUSPICION"
        grounds = {step.because for step in decision.derivation.resolution}
        assert "exception" in grounds

    def test_pep_edd_overrides_sdd_and_missing_edd_blocks(self, checker: Checker) -> None:
        decision = checker.check(
            ACTION,
            _ctx("open_account", ALL_PRIORS, risk_tier="low", is_pep=True),
        )
        assert decision.verdict is Verdict.BLOCK
        assert any(
            "request_edd" in item for item in decision.derivation.unmet_obligations
        )
        assert any(
            step.winner == "R-626-6.1-EDD-PEP"
            and step.loser == "R-626-5.3-SDD-LOWRISK"
            and step.because == "explicit-override"
            for step in decision.derivation.resolution
        )

    def test_pep_with_edd_done_escalates_for_senior_approval(self, checker: Checker) -> None:
        decision = checker.check(
            ACTION,
            _ctx("open_account", ALL_PRIORS + ("request_edd",),
                 risk_tier="low", is_pep=True),
        )
        assert decision.verdict is Verdict.ESCALATE
        assert decision.governing_rule == "R-626-6.3-PEP-SENIOR-APPROVAL"


class TestEscalationAndOverridesOnOtherActions:
    def test_data_export_escalates_by_default(self, checker: Checker) -> None:
        decision = checker.check(
            ProposedAction(tool="export_customer_data"),
            _ctx("export_customer_data"),
        )
        assert decision.verdict is Verdict.ESCALATE
        assert decision.governing_rule == "R-DG-1-EXPORT-APPROVAL"

    def test_data_export_blocked_while_str_pending(self, checker: Checker) -> None:
        decision = checker.check(
            ProposedAction(tool="export_customer_data"),
            _ctx("export_customer_data", str_filed=True),
        )
        assert decision.verdict is Verdict.BLOCK
        assert decision.governing_rule == "R-DG-2-EXPORT-BLOCK-PENDING-STR"


class TestTemporalValidity:
    def test_superseded_sdd_rule_applies_only_in_its_window(
        self, policy_graph: PolicyGraph
    ) -> None:
        checker = Checker(policy_graph)
        past_ctx = MappedContext(
            action_type="open_account",
            attributes={"risk_tier": "low", "product_value_band": "below_threshold"},
            prior_actions=ALL_PRIORS,
            jurisdiction="SG",
            timestamp=dt.date(2019, 6, 1),
        )
        decision = checker.check(ACTION, past_ctx)
        assert "R-626-5.2-SDD-THRESHOLD" in decision.derivation.applicable_rules
        today_decision = checker.check(
            ACTION,
            _ctx("open_account", ALL_PRIORS, risk_tier="low",
                 product_value_band="below_threshold"),
        )
        assert "R-626-5.2-SDD-THRESHOLD" not in today_decision.derivation.applicable_rules


class TestFailModesAndTies:
    def _tiny_graph(self) -> PolicyGraph:
        common = dict(valid_from=dt.date(2020, 1, 1), jurisdiction="SG")
        return PolicyGraph(
            [
                Rule(id="P1", derived_from="test", deontic=DeonticStatus.PERMISSION,
                     action="act", priority=10, rationale="permit", **common),
                Rule(id="B1", derived_from="test", deontic=DeonticStatus.PROHIBITION,
                     action="act", conditions={"bad": True}, priority=10,
                     rationale="forbid", **common),
            ]
        )

    def test_unregulated_action_follows_fail_mode(self) -> None:
        graph = self._tiny_graph()
        ctx = _ctx("unknown_action")
        closed = Checker(graph, fail_mode=Verdict.BLOCK).check(ACTION, ctx)
        opened = Checker(graph, fail_mode=Verdict.ALLOW).check(ACTION, ctx)
        assert closed.verdict is Verdict.BLOCK
        assert opened.verdict is Verdict.ALLOW
        assert closed.governing_rule is None

    def test_equal_priority_conflict_resolves_for_prohibition(self) -> None:
        checker = Checker(self._tiny_graph())
        decision = checker.check(ProposedAction(tool="act"), _ctx("act", bad=True))
        assert decision.verdict is Verdict.BLOCK
        assert any(
            step.because == "conservative-tie-break"
            for step in decision.derivation.resolution
        )

    def test_mapper_omission_causes_underblocking_not_crash(self, checker: Checker) -> None:
        """If the mapper drops sanctions_status, the prohibition silently
        fails to apply -- the measured under-blocking channel (H2)."""
        decision = checker.check(ACTION, _ctx("open_account", ALL_PRIORS, risk_tier="low"))
        assert decision.verdict is Verdict.ALLOW
        assert "R-626-4.3-SANCTIONS-BLOCK" not in decision.derivation.applicable_rules