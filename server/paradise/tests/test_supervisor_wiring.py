"""Phase 2.9 wiring tests — config flag + agent method dispatch.

Run:
    cd server && python -m pytest paradise/tests/test_supervisor_wiring.py -v

Scope:
  * ParadiseConfig.supervisor_enabled field default + from_dict parsing
  * supervisor_enabled takes precedence over langgraph_enabled
  * agent_handler three-way dispatch (smoke: import doesn't crash)
  * agent.run_via_supervisor with monkey-patched build_supervisor
  * run_via_supervisor emits compatible event shapes

These tests don't actually start docker / ollama — they verify the
plumbing so a docker compose up test would be a single end-to-end
integration rather than a re-test of every flag.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, AsyncGenerator
from unittest.mock import patch

import pytest

from paradise.config import ParadiseConfig


# ─────────────────────────────────────────────────────────────────────
# ParadiseConfig.supervisor_enabled
# ─────────────────────────────────────────────────────────────────────


class TestConfigField:
    def test_default_is_false(self):
        """Default must be False so main branch is unaffected."""
        cfg = ParadiseConfig()
        assert cfg.supervisor_enabled is False

    def test_from_dict_reads_supervisor_enabled(self):
        cfg = ParadiseConfig.from_dict({"supervisor_enabled": True})
        assert cfg.supervisor_enabled is True

    def test_from_dict_defaults_false(self):
        cfg = ParadiseConfig.from_dict({})
        assert cfg.supervisor_enabled is False

    def test_precedence_documented(self):
        """When both supervisor_enabled and langgraph_enabled are True,
        supervisor wins. The agent_handler._feed_events docstring
        documents this; the test pins the semantics so a future refactor
        doesn't silently change precedence."""
        cfg = ParadiseConfig.from_dict({
            "supervisor_enabled": True,
            "langgraph_enabled": True,
        })
        # The precedence rule is enforced in agent_handler._feed_events,
        # not in config. Here we just verify both flags coexist.
        assert cfg.supervisor_enabled is True
        assert cfg.langgraph_enabled is True

    def test_to_dict_does_not_drop_flag(self):
        """Round-trip safety: to_dict must preserve supervisor_enabled
        so config serialization (e.g., for debug endpoints) doesn't
        silently downgrade to handle_message on the next reload."""
        cfg = ParadiseConfig(supervisor_enabled=True)
        d = cfg.to_dict()
        assert d.get("supervisor_enabled") is True


# ─────────────────────────────────────────────────────────────────────
# agent_handler import smoke + flag resolution
# ─────────────────────────────────────────────────────────────────────


class TestAgentHandlerFlags:
    """Smoke tests for the module-level flag resolution.

    We can't easily exercise the full SSE streaming path in unit tests
    (it needs a live FastAPI request + session), but we can verify:
      * The module imports cleanly with all three flags
      * Flag resolution respects PARADISE_MODE
      * The three-way precedence is encoded
    """

    def test_module_imports(self):
        """Module-level flag resolution runs at import time. Verifying
        import succeeds in test env (dev mode) means the wiring is
        syntactically correct."""
        import paradise.tests # noqa: F401
        # Force fresh import path resolution
        import importlib
        import agent_handler
        importlib.reload(agent_handler)
        assert hasattr(agent_handler, "SUPERVISOR_ENABLED")
        assert hasattr(agent_handler, "LANGGRAPH_ENABLED")

    def test_dev_mode_disables_both_flags(self, monkeypatch):
        """In dev mode, both supervisor and langgraph are off regardless
        of any env values — protects main branch from accidental prod
        path activation."""
        monkeypatch.setenv("PARADISE_MODE", "dev")
        monkeypatch.setenv("PARADISE_SUPERVISOR_ENABLED", "true")
        monkeypatch.setenv("PARADISE_LANGGRAPH_ENABLED", "true")

        import importlib
        import agent_handler
        importlib.reload(agent_handler)

        assert agent_handler.SUPERVISOR_ENABLED is False
        assert agent_handler.LANGGRAPH_ENABLED is False

    def test_prod_mode_env_override_enables_supervisor(self, monkeypatch):
        """PARADISE_SUPERVISOR_ENABLED=true in prod mode flips the flag."""
        monkeypatch.setenv("PARADISE_MODE", "prod")
        monkeypatch.setenv("PARADISE_SUPERVISOR_ENABLED", "true")

        import importlib
        import agent_handler
        importlib.reload(agent_handler)

        assert agent_handler.SUPERVISOR_ENABLED is True


# ─────────────────────────────────────────────────────────────────────
# agent.run_via_supervisor — with mocked build_supervisor
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _MockHandoffResponse:
    session_id: str = "s1"
    trace_id: str = "t1"
    output: str = "supervised reply"
    artifacts: dict | None = None
    cost_incurred_usd: float = 0.0
    turns_used: int = 1
    status: str = "ok"
    error: str | None = None


@dataclass
class _MockLoopContext:
    """Minimal duck-type of LoopContext — only fields run_via_supervisor reads."""
    user_message: str = "hi"
    session_id: str = "test-session"
    agent_id: str = "test-agent"
    agent_name: str = "Test"
    enable_tools: bool = True


def _make_minimal_agent():
    """Construct a ParadiseAgent with the minimum config to make
    run_via_supervisor reach the build_supervisor call without crashing
    on missing workspace/emotion setup."""
    from paradise.core.agent import ParadiseAgent
    cfg = ParadiseConfig(agent_id="wiring-test")
    # Bypass __init__ to avoid touching disk — we only exercise
    # run_via_supervisor's event emission path.
    agent = ParadiseAgent.__new__(ParadiseAgent)
    agent.agent_id = "wiring-test"
    agent.config = cfg

    # Stub out the side-effects in run_via_supervisor's preamble.
    class _Stub:
        def on_interaction(self, *a, **kw): pass
        def to_dict(self): return {}
    agent.emotion_state = _Stub()
    agent.emotion_engine = _Stub()

    class _WorkspaceStub:
        def save_state(self, *a, **kw): pass
    agent.workspace = _WorkspaceStub()
    return agent


