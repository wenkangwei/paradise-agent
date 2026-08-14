"""Phase 2.10 — ChatPattern + ToolReactPattern tests.

Run:
    cd server && python -m pytest paradise/tests/test_patterns_basic.py -v

Scope:
  * ChatPattern: build → invoke → HandoffResponse round-trip
  * ChatPattern: transport.chat raising → HANDOFF_ERROR
  * ChatPattern: kwargs construction (provider-specific)
  * ToolReactPattern: LLM returns final answer immediately → exit ok
  * ToolReactPattern: tool_calls → dispatch → loop → final answer
  * ToolReactPattern: stuck in tool loop → HANDOFF_MAX_TURNS
  * ToolReactPattern: transport failure → HANDOFF_ERROR
  * ToolReactPattern: tool dispatch failure → error in scratchpad, loop continues
  * register_basic_patterns: replaces stubs with real patterns

All tests use MOCK transports / tool registries — no live LLM calls.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from paradise.core.handoff import (
    HANDOFF_ERROR,
    HANDOFF_MAX_TURNS,
    HANDOFF_OK,
    HandoffRequest,
    HandoffResponse,
)
from paradise.core.patterns import (
    ChatPattern,
    ToolReactPattern,
    register_all,
    register_basic_patterns,
)
from paradise.core.registry import SubgraphRegistry


# ─────────────────────────────────────────────────────────────────────
# Mock fixtures
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _MockUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 20
    total_tokens: int = 30


@dataclass
class _MockToolCall:
    """Mirror of paradise.transports.types.ToolCall — only fields used by
    ToolReactPattern are exercised here."""
    id: str | None
    name: str
    arguments: str
    provider_data: dict | None = None

    # ToolCall has these properties — keep the mock API-compatible.
    @property
    def type(self) -> str: return "function"

    @property
    def function(self) -> "_MockToolCall": return self


@dataclass
class _MockResponse:
    """Stand-in for transport.chat() return value."""
    content: str | None
    tool_calls: list[_MockToolCall] | None = None
    finish_reason: str = "stop"
    usage: _MockUsage | None = None


class _ScriptedTransport:
    """Transport mock that returns responses from a scripted queue.

    Each call to .chat() pops the next response. Tests pre-load the
    queue with the sequence of LLM responses they want the pattern to
    see. Raises if the queue runs out (unexpected extra call).
    """

    api_mode = "openai_compat"  # default; tests can override

    def __init__(self, responses: list[_MockResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []   # captured kwargs per call

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError(
                f"ScriptedTransport queue empty — unexpected .chat() call. "
                f"Previous calls: {len(self.calls)}"
            )
        resp = self._responses.pop(0)
        # Simulate failure if the queued response is an exception
        if isinstance(resp, Exception):
            raise resp
        return resp


class _MockToolRegistry:
    """Minimal ToolRegistry mock with the API surface ToolReactPattern uses.

    Real ToolRegistry.dispatch_async returns a JSON string; this mock
    returns whatever the test scripts (already a JSON string).
    """

    def __init__(self, results: dict[str, str] | None = None,
                 fail_with: dict[str, Exception] | None = None):
        self._results = results or {}
        self._fail_with = fail_with or {}
        self.dispatched: list[tuple[str, dict]] = []

    def _snapshot_entries(self):
        return []  # no tools — tool_react pattern degrades gracefully

    def get_definitions(self, names):
        return []

    async def dispatch_async(self, name: str, args: dict, **kwargs) -> str:
        self.dispatched.append((name, args))
        if name in self._fail_with:
            raise self._fail_with[name]
        return self._results.get(name, json.dumps({"status": "ok"}))


def _req(message: str = "hello", trace_id: str = "t1") -> HandoffRequest:
    return HandoffRequest(
        session_id="s1",
        user_id="u1",
        trace_id=trace_id,
        message=message,
    )


# ─────────────────────────────────────────────────────────────────────
# ChatPattern
# ─────────────────────────────────────────────────────────────────────


class TestChatPatternBuild:
    def test_build_returns_compiled_graph(self):
        p = ChatPattern(transport=_ScriptedTransport([_MockResponse(content="hi")]))
        graph = p.build(SubgraphRegistry())
        assert graph is not None
        # compiled LangGraph runnable has .ainvoke
        assert hasattr(graph, "ainvoke")

    def test_availability_true(self):
        """ChatPattern (with injected transport) is always available."""
        p = ChatPattern(transport=_ScriptedTransport([]))
        assert p.availability() is True

    def test_category_basic(self):
        assert ChatPattern.category == "basic"


class TestChatPatternInvoke:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        transport = _ScriptedTransport([_MockResponse(content="hello back")])
        p = ChatPattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req("hi")})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.output == "hello back"
        assert resp.session_id == "s1"
        assert resp.trace_id == "t1"
        assert resp.error is None

    @pytest.mark.asyncio
    async def test_transport_kwargs_propagate_model(self):
        """Ensure model/system_prompt/messages are passed through."""
        transport = _ScriptedTransport([_MockResponse(content="ok")])
        p = ChatPattern(
            transport=transport,
            llm_config=_LLMConfigStub(model="test-model", api_url="http://x"),
        )
        graph = p.build(SubgraphRegistry())
        await graph.ainvoke({"handoff": _req("hi")})

        kwargs = transport.calls[0]
        assert kwargs["model"] == "test-model"
        assert "concise" in kwargs["system_prompt"].lower()
        assert kwargs["messages"] == [{"role": "user", "content": "hi"}]

    @pytest.mark.asyncio
    async def test_transport_failure_yields_error_response(self):
        transport = _ScriptedTransport([RuntimeError("connection refused")])
        p = ChatPattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req("hi")})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_ERROR
        assert resp.output == ""
        assert "connection refused" in (resp.error or "")
        # Even on failure, session/trace echo must be correct
        assert resp.session_id == "s1"

    @pytest.mark.asyncio
    async def test_content_extraction_falls_back_to_str(self):
        """When result has no .content attr, str(result) is used."""
        @dataclass
        class _NoContent:
            text: str = "fallback"
            def __str__(self): return self.text

        transport = _ScriptedTransport([_NoContent()])
        # _MockResponse used just to satisfy the type — replace .content
        # by injecting a non-_MockResponse object
        transport._responses = [_NoContent()]
        p = ChatPattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})
        assert result["handoff_response"].output == "fallback"


# ─────────────────────────────────────────────────────────────────────
# ToolReactPattern
# ─────────────────────────────────────────────────────────────────────


class TestToolReactPatternBuild:
    def test_build_returns_compiled_graph(self):
        p = ToolReactPattern(
            transport=_ScriptedTransport([_MockResponse(content="done")]),
            tool_registry=_MockToolRegistry(),
        )
        graph = p.build(SubgraphRegistry())
        assert graph is not None
        assert hasattr(graph, "ainvoke")

    def test_availability_true(self):
        p = ToolReactPattern(
            transport=_ScriptedTransport([]),
            tool_registry=_MockToolRegistry(),
        )
        assert p.availability() is True

    def test_max_turns_default_3(self):
        assert ToolReactPattern.max_turns == 3


class TestToolReactPatternHappyPath:
    @pytest.mark.asyncio
    async def test_immediate_final_answer(self):
        """LLM has enough info, returns no tool_calls → exit on iter 1."""
        transport = _ScriptedTransport([_MockResponse(
            content="The answer is 42",
            tool_calls=None,
        )])
        p = ToolReactPattern(transport=transport, tool_registry=_MockToolRegistry())
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req("what is the answer?")})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.output == "The answer is 42"
        assert resp.turns_used == 1
        # Tool registry should never have been called
        assert isinstance(p._resolve_tool_registry_safe(), _MockToolRegistry)
        # Tools=0 dispatched
        tool_reg = p._resolve_tool_registry_safe()
        assert tool_reg.dispatched == []

    @pytest.mark.asyncio
    async def test_one_tool_then_final_answer(self):
        """LLM calls weather.query → result → LLM produces final answer."""
        tool_reg = _MockToolRegistry(results={
            "weather.query": json.dumps({"temp": 22, "city": "Beijing"}),
        })
        transport = _ScriptedTransport([
            _MockResponse(
                content="let me check",
                tool_calls=[_MockToolCall(id="c1", name="weather.query",
                                          arguments=json.dumps({"city": "Beijing"}))],
            ),
            _MockResponse(content="Beijing is 22°C"),  # after tool result
        ])
        p = ToolReactPattern(transport=transport, tool_registry=tool_reg)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req("weather in Beijing?")})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert "22" in resp.output
        assert resp.turns_used == 2  # one tool round + one final
        # Verify dispatch happened with correct args
        assert len(tool_reg.dispatched) == 1
        name, args = tool_reg.dispatched[0]
        assert name == "weather.query"
        assert args == {"city": "Beijing"}


class TestToolReactPatternMaxTurns:
    @pytest.mark.asyncio
    async def test_stuck_in_tool_loop_hits_max_turns(self):
        """LLM keeps calling tools forever → exit with HANDOFF_MAX_TURNS."""
        tool_reg = _MockToolRegistry(results={
            "search": json.dumps({"hits": 0}),
        })
        # 5 responses, all tool calls — exceeds max_turns=3
        looping_call = _MockToolCall(id="c", name="search", arguments="{}")
        transport = _ScriptedTransport([
            _MockResponse(content="searching", tool_calls=[looping_call]),
            _MockResponse(content="searching", tool_calls=[looping_call]),
            _MockResponse(content="searching", tool_calls=[looping_call]),
            _MockResponse(content="searching", tool_calls=[looping_call]),
            _MockResponse(content="searching", tool_calls=[looping_call]),
        ])
        p = ToolReactPattern(transport=transport, tool_registry=tool_reg)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req("find X")})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_MAX_TURNS
        assert resp.turns_used == 3  # capped at max_turns
        assert "max_turns" in (resp.error or "")


class TestToolReactPatternErrors:
    @pytest.mark.asyncio
    async def test_transport_failure_yields_error(self):
        transport = _ScriptedTransport([RuntimeError("LLM timeout")])
        p = ToolReactPattern(transport=transport, tool_registry=_MockToolRegistry())
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req("hi")})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_ERROR
        assert "LLM timeout" in (resp.error or "")

    @pytest.mark.asyncio
    async def test_tool_dispatch_failure_continues_loop(self):
        """When a tool raises, the error becomes a tool message and the
        LLM gets a chance to recover on the next iteration."""
        tool_reg = _MockToolRegistry(
            fail_with={"broken.tool": RuntimeError("network error")}
        )
        transport = _ScriptedTransport([
            _MockResponse(
                content="trying",
                tool_calls=[_MockToolCall(id="c1", name="broken.tool",
                                          arguments="{}")],
            ),
            _MockResponse(content="Sorry, I couldn't reach the service."),
        ])
        p = ToolReactPattern(transport=transport, tool_registry=tool_reg)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req("use broken tool")})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.turns_used == 2
        # Verify the tool was actually dispatched (and failed)
        assert len(tool_reg.dispatched) == 1


# ─────────────────────────────────────────────────────────────────────
# register_basic_patterns — supervisor-side wiring helper
# ─────────────────────────────────────────────────────────────────────


class TestRegisterBasicPatterns:
    def test_replaces_basic_pattern_stubs(self):
        """After register_basic_patterns, the 4 basic patterns (chat /
        tool_react / rag / plan_execute) are real (availability=True),
        while 7 multi-agent patterns remain stubs.

        Phase 2.11 expanded this from 2 → 4 basic patterns.
        """
        r = SubgraphRegistry()
        register_all(r)
        # Pre: all 11 unavailable
        assert sorted(r.list_modes()) == []

        register_basic_patterns(
            r,
            transport=_ScriptedTransport([_MockResponse(content="ok")]),
            tool_registry=_MockToolRegistry(),
        )

        # Post: 4 basic patterns available, rest still stubs
        modes = sorted(r.list_modes())
        assert modes == ["chat", "plan_execute", "rag", "tool_react"]

    def test_multi_agent_patterns_remain_stubs(self):
        """The 7 multi-agent patterns must remain stubs after Phase 2.11."""
        r = SubgraphRegistry()
        register_all(r)
        register_basic_patterns(
            r,
            transport=_ScriptedTransport([_MockResponse(content="ok")]),
            tool_registry=_MockToolRegistry(),
        )
        all_modes = sorted(r.list_modes(include_unavailable=True))
        assert len(all_modes) == 11
        # 4 real basic + 7 stub multi-agent
        assert sorted(r.list_modes()) == ["chat", "plan_execute", "rag", "tool_react"]
        # The 7 unavailable:
        unavailable = sorted(
            m for m in all_modes if m not in r.list_modes()
        )
        assert unavailable == [
            "agent_team", "chain_of_expert", "debate",
            "guardrail", "hitl", "map_reduce", "reflection",
        ]

    def test_requires_register_all_first(self):
        """If caller forgets register_all(), we fail loudly."""
        r = SubgraphRegistry()
        with pytest.raises(ValueError, match="register_all"):
            register_basic_patterns(r, transport=object())

    def test_registered_chat_pattern_invokable(self):
        """End-to-end: registry.get('chat').compiled is a real graph
        that can be ainvoked."""
        r = SubgraphRegistry()
        register_all(r)
        register_basic_patterns(
            r,
            transport=_ScriptedTransport([_MockResponse(content="hi")]),
        )
        spec = r.get("chat")
        assert spec.compiled is not None

        async def _run():
            return await spec.compiled.ainvoke({"handoff": _req("hello")})

        result = asyncio.run(_run())
        assert result["handoff_response"].output == "hi"

    def test_idempotent_after_double_call_raises(self):
        """Second call on a registry with real patterns already installed
        fails — caller must reset the registry."""
        r = SubgraphRegistry()
        register_all(r)
        register_basic_patterns(
            r,
            transport=_ScriptedTransport([_MockResponse(content="ok")]),
            tool_registry=_MockToolRegistry(),
        )
        with pytest.raises(ValueError, match="already a real pattern"):
            register_basic_patterns(
                r,
                transport=_ScriptedTransport([_MockResponse(content="ok")]),
                tool_registry=_MockToolRegistry(),
            )


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _LLMConfigStub:
    """Minimal LLMConfig duck-type for chat/tool_react kwargs construction.
    Real LLMConfig has many more fields; pattern only reads these."""
    model: str = "test-model"
    api_url: str = "http://localhost:11434"
    api_key: str = ""
    max_tokens: int = 512
    provider: str = "ollama"
    temperature: float = 0.7


def _patch_resolve_safe():
    """Add a helper to ToolReactPattern for test-only access to the
    resolved tool registry (so tests can assert on dispatch counts).

    Done via monkey-patch rather than exposing internals on the public
    API."""
    def _safe(self):
        return self._tool_registry
    ToolReactPattern._resolve_tool_registry_safe = _safe


_patch_resolve_safe()
