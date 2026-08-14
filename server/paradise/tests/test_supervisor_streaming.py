"""Phase 3-C — supervisor real-time streaming tests.

Run:
    cd server && python -m pytest paradise/tests/test_supervisor_streaming.py -v

Scope:
  * tool_react think_act_node emits tool_call events to event_sink
  * intent_node emits intent events to event_sink
  * run_via_supervisor yields events incrementally (before ainvoke finishes)
  * event_sink is optional (no crash when absent)
  * mcp_manager.aclose() called on both success and crash paths

Architecture:
  asyncio.Queue is bound via ContextVar (paradise.core.streaming) by
  run_via_supervisor → visible to intent_node and tool_react without
  entering serializable graph state. The queue is drained concurrently
  while compiled.ainvoke() runs in a background task.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from paradise.config import ParadiseConfig, BashConfig
from paradise.core.handoff import (
    HandoffRequest, HandoffResponse, HANDOFF_OK,
)
from paradise.core.patterns.tool_react import ToolReactPattern
from paradise.core.registry import SubgraphRegistry
from paradise.core.streaming import set_event_sink, get_event_sink
from paradise.tools.bash_tool import configure as configure_bash
from paradise.tools.registry import discover_builtin_tools


# ─────────────────────────────────────────────────────────────────────
# Shared mock helpers
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _MockToolCall:
    """Duck-type of paradise.transports.types.ToolCall."""
    id: str = "call_1"
    name: str = "bash_read"
    arguments: str = "{}"
    @property
    def type(self): return "function"
    @property
    def function(self): return self


@dataclass
class _MockResponse:
    """Duck-type of NormalizedResponse."""
    content: str | None = ""
    tool_calls: list[_MockToolCall] | None = None
    finish_reason: str = "stop"
    usage: Any = None


class _ScriptedTransport:
    """Returns responses from a queue. Captures all chat() calls."""
    api_mode = "openai_compat"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("scripted transport queue empty")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _make_handoff_request() -> HandoffRequest:
    """Build a plain HandoffRequest (sink travels via ContextVar)."""
    return HandoffRequest(
        session_id="s1",
        user_id="u1",
        trace_id="t1",
        message="test message",
    )


# ─────────────────────────────────────────────────────────────────────
# Test 1: tool_react emits tool_call event to event_sink
# ─────────────────────────────────────────────────────────────────────


class TestToolReactEmitsToolCallEvent:
    @pytest.mark.asyncio
    async def test_tool_call_event_has_correct_shape(self):
        """When LLM returns a tool_call and event_sink is wired via
        ContextVar, tool_react puts an event with {type, name, arguments,
        result, duration_ms} into the queue."""
        discover_builtin_tools()
        configure_bash(BashConfig(enabled=True, read_timeout_seconds=5))

        sink = asyncio.Queue()
        set_event_sink(sink)
        try:
            transport = _ScriptedTransport([
                _MockResponse(
                    content="Running it",
                    tool_calls=[_MockToolCall(
                        id="call_1",
                        name="bash_read",
                        arguments=json.dumps({"command": "echo streaming_test"}),
                    )],
                ),
                _MockResponse(content="done: streaming_test", tool_calls=None),
            ])

            pattern = ToolReactPattern(transport=transport)
            graph = pattern.build(SubgraphRegistry())
            await graph.ainvoke({"handoff": _make_handoff_request()})
        finally:
            set_event_sink(None)

        # Drain the queue
        events = []
        while not sink.empty():
            events.append(sink.get_nowait())

        tool_call_evts = [e for e in events if e.get("type") == "tool_call"]
        assert len(tool_call_evts) == 1, f"expected 1 tool_call event, got {len(tool_call_evts)}"
        evt = tool_call_evts[0]
        assert evt["name"] == "bash_read"
        assert evt["arguments"] == {"command": "echo streaming_test"}
        assert "streaming_test" in evt["result"]
        assert isinstance(evt["duration_ms"], int)
        assert evt["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_emit_multiple_events(self):
        """Two tool_calls in one LLM response → two tool_call events."""
        discover_builtin_tools()
        configure_bash(BashConfig(enabled=True, read_timeout_seconds=5))

        sink = asyncio.Queue()
        set_event_sink(sink)
        try:
            transport = _ScriptedTransport([
                _MockResponse(
                    content="batch",
                    tool_calls=[
                        _MockToolCall(id="a", name="bash_read",
                                      arguments=json.dumps({"command": "echo one"})),
                        _MockToolCall(id="b", name="bash_read",
                                      arguments=json.dumps({"command": "echo two"})),
                    ],
                ),
                _MockResponse(content="all done", tool_calls=None),
            ])

            pattern = ToolReactPattern(transport=transport)
            graph = pattern.build(SubgraphRegistry())
            await graph.ainvoke({"handoff": _make_handoff_request()})
        finally:
            set_event_sink(None)

        events = []
        while not sink.empty():
            events.append(sink.get_nowait())
        tool_call_evts = [e for e in events if e.get("type") == "tool_call"]
        assert len(tool_call_evts) == 2
        assert tool_call_evts[0]["arguments"]["command"] == "echo one"
        assert tool_call_evts[1]["arguments"]["command"] == "echo two"

    @pytest.mark.asyncio
    async def test_tool_error_result_still_emits_event(self):
        """When a tool dispatch raises, the error is captured in the
        result field of the tool_call event (not swallowed)."""
        discover_builtin_tools()
        configure_bash(BashConfig(enabled=True, read_timeout_seconds=5))

        sink = asyncio.Queue()
        set_event_sink(sink)
        try:
            transport = _ScriptedTransport([
                _MockResponse(
                    content="trying",
                    tool_calls=[_MockToolCall(
                        id="x", name="bash_write",
                        arguments=json.dumps({"command": "rm -rf /"}),
                    )],
                ),
                _MockResponse(content="sorry, blocked", tool_calls=None),
            ])

            pattern = ToolReactPattern(transport=transport)
            graph = pattern.build(SubgraphRegistry())
            await graph.ainvoke({"handoff": _make_handoff_request()})
        finally:
            set_event_sink(None)

        events = []
        while not sink.empty():
            events.append(sink.get_nowait())
        tool_call_evts = [e for e in events if e.get("type") == "tool_call"]
        assert len(tool_call_evts) == 1
        # The result should contain the block error (bash_write blocks rm -rf /)
        assert "error" in tool_call_evts[0]["result"].lower() or \
               "block" in tool_call_evts[0]["result"].lower()


# ─────────────────────────────────────────────────────────────────────
# Test 2: no event_sink → tool_react runs normally (no crash)
# ─────────────────────────────────────────────────────────────────────


class TestEventSinkOptional:
    @pytest.mark.asyncio
    async def test_no_sink_no_crash(self):
        """When no ContextVar sink is bound, tool_react should run
        exactly as before Phase 3-C — no AttributeError, no KeyError."""
        discover_builtin_tools()
        configure_bash(BashConfig(enabled=True, read_timeout_seconds=5))

        # Ensure no sink is bound in the current context
        set_event_sink(None)
        assert get_event_sink() is None

        transport = _ScriptedTransport([
            _MockResponse(
                content="",
                tool_calls=[_MockToolCall(
                    id="c1", name="bash_read",
                    arguments=json.dumps({"command": "echo no_sink_ok"}),
                )],
            ),
            _MockResponse(content="ok", tool_calls=None),
        ])

        pattern = ToolReactPattern(transport=transport)
        graph = pattern.build(SubgraphRegistry())
        result = await graph.ainvoke({
            "handoff": _make_handoff_request(),
        })

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert "ok" in resp.output


# ─────────────────────────────────────────────────────────────────────
# Test 3: intent_node emits intent event via _emit_intent_event
# ─────────────────────────────────────────────────────────────────────


class TestIntentNodeEmitsEvent:
    @pytest.mark.asyncio
    async def test_emit_intent_event_pushes_to_sink(self):
        """_emit_intent_event puts a well-formed intent event into the
        ContextVar-bound queue when sink is present."""
        from paradise.core.supervisor import _emit_intent_event

        sink = asyncio.Queue()
        set_event_sink(sink)
        try:
            fused = {
                "intent": "tool_search",
                "intent_confidence": 0.88,
                "intent_source": "llm",
                "mode": "tool_react",
            }
            await _emit_intent_event(fused)
        finally:
            set_event_sink(None)

        assert not sink.empty()
        evt = sink.get_nowait()
        assert evt["type"] == "intent"
        assert evt["content"] == "tool_search"
        assert evt["confidence"] == 0.88
        assert evt["source"] == "llm"
        assert evt["mode"] == "tool_react"

    @pytest.mark.asyncio
    async def test_emit_intent_event_silent_when_no_sink(self):
        """When no ContextVar sink is bound, _emit_intent_event is a
        no-op (no crash, no event put anywhere)."""
        from paradise.core.supervisor import _emit_intent_event

        set_event_sink(None)  # ensure clean state
        fused = {"intent": "chitchat", "mode": "chat"}

        # Should not raise
        await _emit_intent_event(fused)


# ─────────────────────────────────────────────────────────────────────
# Test 4: ContextVar propagates into asyncio.create_task
# ─────────────────────────────────────────────────────────────────────


class TestContextVarPropagation:
    @pytest.mark.asyncio
    async def test_sink_visible_inside_create_task(self):
        """The sink bound via set_event_sink must be visible inside
        a child task created via asyncio.create_task — this is how
        run_via_supervisor makes it visible to compiled.ainvoke nodes."""
        sink = asyncio.Queue()
        set_event_sink(sink)
        try:
            async def _child():
                # This runs in a copied context — must see the sink
                return get_event_sink()

            task = asyncio.create_task(_child())
            result = await task
            assert result is sink
        finally:
            set_event_sink(None)

    @pytest.mark.asyncio
    async def test_sink_not_visible_after_reset(self):
        """After set_event_sink(None), get_event_sink returns None."""
        sink = asyncio.Queue()
        set_event_sink(sink)
        assert get_event_sink() is sink
        set_event_sink(None)
        assert get_event_sink() is None


# ─────────────────────────────────────────────────────────────────────
# Test 5: run_via_supervisor yields events incrementally
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _MockLoopContext:
    user_message: str = "hi"
    session_id: str = "stream-session"
    agent_id: str = "test-agent"
    agent_name: str = "Test"
    enable_tools: bool = True


def _make_minimal_agent():
    """Construct ParadiseAgent bypassing __init__ (avoids disk/emotion)."""
    from paradise.core.agent import ParadiseAgent
    cfg = ParadiseConfig(agent_id="stream-test")
    agent = ParadiseAgent.__new__(ParadiseAgent)
    agent.agent_id = "stream-test"
    agent.config = cfg

    class _Stub:
        def on_interaction(self, *a, **kw): pass
        def to_dict(self): return {}
    agent.emotion_state = _Stub()
    agent.emotion_engine = _Stub()

    class _WorkspaceStub:
        def save_state(self, *a, **kw): pass
    agent.workspace = _WorkspaceStub()
    return agent


class _SlowGraph:
    """Fake compiled graph that emits events with deliberate delays,
    so we can prove run_via_supervisor forwards them BEFORE ainvoke
    returns. Reads sink from ContextVar (same as real nodes)."""

    def __init__(self):
        self.ainvoke_started = asyncio.Event()
        self.ainvoke_finished = asyncio.Event()

    async def ainvoke(self, state, config=None):
        self.ainvoke_started.set()
        sink = get_event_sink()

        # Simulate intent_node emitting after 50ms
        await asyncio.sleep(0.05)
        if sink is not None:
            await sink.put({"type": "intent", "content": "tool_use",
                            "confidence": 0.9, "source": "llm",
                            "mode": "tool_react"})

        # Simulate tool_react emitting after 150ms
        await asyncio.sleep(0.10)
        if sink is not None:
            await sink.put({"type": "tool_call", "name": "bash_read",
                            "arguments": {"command": "echo hi"},
                            "result": "hi", "duration_ms": 5})

        # Simulate cleanup finishing after another 50ms
        await asyncio.sleep(0.05)
        self.ainvoke_finished.set()
        return {
            "intent": "tool_use",
            "handoff_response": HandoffResponse(
                session_id="s", trace_id="t", output="all done"),
            "final_output": "all done",
        }


class TestRunViaSupervisorStreaming:
    @pytest.mark.asyncio
    async def test_events_arrive_before_done(self):
        """The intent and tool_call events must be yielded BEFORE the
        final done event — that's the whole point of Phase 3-C."""
        agent = _make_minimal_agent()
        fake_graph = _SlowGraph()

        timestamps = []
        with patch("paradise.factory.build_supervisor",
                   return_value=(fake_graph, None, None)):
            async for evt in agent.run_via_supervisor(_MockLoopContext()):
                timestamps.append((evt["type"], asyncio.get_event_loop().time()))

        types = [t for t, _ in timestamps]
        assert "intent" in types
        assert "tool_call" in types
        assert "done" in types

        # The critical assertion: intent and tool_call arrive before done
        intent_idx = types.index("intent")
        tool_idx = types.index("tool_call")
        done_idx = types.index("done")
        assert intent_idx < done_idx
        assert tool_idx < done_idx

        # And intent arrives before tool_call (causal order)
        assert intent_idx < tool_idx

    @pytest.mark.asyncio
    async def test_crash_yields_error_and_closes_mcp(self):
        """If the supervisor graph raises, run_via_supervisor must:
        1) yield an error event (not raise)
        2) still close the mcp_manager in finally"""
        agent = _make_minimal_agent()

        class _CrashingGraph:
            async def ainvoke(self, state, config=None):
                raise RuntimeError("kaboom")

        fake_mcp = MagicMock()
        fake_mcp.aclose = MagicMock(return_value=asyncio.sleep(0))

        with patch("paradise.factory.build_supervisor",
                   return_value=(_CrashingGraph(), None, fake_mcp)):
            events = []
            async for evt in agent.run_via_supervisor(_MockLoopContext()):
                events.append(evt)

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "kaboom" in events[0]["content"]
        # mcp_manager.aclose must have been awaited
        fake_mcp.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_success_closes_mcp(self):
        """On a successful run, mcp_manager.aclose() is still called
        (finally block runs on all paths)."""
        agent = _make_minimal_agent()

        class _OkGraph:
            async def ainvoke(self, state, config=None):
                return {
                    "handoff_response": HandoffResponse(
                        session_id="s", trace_id="t", output="ok"),
                    "final_output": "ok",
                }

        fake_mcp = MagicMock()
        fake_mcp.aclose = MagicMock(return_value=asyncio.sleep(0))

        with patch("paradise.factory.build_supervisor",
                   return_value=(_OkGraph(), None, fake_mcp)):
            events = []
            async for evt in agent.run_via_supervisor(_MockLoopContext()):
                events.append(evt)

        fake_mcp.aclose.assert_called_once()
        assert events[-1]["type"] == "done"
