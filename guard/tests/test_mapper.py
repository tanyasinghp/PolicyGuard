"""Tests for mappers: oracle behavior and LLM parsing via a stub client."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from guard.mapper import LLMMapper, MapperError, OracleMapper
from guard.models import ProposedAction
from policy.compile_graph import compile_policy

TODAY = dt.date(2026, 7, 6)
RULES_PATH = Path(__file__).resolve().parents[2] / "policy" / "rules_sg_mas626.yaml"


class TestOracleMapper:
    def test_maps_revealed_sanctions_and_risk(self) -> None:
        oracle = OracleMapper(
            revealed={"c1": {"sanctions_status": "unresolved_match",
                             "risk_tier": "high"}},
            str_filed=set(),
        )
        ctx = oracle.map(
            ProposedAction(tool="open_account", args={"customer_id": "c1"}),
            evidence="screening result: unresolved_match; PEP: yes",
            prior_actions=("screen_sanctions",),
            jurisdiction="SG",
            today=TODAY,
        )
        assert ctx.attributes["sanctions_status"] == "unresolved_match"
        assert ctx.attributes["risk_tier"] == "high"
        assert ctx.attributes["is_pep"] is True
        assert ctx.prior_actions == ("screen_sanctions",)

    def test_email_disclosure_detection(self) -> None:
        oracle = OracleMapper(revealed={"c1": {}}, str_filed={"c1"})
        ctx = oracle.map(
            ProposedAction(
                tool="send_customer_email",
                args={"customer_id": "c1", "subject": "Update",
                      "body": "We filed a suspicious transaction report about you."},
            ),
            evidence="",
            prior_actions=(),
            jurisdiction="SG",
            today=TODAY,
        )
        assert ctx.attributes["discloses_str"] is True
        assert ctx.attributes["str_filed"] is True


@dataclass
class _StubBlock:
    text: str


class _StubResponse:
    def __init__(self, text: str) -> None:
        self.content = [_StubBlock(text)]


class _StubClient:
    """Injectable stand-in for client.messages; records the last call."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> _StubResponse:
        self.last_kwargs = kwargs
        return _StubResponse(self.reply)


@pytest.fixture(scope="module")
def graph():  # noqa: ANN201
    return compile_policy(RULES_PATH)


class TestLLMMapper:
    def test_parses_json_and_filters_to_vocabulary(self, graph) -> None:  # noqa: ANN001
        stub = _StubClient(
            '{"attributes": {"risk_tier": "low", "made_up_key": 1}, "confidence": 0.9}'
        )
        mapper = LLMMapper(graph, client=stub, model="stub-model")
        ctx = mapper.map(
            ProposedAction(tool="open_account", args={"customer_id": "c1"}),
            evidence="risk tier: low",
            prior_actions=(),
            jurisdiction="SG",
            today=TODAY,
        )
        assert ctx.attributes == {"risk_tier": "low"}
        assert ctx.confidence == 0.9
        assert stub.last_kwargs is not None
        assert "sanctions_status" in stub.last_kwargs["system"]

    def test_code_fenced_json_is_tolerated(self, graph) -> None:  # noqa: ANN001
        stub = _StubClient('```json\n{"attributes": {}, "confidence": 0.4}\n```')
        mapper = LLMMapper(graph, client=stub, model="stub-model")
        ctx = mapper.map(
            ProposedAction(tool="file_str", args={}),
            evidence="", prior_actions=(), jurisdiction="SG", today=TODAY,
        )
        assert ctx.confidence == 0.4

    def test_garbage_output_raises_mapper_error(self, graph) -> None:  # noqa: ANN001
        stub = _StubClient("I think the customer is probably fine!")
        mapper = LLMMapper(graph, client=stub, model="stub-model")
        with pytest.raises(MapperError):
            mapper.map(
                ProposedAction(tool="open_account", args={}),
                evidence="", prior_actions=(), jurisdiction="SG", today=TODAY,
            )