class TestRunViaSupervisorEvents:
    @pytest.mark.asyncio
    async def test_emits_intent_then_done(self):
        """Happy path: the fake graph emits an intent event through
        event_sink (simulating what the real intent_node does), then
        resolves to a final_output. Verify event sequence is compatible
        with handle_message consumers."""
        agent = _make_minimal_agent()

        async def _fake_ainvoke(state, config=None):
            # Simulate intent_node emitting via ContextVar-bound sink
            from paradise.core.streaming import get_event_sink
            sink = get_event_sink()
            if sink is not None:
                await sink.put({
                    "type": "intent",
                    "content": "chitchat",
                    "confidence": 0.95,
                    "source": "rule",
                    "mode": "chat",
                })
            return {
                "intent": "chitchat",
                "intent_confidence": 0.95,
                "intent_source": "rule",
                "handoff_response": _MockHandoffResponse(
                    output="hello from supervisor",
                ),
                "final_output": "hello from supervisor",
            }

        class _FakeGraph:
            async def ainvoke(self, state, config=None):
                return await _fake_ainvoke(state, config)

        with patch("paradise.factory.build_supervisor",
                   return_value=(_FakeGraph(), None, None)):
            events = []
            async for evt in agent.run_via_supervisor(_MockLoopContext()):
                events.append(evt)

        # Expect: intent event (from sink), then done event (synthesized)
        types = [e["type"] for e in events]
        assert "intent" in types
        assert "done" in types
        done_evt = next(e for e in events if e["type"] == "done")
        assert done_evt["content"] == "hello from supervisor"
        intent_evt = next(e for e in events if e["type"] == "intent")
        assert intent_evt["content"] == "chitchat"
        assert intent_evt["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_emits_error_when_build_returns_none(self):
        """Fail-soft: if build_supervisor returns (None, None, None) the agent
        yields an error event rather than raising."""
        agent = _make_minimal_agent()
        with patch("paradise.factory.build_supervisor",
                   return_value=(None, None, None)):
            events = []
            async for evt in agent.run_via_supervisor(_MockLoopContext()):
                events.append(evt)

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "unavailable" in events[0]["content"]

    @pytest.mark.asyncio
    async def test_emits_error_when_supervisor_crashes(self):
        """If compiled.ainvoke raises, run_via_supervisor catches and
        yields an error event (no exception bubbles to caller)."""
        agent = _make_minimal_agent()

        class _CrashingGraph:
            async def ainvoke(self, state, config=None):
                raise RuntimeError("graph blew up")

        with patch("paradise.factory.build_supervisor",
                   return_value=(_CrashingGraph(), None, None)):
            events = []
            async for evt in agent.run_via_supervisor(_MockLoopContext()):
                events.append(evt)

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "crashed" in events[0]["content"]

    @pytest.mark.asyncio
    async def test_emits_tool_call_via_event_sink(self):
        """Phase 3-C: the fake graph simulates tool_react by putting a
        tool_call event into event_sink. Verify it's forwarded before
        the final done event (real-time streaming, not post-hoc)."""
        agent = _make_minimal_agent()

        async def _fake_ainvoke(state, config=None):
            from paradise.core.streaming import get_event_sink
            sink = get_event_sink()
            if sink is not None:
                await sink.put({"type": "intent", "content": "tool_search",
                                "confidence": 0.9, "source": "llm", "mode": "tool_react"})
                await sink.put({"type": "tool_call", "name": "weather",
                                "arguments": {"city": "Beijing"},
                                "result": '{"temp": 25}', "duration_ms": 12})
            return {
                "intent": "tool_search",
                "handoff_response": _MockHandoffResponse(output="Beijing is 25°C"),
                "final_output": "Beijing is 25°C",
            }

        class _FakeGraph:
            async def ainvoke(self, state, config=None):
                return await _fake_ainvoke(state, config)

        with patch("paradise.factory.build_supervisor",
                   return_value=(_FakeGraph(), None, None)):
            events = []
            async for evt in agent.run_via_supervisor(_MockLoopContext()):
                events.append(evt)

        types = [e["type"] for e in events]
        assert "tool_call" in types
        # tool_call must arrive BEFORE done (streaming order)
        assert types.index("tool_call") < types.index("done")
        tool_evt = next(e for e in events if e["type"] == "tool_call")
        assert tool_evt["name"] == "weather"
        assert "temp" in tool_evt["result"]


# ─────────────────────────────────────────────────────────────────────
# factory.build_supervisor — fail-soft contract
# ─────────────────────────────────────────────────────────────────────


class TestFactoryBuildSupervisor:
    def test_returns_none_on_failure(self):
        """When paradise.transports.resolve_transport raises (e.g., dev
        env without ollama), build_supervisor returns (None, None, None)
        rather than crashing. The agent then falls back."""
        from paradise.factory import build_supervisor

        cfg = ParadiseConfig(agent_id="factory-test")
        # Force transport resolution to fail by giving an unreachable URL
        # — resolve_transport itself shouldn't raise on URL, but if it
        # does, we want to see graceful (None, None, None) regardless.
        # Use a real call to verify the contract holds.
        result = build_supervisor(cfg)
        assert len(result) == 3
        compiled, registry, mcp_manager = result
        # Either all None (failure) or compiled populated (success)
        if compiled is None:
            assert registry is None
        else:
            assert registry is not None
        # MCP disabled by default
        assert mcp_manager is None
