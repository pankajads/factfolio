"""run_review's on_event callback — the live status line `factfolio report`
shows during a multi-minute run instead of sitting silently. Exercised
against a fake `query()` so this never touches the real API: what matters
here is that tool calls, subagent dispatches, and text chunks each produce
exactly the event the CLI's spinner expects, not model behaviour.
"""

from __future__ import annotations

import anyio
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from mybroker.agents import orchestrator


def _fake_result_message(**overrides):
    defaults = dict(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=3,
        session_id="test-session",
        total_cost_usd=0.05,
    )
    defaults.update(overrides)
    return ResultMessage(**defaults)


async def _fake_query(*, prompt, options):
    yield AssistantMessage(
        content=[ToolUseBlock(id="1", name="get_quote", input={"symbol": "TCS"})],
        model="test-model",
    )
    yield AssistantMessage(
        content=[ToolUseBlock(
            id="2", name="Agent", input={"subagent_type": "market-analyst"},
        )],
        model="test-model",
    )
    yield AssistantMessage(
        content=[TextBlock(text="# Portfolio Review\nEverything looks fine.")],
        model="test-model",
    )
    yield _fake_result_message()


def test_on_event_fires_for_tool_calls_agent_dispatch_and_text(monkeypatch):
    monkeypatch.setattr(orchestrator, "query", _fake_query)
    monkeypatch.setattr(orchestrator, "build_options", lambda run_id: object())

    events: list[str] = []
    result = anyio.run(orchestrator.run_review, "test prompt", events.append)

    assert events == [
        "tool: get_quote",
        "dispatching market-analyst",
        "writing…",
    ]
    assert result.report == "# Portfolio Review\nEverything looks fine."
    assert result.tool_calls == ["get_quote", "Agent"]
    assert result.turns == 3
    assert result.cost_usd == 0.05


def test_on_event_is_optional(monkeypatch):
    """Every other caller (tests, the MCP server) leaves on_event unset —
    must not require it."""
    monkeypatch.setattr(orchestrator, "query", _fake_query)
    monkeypatch.setattr(orchestrator, "build_options", lambda run_id: object())

    result = anyio.run(orchestrator.run_review, "test prompt")
    assert result.tool_calls == ["get_quote", "Agent"]
