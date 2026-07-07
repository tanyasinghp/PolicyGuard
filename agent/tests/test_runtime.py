"""Runtime tests with a scripted model: no network, deterministic loops."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.runtime import AgentRuntime
from agent.tools import BankWorld, CustomerProfile
from guard.checker import Checker
from guard.guard import Guard
from guard.mapper import OracleMapper
from policy.compile_graph import compile_policy

TODAY = dt.date(2026, 7, 6)
RULES_PATH = Path(__file__).resolve().parents[1].parent / "policy" / "rules_sg_mas626.yaml"


@dataclass
class _ToolUse:
    name: str
    input: dict[str, Any]
    id: str = "tu_1"
    type: str = "tool_use"


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _Response:
    content: list[Any] = field(default_factory=list)


class _ScriptedClient:
    """Plays back a fixed sequence of model responses."""

    def __init__(self, script: list[_Response]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self._script.pop(0)


def _world() -> BankWorld:
    return BankWorld(
        [CustomerProfile(customer_id="c-sanc", name="S", on_sanctions_list=True)],
        today=TODAY,
    )


def _guard(world: BankWorld) -> Guard:
    return Guard(
        Checker(compile_policy(RULES_PATH)),
        OracleMapper(world.revealed, world.str_filed),
        today=TODAY,
    )


OPEN = _ToolUse(name="open_account", input={"customer_id": "c-sanc"})
DONE = _Response(content=[_Text(text="All done.")])


def test_unguarded_agent_executes_immediately() -> None:
    world = _world()
    runtime = AgentRuntime(_ScriptedClient([_Response(content=[OPEN]), DONE]),
                           model="stub")
    result = runtime.run_episode("ep", world, "Onboard c-sanc", "system", guard=None)
    assert world.accounts.get("c-sanc") == "open"          # harm executed
    assert result.tool_events[0].verdict == "unguarded"
    assert result.final_message == "All done."


def test_guarded_agent_is_blocked_and_receives_derivation() -> None:
    world = _world()
    runtime = AgentRuntime(_ScriptedClient([_Response(content=[OPEN]), DONE]),
                           model="stub")
    result = runtime.run_episode("ep", world, "Onboard c-sanc", "system",
                                 guard=_guard(world))
    assert "c-sanc" not in world.accounts                  # harm prevented
    event = result.tool_events[0]
    assert event.verdict == "block"
    assert "POLICY GUARD" in event.result
    assert "unmet obligation" in event.result


def test_blocked_agent_can_recover_and_priors_are_recorded() -> None:
    world = _world()
    script = [
        _Response(content=[OPEN]),                                   # blocked
        _Response(content=[_ToolUse(name="verify_identity",
                                    input={"customer_id": "c-sanc"}, id="tu_2")]),
        DONE,
    ]
    runtime = AgentRuntime(_ScriptedClient(script), model="stub")
    guard = _guard(world)
    result = runtime.run_episode("ep", world, "Onboard c-sanc", "system", guard=guard)
    verdicts = [event.verdict for event in result.tool_events]
    assert verdicts == ["block", "allow"]
    assert guard.executed_actions("ep") == ("verify_identity",)


def test_turn_cap_marks_episode_aborted() -> None:
    world = _world()
    looping = [_Response(content=[_ToolUse(name="verify_identity",
                                           input={"customer_id": "c-sanc"},
                                           id=f"tu_{i}")])
               for i in range(20)]
    runtime = AgentRuntime(_ScriptedClient(looping), model="stub", max_turns=3)
    result = runtime.run_episode("ep", world, "task", "system", guard=None)
    assert result.aborted is True
    assert result.turns == 3