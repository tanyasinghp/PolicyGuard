"""Agent runtime: the tool-use loop, with or without the guard.

The runtime is condition-agnostic plumbing: it sends the conversation to
the model, intercepts tool_use blocks, consults the guard (condition B)
or executes directly (condition A), and feeds results back until the
model stops or ``max_turns`` is reached.

Guard integration contract:
    * Every proposed tool call goes through ``guard.check`` BEFORE
      execution; only ALLOW executes.
    * ``guard.record_execution`` is called only after actual execution,
      preserving the guard-recorded priors contract.
    * BLOCK/ESCALATE verdicts return the derivation text to the agent as
      an error tool result, so the agent can satisfy unmet obligations
      and continue (blocked is not crashed).

The Anthropic client is dependency-injected (``client.messages``), so
tests drive the loop with scripted responses and no network.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agent.tools import TOOLS_SPEC, BankWorld, build_dispatch
from guard.guard import Guard
from guard.models import ProposedAction, Verdict

logger = logging.getLogger(__name__)

MAX_EVIDENCE_CHARS = 6000
"""Evidence passed to the mapper is the tail of accumulated tool output."""


class _MessagesClient(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class ToolEvent(BaseModel):
    """One proposed tool call and what became of it."""

    tool: str
    args: dict[str, Any]
    verdict: str  # "allow" | "block" | "escalate" | "unguarded"
    governing_rule: str | None = None
    result: str


class EpisodeResult(BaseModel):
    """Everything the eval harness needs about one episode run."""

    episode_id: str
    guarded: bool
    tool_events: list[ToolEvent] = Field(default_factory=list)
    final_message: str = ""
    turns: int = 0
    aborted: bool = False


class AgentRuntime:
    """Runs onboarding episodes against a model with optional guarding.

    Args:
        client: Anthropic ``client.messages`` handle (or a test stub).
        model: Model ID for the agent.
        max_turns: Hard cap on assistant turns per episode.
    """

    def __init__(self, client: _MessagesClient, model: str,
                 max_turns: int = 12) -> None:
        self._client = client
        self._model = model
        self._max_turns = max_turns

    def run_episode(
        self,
        episode_id: str,
        world: BankWorld,
        task: str,
        system_prompt: str,
        guard: Guard | None,
    ) -> EpisodeResult:
        """Run one episode to completion.

        Args:
            episode_id: Stable episode identifier (audit + eval joins).
            world: The episode's banking world (fresh per run).
            task: The user-turn task description.
            system_prompt: Condition-appropriate system prompt.
            guard: Guard instance for condition B, or None for A.
        """
        dispatch = build_dispatch(world)
        result = EpisodeResult(episode_id=episode_id, guarded=guard is not None)
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        evidence = ""

        for _ in range(self._max_turns):
            result.turns += 1
            try:
                response = self._client.create(
                    model=self._model,
                    max_tokens=1500,
                    system=system_prompt,
                    tools=TOOLS_SPEC,
                    messages=messages,
                )
            except Exception as exc:
                logger.error("model call failed on %s: %s", episode_id, exc)
                result.aborted = True
                return result

            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    result.final_message = block.text
                elif getattr(block, "type", None) == "tool_use":
                    outcome = self._handle_tool_call(
                        episode_id, block, dispatch, guard, evidence, result
                    )
                    evidence = outcome["evidence"]
                    tool_results.append(outcome["tool_result"])

            if not tool_results:  # no tool_use -> the model is done
                return result
            messages.append({"role": "assistant",
                             "content": self._dump_content(response.content)})
            messages.append({"role": "user", "content": tool_results})

        result.aborted = True  # ran out of turns
        return result

    def _handle_tool_call(
        self,
        episode_id: str,
        block: Any,
        dispatch: dict[str, Any],
        guard: Guard | None,
        evidence: str,
        result: EpisodeResult,
    ) -> dict[str, Any]:
        """Guard-check (if applicable), maybe execute, build the tool result."""
        action = ProposedAction(tool=block.name, args=dict(block.input))
        if guard is not None:
            decision = guard.check(episode_id, action,
                                   evidence[-MAX_EVIDENCE_CHARS:])
            if decision.verdict is not Verdict.ALLOW:
                text = (
                    f"[POLICY GUARD: {decision.verdict.value.upper()}] "
                    f"This action was not executed.\n"
                    f"{decision.derivation.to_text()}"
                )
                result.tool_events.append(ToolEvent(
                    tool=action.tool, args=action.args,
                    verdict=decision.verdict.value,
                    governing_rule=decision.governing_rule, result=text,
                ))
                return {
                    "evidence": evidence,
                    "tool_result": {
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": text, "is_error": True,
                    },
                }
        output = self._execute(dispatch, action)
        if guard is not None:
            guard.record_execution(episode_id, action.tool)
        result.tool_events.append(ToolEvent(
            tool=action.tool, args=action.args,
            verdict="allow" if guard is not None else "unguarded",
            result=output,
        ))
        return {
            "evidence": evidence + f"\n[{action.tool}] {output}",
            "tool_result": {
                "type": "tool_result", "tool_use_id": block.id,
                "content": output,
            },
        }

    @staticmethod
    def _execute(dispatch: dict[str, Any], action: ProposedAction) -> str:
        try:
            return dispatch[action.tool](**action.args)
        except KeyError:
            return f"error: unknown tool {action.tool}"
        except Exception as exc:
            return f"error: {exc}"

    @staticmethod
    def _dump_content(content: list[Any]) -> list[dict[str, Any]]:
        """Serialize response blocks back into message-history form."""
        dumped: list[dict[str, Any]] = []
        for block in content:
            if getattr(block, "type", None) == "text":
                dumped.append({"type": "text", "text": block.text})
            elif getattr(block, "type", None) == "tool_use":
                dumped.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": dict(block.input),
                })
        return dumped


def build_client() -> Any:
    """Construct the real Anthropic messages client from the environment."""
    import os

    from anthropic import Anthropic
    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set; copy .env.example to .env")
    return Anthropic(api_key=api_key).messages