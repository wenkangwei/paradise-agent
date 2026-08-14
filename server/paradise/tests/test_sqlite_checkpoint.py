"""Phase 5 — AsyncSqliteSaver checkpoint persistence tests.

Verifies:
  1. Checkpoint survives graph rebuild (same DB, same thread_id)
  2. HandoffResponse dataclass round-trips through AsyncSqliteSaver
  3. Different thread_ids get independent checkpoints

Run:
    cd server && python -m pytest paradise/tests/test_sqlite_checkpoint.py -v

Skips if langgraph-checkpoint-sqlite is not installed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from paradise.core.handoff import HANDOFF_OK, HandoffResponse
from paradise.core.patterns import ChatPattern, register_all, register_basic_patterns
from paradise.core.registry import SubgraphRegistry
from paradise.core.supervisor import build_supervisor_graph


# Skip entire module if sqlite checkpoint dep is missing
sqlite_saver_mod = pytest.importorskip("langgraph.checkpoint.sqlite")
# Also skip if aiosqlite is not available (required for AsyncSqliteSaver)
pytest.importorskip("aiosqlite")

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver, _ensure_connected
import aiosqlite


# ─────────────────────────────────────────────────────────────────────
# Mocks
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _MockResponse:
    content: str
    tool_calls: list | None = None
    usage: Any = None
    finish_reason: str = "stop"


class _ScriptedTransport:
    api_mode = "openai_compat"

    def __init__(self, response: _MockResponse):
        self._response = response

    async def chat(self, **kwargs):
        return self._response


def _build_chat_registry(content: str = "checkpoint test"):
    transport = _ScriptedTransport(_MockResponse(content=content))
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=transport)
    return registry


def _initial_state(message: str = "hello") -> dict:
    return {
        "user_message": message,
        "session_id": "sess-chk-1",
        "trace_id": "trace-chk-1",
        "user_id": "user-1",
        "user_tier": "free",
    }


class _WALAsyncSqliteSaver(AsyncSqliteSaver):
    """Test helper: sets WAL + busy_timeout before table creation."""

    async def setup(self):
        await _ensure_connected(self.conn)
        cur = await self.conn.execute("PRAGMA journal_mode=WAL")
        await cur.fetchone()
        cur = await self.conn.execute("PRAGMA busy_timeout=5000")
        await cur.fetchone()
        await super().setup()


def _make_saver(db_path: str):
    """Create an AsyncSqliteSaver for test stability."""
    conn = aiosqlite.connect(db_path)
    return AsyncSqliteSaver(conn)


# ─────────────────────────────────────────────────────────────────────
# 1. Checkpoint survives graph rebuild
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkpoint_survives_graph_rebuild(tmp_path):
    """Graph A writes checkpoint → Graph B (same DB, same thread_id)
    reads it back via aget_state."""
    db_path = str(tmp_path / "test_checkpoint.db")

    # Graph A: run a turn and write checkpoint
    saver_a = _make_saver(db_path)
    registry_a = _build_chat_registry(content="remember this")
    graph_a = build_supervisor_graph(
        registry_a,
        checkpointer=saver_a,
    )

    state_a = _initial_state("test message")
    config = {"configurable": {"thread_id": "session-rebuild-1"}}
    await graph_a.ainvoke(state_a, config=config)

    # Graph B: same DB file, same thread_id → should see checkpoint
    saver_b = _make_saver(db_path)
    registry_b = _build_chat_registry(content="different response")
    graph_b = build_supervisor_graph(
        registry_b,
        checkpointer=saver_b,
    )

    state_b = await graph_b.aget_state(config=config)

    # Verify the checkpoint has the user_message from graph A
    assert state_b is not None
    assert state_b.values.get("user_message") == "test message"
    # final_output should also be preserved
    assert state_b.values.get("final_output") == "remember this"


# ─────────────────────────────────────────────────────────────────────
# 2. HandoffResponse dataclass survives checkpoint round-trip
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handoff_dataclass_checkpoint_roundtrip(tmp_path):
    """Verify HandoffResponse (a dataclass) survives SqliteSaver
    serialization + deserialization without field loss.

    This is the critical risk-1 regression test from the plan:
    langgraph-checkpoint-sqlite uses JsonPlusSerializer which may or
    may not handle arbitrary dataclasses. If this test fails, the
    fallback is to store handoff_response as dict in cleanup_node.
    """
    db_path = str(tmp_path / "test_dataclass.db")

    saver = _make_saver(db_path)
    registry = _build_chat_registry(content="dataclass roundtrip")
    graph = build_supervisor_graph(
        registry,
        checkpointer=saver,
    )

    config = {"configurable": {"thread_id": "dc-roundtrip-1"}}
    await graph.ainvoke(_initial_state("check dc"), config=config)

    state = await graph.aget_state(config=config)
    assert state is not None

    hr = state.values.get("handoff_response")
    assert hr is not None, "handoff_response missing from checkpoint"

    # HandoffResponse may come back as dataclass or dict depending on
    # serializer. Handle both.
    if hasattr(hr, "output"):
        output = hr.output
        status = hr.status
    elif isinstance(hr, dict):
        output = hr.get("output", "")
        status = hr.get("status", "")
    else:
        pytest.fail(f"Unexpected handoff_response type: {type(hr)}")

    assert output == "dataclass roundtrip"
    assert status == HANDOFF_OK


# ─────────────────────────────────────────────────────────────────────
# 3. Different thread_ids get independent checkpoints
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_different_threads_isolated(tmp_path):
    """Two threads in the same DB don't interfere."""
    db_path = str(tmp_path / "test_threads.db")

    saver = _make_saver(db_path)
    registry = _build_chat_registry(content="thread response")
    graph = build_supervisor_graph(
        registry,
        checkpointer=saver,
    )

    # Thread 1
    config1 = {"configurable": {"thread_id": "thread-A"}}
    await graph.ainvoke(
        _initial_state("message A"),
        config=config1,
    )

    # Thread 2
    config2 = {"configurable": {"thread_id": "thread-B"}}
    await graph.ainvoke(
        _initial_state("message B"),
        config=config2,
    )

    # Verify isolation
    state_a = await graph.aget_state(config=config1)
    state_b = await graph.aget_state(config=config2)

    assert state_a.values.get("user_message") == "message A"
    assert state_b.values.get("user_message") == "message B"
