"""Phase 2 tests — LangGraph orchestration.

Verifies:
1. Graph compiles with 4 nodes
2. State shape correct
3. Flag-gated activation (langgraph_enabled=False → handle_message path)
4. End-to-end graph run with mocked agent (no LLM calls)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ── Import + structure ────────────────────────────────────────────

def test_graph_module_importable():
    """paradise.core.graph must import cleanly when langgraph installed."""
    from paradise.core import graph
    assert hasattr(graph, "AgentState")
    assert hasattr(graph, "build_agent_graph")
    assert hasattr(graph, "stream_graph_events")


def test_agent_state_is_typed_dict():
    """AgentState must declare required input fields."""
    from paradise.core.graph import AgentState
    hints = AgentState.__annotations__
    for required in ("user_message", "agent_id", "session_id", "tool_results",
                      "thinking_text", "response_text", "events"):
        assert required in hints, f"AgentState missing field: {required}"


# ── Agent.run_via_graph exists ────────────────────────────────────

def test_run_via_graph_method_exists():
    """ParadiseAgent must expose run_via_graph as an async generator method."""
    from paradise.core.agent import ParadiseAgent
    assert hasattr(ParadiseAgent, "run_via_graph")
    # Verify it's still a coroutine function (async generator)
    import inspect
    assert inspect.isasyncgenfunction(ParadiseAgent.run_via_graph)


def test_handle_message_still_exists():
    """Main-branch entry point must remain untouched (coexistence)."""
    from paradise.core.agent import ParadiseAgent
    assert hasattr(ParadiseAgent, "handle_message")
    import inspect
    assert inspect.isasyncgenfunction(ParadiseAgent.handle_message)


# ── Config flag ───────────────────────────────────────────────────

def test_langgraph_enabled_defaults_false():
    """langgraph_enabled must default False so main branch unaffected."""
    from paradise.config import ParadiseConfig
    cfg = ParadiseConfig()
    assert cfg.langgraph_enabled is False


def test_from_dict_parses_langgraph_enabled():
    """Config YAML must be able to set langgraph_enabled=True."""
    from paradise.config import ParadiseConfig
    cfg = ParadiseConfig.from_dict({"langgraph_enabled": True})
    assert cfg.langgraph_enabled is True


# ── Graph topology ────────────────────────────────────────────────

def test_graph_has_four_nodes():
    """Compiled graph must have exactly: tool, think, respond, reflect."""
    from paradise.core.graph import build_agent_graph
    fake_agent = MagicMock()
    fake_agent._tool_phase = AsyncMock(return_value=aiter([]))
    fake_agent._think_phase = AsyncMock(return_value="")
    fake_agent._respond_phase = AsyncMock(return_value=aiter([]))
    fake_agent._reflection_phase = AsyncMock(return_value=None)
    fake_agent.agent_id = "test"

    compiled = build_agent_graph(fake_agent)
    # LangGraph compiled graph exposes nodes via .nodes
    node_names = set(compiled.nodes.keys())
    # Filter out langgraph's built-in __start__/__end__
    real_nodes = {n for n in node_names if not n.startswith("__")}
    assert {"tool", "think", "respond", "reflect"} <= real_nodes


# ── End-to-end with mocked phase methods ──────────────────────────

@pytest.mark.asyncio
async def test_graph_streams_events_end_to_end():
    """With mocked _xxx_phase methods, the graph should stream events in
    the same shape as handle_message."""
    from paradise.core.graph import build_agent_graph, stream_graph_events

    # Mock agent with controllable phase outputs
    async def mock_tool_phase(ctx):
        yield {"type": "tool_call", "name": "search", "arguments": {}, "result": "x"}
        yield {"type": "tool_results", "content": "search result text"}

    async def mock_think_phase(ctx, tool_results):
        return "internal reasoning"

    async def mock_respond_phase(ctx, tool_results, thinking):
        yield {"type": "content", "content": "Hello "}
        yield {"type": "content", "content": "world"}
        yield {"type": "done", "content": "Hello world", "thinking": thinking}

    async def mock_reflect_phase(ctx):
        return None

    fake_agent = MagicMock()
    fake_agent._tool_phase = mock_tool_phase
    fake_agent._think_phase = mock_think_phase
    fake_agent._respond_phase = mock_respond_phase
    fake_agent._reflection_phase = mock_reflect_phase
    fake_agent.agent_id = "agent-x"

    compiled = build_agent_graph(fake_agent)
    initial = {
        "user_message": "Please explain quantum computing",  # non-greeting → triggers think
        "agent_id": "agent-x",
        "session_id": "sess-1",
        "enable_tools": True,
    }
    events = []
    async for event in stream_graph_events(compiled, initial, "thread-1"):
        events.append(event)

    # Assert event sequence: tool_call → tool_results(hidden) → thinking → content → content → done
    types = [e.get("type") for e in events]
    assert "tool_call" in types
    assert "thinking" in types
    assert "content" in types
    assert "done" in types
    # The done event must carry the full response content
    done_events = [e for e in events if e.get("type") == "done"]
    assert done_events
    assert done_events[0]["content"] == "Hello world"
    assert done_events[0]["thinking"] == "internal reasoning"


# ── Helper ─────────────────────────────────────────────────────────

async def aiter(items):
    """Make an async generator from a list — for AsyncMock return values."""
    for item in items:
        yield item
