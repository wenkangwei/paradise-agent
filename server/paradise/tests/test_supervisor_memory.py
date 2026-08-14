"""Phase 5 — memory + reflection wiring in supervisor cleanup_node.

Verifies:
  1. cleanup_node calls memory_provider.sync_turn(user_msg, final_output)
  2. memory sync exception doesn't crash the turn
  3. cleanup_node schedules reflection_engine.on_turn_complete (fire-and-forget)
  4. memory_synced flag set in final state when provider is wired

Run:
    cd server && python -m pytest paradise/tests/test_supervisor_memory.py -v
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from paradise.core.handoff import HANDOFF_OK, HandoffResponse
from paradise.core.patterns import ChatPattern, register_all, register_basic_patterns
from paradise.core.registry import SubgraphRegistry
from paradise.core.supervisor import build_supervisor_graph


# ─────────────────────────────────────────────────────────────────────
# Minimal mocks — just enough for cleanup_node to reach its memory hooks
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _MockResponse:
    content: str
    tool_calls: list | None = None
    usage: Any = None
    finish_reason: str = "stop"


class _ScriptedTransport:
    """Returns one canned response per chat() call."""
    api_mode = "openai_compat"

    def __init__(self, response: _MockResponse):
        self._response = response

    async def chat(self, **kwargs):
        return self._response


def _build_chat_registry(content: str = "hi there"):
    """Build a registry with a real ChatPattern backed by a mock transport."""
    transport = _ScriptedTransport(_MockResponse(content=content))
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=transport)
    return registry


def _initial_state(message: str = "hello") -> dict:
    return {
        "user_message": message,
        "session_id": "sess-mem-1",
        "trace_id": "trace-mem-1",
        "user_id": "user-1",
        "user_tier": "free",
    }


def _config(tid: str = "trace-mem-1") -> dict:
    return {"configurable": {"thread_id": tid}}


# ─────────────────────────────────────────────────────────────────────
# 1. memory_provider.sync_turn is called
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_calls_memory_sync_turn():
    """cleanup_node calls memory_provider.sync_turn(user_msg, final_output)."""
    memory = MagicMock()
    memory.sync_turn = MagicMock()

    registry = _build_chat_registry(content="Hello user!")
    graph = build_supervisor_graph(
        registry,
        memory_provider=memory,
        reflection_engine=None,
    )

    result = await graph.ainvoke(_initial_state("hi"), config=_config())

    memory.sync_turn.assert_called_once_with("hi", "Hello user!")
    assert result.get("memory_synced") is True
    assert result["final_output"] == "Hello user!"


# ─────────────────────────────────────────────────────────────────────
# 2. memory sync error doesn't crash the turn
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_memory_error_doesnt_crash():
    """Memory sync exception is swallowed — turn still succeeds."""
    memory = MagicMock()
    memory.sync_turn.side_effect = RuntimeError("DB connection lost")

    registry = _build_chat_registry(content="response")
    graph = build_supervisor_graph(
        registry,
        memory_provider=memory,
    )

    result = await graph.ainvoke(_initial_state("q"), config=_config("err-1"))

    # Turn completed despite memory error
    assert result["final_output"] == "response"
    # memory_synced not set because sync raised
    assert "memory_synced" not in result or result.get("memory_synced") is not True


# ─────────────────────────────────────────────────────────────────────
# 3. reflection_engine.on_turn_complete scheduled (fire-and-forget)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_schedules_reflection():
    """cleanup_node schedules on_turn_complete as a background task."""
    reflection = MagicMock()
    reflection.on_turn_complete = AsyncMock()

    registry = _build_chat_registry(content="answer")
    graph = build_supervisor_graph(
        registry,
        memory_provider=None,
        reflection_engine=reflection,
    )

    await graph.ainvoke(_initial_state("question"), config=_config("refl-1"))

    # Give the fire-and-forget task time to execute
    await asyncio.sleep(0.15)

    reflection.on_turn_complete.assert_called_once()
    call_kwargs = reflection.on_turn_complete.call_args.kwargs
    assert call_kwargs["user_message"] == "question"
    assert call_kwargs["agent_response"] == "answer"


# ─────────────────────────────────────────────────────────────────────
# 4. No memory/reflection providers → backward compat (no crash)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_providers_backward_compat():
    """When memory_provider=None and reflection_engine=None,
    cleanup_node behaves like Phase 2.13 (no memory hooks)."""
    registry = _build_chat_registry(content="plain")
    graph = build_supervisor_graph(
        registry,
        memory_provider=None,
        reflection_engine=None,
    )

    result = await graph.ainvoke(_initial_state("msg"), config=_config("bc-1"))

    assert result["final_output"] == "plain"
    assert "memory_synced" not in result


# ─────────────────────────────────────────────────────────────────────
# 5. reflection scheduling error doesn't crash
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reflection_error_doesnt_crash():
    """If on_turn_complete raises synchronously (before task creation),
    cleanup_node catches it and still returns final_output."""
    reflection = MagicMock()
    # Make the property access or method call raise synchronously
    reflection.on_turn_complete = MagicMock(
        side_effect=RuntimeError("reflection broken")
    )

    registry = _build_chat_registry(content="safe output")
    graph = build_supervisor_graph(
        registry,
        reflection_engine=reflection,
    )

    result = await graph.ainvoke(_initial_state("test"), config=_config("refl-err"))

    assert result["final_output"] == "safe output"
