"""Phase 2.9 — supervisor graph integration tests.

Run:
    cd server && python -m pytest paradise/tests/test_supervisor.py -v

Scope:
  * End-to-end: intent → mode → handoff → reflect → cleanup → final_output
  * chitchat intent → chat subgraph → ok
  * tool intent → tool_react subgraph → ok
  * unknown intent → fallback to chat
  * intent classifier raises → fallback to chat
  * subgraph not registered (mode=rag) → fallback to chat
  * subgraph returns HANDOFF_ERROR → reflect retries once in chat mode
  * retry exhausted → user-friendly error message
  * intent classifier None → defaults to chat

All tests use mock transport + mock intent classifier (no live LLM).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

import pytest

from paradise.core.handoff import (
    HANDOFF_DEPTH_CAPPED,
    HANDOFF_ERROR,
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
from paradise.core.supervisor import build_supervisor_graph, invoke_subgraph


# ─────────────────────────────────────────────────────────────────────
# Mocks
# ─────────────────────────────────────────────────────────────────────


class _Intent(str, Enum):
    """Mirror of paradise.core.intent.base.Intent for test fixtures."""
    CHITCHAT = "chitchat"
    TOOL_SEARCH = "tool_search"
    UNKNOWN = "unknown"
    UNSAFE = "unsafe"


@dataclass
class _IntentResult:
    """Mirror of IntentResult — only fields supervisor uses."""
    intent: _Intent
    confidence: float
    source: str


class _ScriptedClassifier:
    """Returns canned IntentResult values from a queue."""
    def __init__(self, results: list[_IntentResult]):
        self._results = list(results)
        self.calls: list[str] = []

    async def classify(self, message: str) -> _IntentResult:
        self.calls.append(message)
        if not self._results:
            return _IntentResult(_Intent.UNKNOWN, 0.0, "exhausted")
        return self._results.pop(0)


class _FailingClassifier:
    """Always raises — exercises supervisor's fail-soft path."""
    async def classify(self, message: str) -> _IntentResult:
        raise RuntimeError("classifier offline")


@dataclass
class _ModeResult:
    """Mirror of ModeJudgeResult — only fields supervisor uses."""
    mode: str
    confidence: float
    source: str = "llmjudge"


class _ScriptedModeJudge:
    """Returns canned ModeJudgeResult values from a queue.

    A None entry in the queue simulates the LLM returning None
    (network error / invalid JSON / hallucination guard).
    """
    def __init__(self, results: list):
        self._results = list(results)
        self.calls: list[str] = []

    async def classify(self, message: str):
        self.calls.append(message)
        if not self._results:
            return None
        return self._results.pop(0)


class _CrashingModeJudge:
    """Always raises — exercises supervisor's gather(return_exceptions) path."""
    async def classify(self, message: str):
        raise RuntimeError("judge crashed")


@dataclass
class _MockUsage:
    prompt_tokens: int = 5
    completion_tokens: int = 10
    total_tokens: int = 15


@dataclass
class _MockToolCall:
    id: str | None
    name: str
    arguments: str
    provider_data: dict | None = None

    @property
    def type(self): return "function"

    @property
    def function(self): return self


@dataclass
class _MockResponse:
    content: str | None
    tool_calls: list[_MockToolCall] | None = None
    finish_reason: str = "stop"
    usage: _MockUsage | None = None


class _ScriptedTransport:
    """Mock transport that pops responses from a queue."""
    api_mode = "openai_compat"

    def __init__(self, responses: list[_MockResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("ScriptedTransport queue empty")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class _MockToolRegistry:
    """Minimal tool registry for tool_react tests."""
    def __init__(self, results: dict[str, str] | None = None):
        self._results = results or {}
        self.dispatched: list[tuple[str, dict]] = []

    def _snapshot_entries(self):
        return []

    def get_definitions(self, names):
        return []

    async def dispatch_async(self, name: str, args: dict, **kwargs) -> str:
        self.dispatched.append((name, args))
        return self._results.get(name, json.dumps({"status": "ok"}))


# ─────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────


def _build_registry(transport_responses: list[_MockResponse],
                    tool_results: dict | None = None) -> tuple[SubgraphRegistry, _ScriptedTransport]:
    """Build a registry with real ChatPattern + ToolReactPattern,
    sharing a single scripted transport. Returns (registry, transport)
    so tests can inspect transport.calls after the run."""
    transport = _ScriptedTransport(transport_responses)
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(
        registry,
        transport=transport,
        tool_registry=_MockToolRegistry(results=tool_results or {}),
    )
    return registry, transport


def _initial_state(message: str, trace_id: str = "trace-1") -> dict:
    """Standard initial supervisor state for tests."""
    return {
        "user_message": message,
        "session_id": "sess-1",
        "trace_id": trace_id,
        "user_id": "user-1",
        "user_tier": "free",
    }


def _config(trace_id: str = "trace-1") -> dict:
    """LangGraph thread config."""
    return {"configurable": {"thread_id": trace_id}}


# ─────────────────────────────────────────────────────────────────────
# Happy paths
# ─────────────────────────────────────────────────────────────────────


class TestSupervisorHappyPath:
    @pytest.mark.asyncio
    async def test_chitchat_routes_to_chat(self):
        """chitchat intent → mode=chat → ChatPattern → final output."""
        registry, transport = _build_registry([
            _MockResponse(content="hi there!"),
        ])
        clf = _ScriptedClassifier([_IntentResult(_Intent.CHITCHAT, 0.95, "rule")])
        graph = build_supervisor_graph(registry, intent_classifier=clf)

        result = await graph.ainvoke(_initial_state("你好"), config=_config())

        assert result["final_output"] == "hi there!"
        assert result["mode"] == "chat"
        assert result["intent"] == "chitchat"
        # Only one LLM call (chat subgraph is single-shot)
        assert len(transport.calls) == 1
        # Verify classifier was consulted
        assert clf.calls == ["你好"]

    @pytest.mark.asyncio
    async def test_tool_intent_routes_to_tool_react_immediate_answer(self):
        """tool_search intent → mode=tool_react → LLM answers without
        calling any tool → single LLM call."""
        registry, transport = _build_registry([
            _MockResponse(content="The answer is 42", tool_calls=None),
        ])
        clf = _ScriptedClassifier([_IntentResult(_Intent.TOOL_SEARCH, 0.85, "llm")])
        graph = build_supervisor_graph(registry, intent_classifier=clf)

        result = await graph.ainvoke(
            _initial_state("what is the answer?"),
            config=_config(),
        )

        assert result["final_output"] == "The answer is 42"
        assert result["mode"] == "tool_react"
        assert result["handoff_response"].turns_used == 1

    @pytest.mark.asyncio
    async def test_tool_intent_with_tool_dispatch(self):
        """tool_react subgraph: LLM calls weather → result → final answer."""
        registry, transport = _build_registry(
            transport_responses=[
                _MockResponse(
                    content="checking",
                    tool_calls=[_MockToolCall(
                        id="c1", name="weather",
                        arguments=json.dumps({"city": "Shanghai"}),
                    )],
                ),
                _MockResponse(content="Shanghai is 25°C"),
            ],
            tool_results={"weather": json.dumps({"temp": 25})},
        )
        clf = _ScriptedClassifier([_IntentResult(_Intent.TOOL_SEARCH, 0.9, "llm")])
        graph = build_supervisor_graph(registry, intent_classifier=clf)

        result = await graph.ainvoke(
            _initial_state("weather in Shanghai?"),
            config=_config(),
        )

        assert "25" in result["final_output"]
        assert result["handoff_response"].turns_used == 2


# ─────────────────────────────────────────────────────────────────────
# Fail-soft paths
# ─────────────────────────────────────────────────────────────────────


class TestSupervisorFallbacks:
    @pytest.mark.asyncio
    async def test_unknown_intent_routes_to_tool_react(self):
        """unknown intent → tool_react (gives LLM the option to use tools
        rather than defaulting to chat where it would hallucinate)."""
        registry, transport = _build_registry([
            _MockResponse(content="default reply"),
        ])
        clf = _ScriptedClassifier([_IntentResult(_Intent.UNKNOWN, 0.0, "none")])
        graph = build_supervisor_graph(registry, intent_classifier=clf)

        result = await graph.ainvoke(_initial_state("???"), config=_config())

        assert result["final_output"] == "default reply"
        assert result["mode"] == "tool_react"

    @pytest.mark.asyncio
    async def test_classifier_exception_falls_back_to_chat(self):
        """Classifier raising → intent_node catches → mode=chat."""
        registry, transport = _build_registry([
            _MockResponse(content="safe reply"),
        ])
        graph = build_supervisor_graph(registry, intent_classifier=_FailingClassifier())

        result = await graph.ainvoke(_initial_state("hi"), config=_config())

        assert result["final_output"] == "safe reply"
        assert result["intent"] == "unknown"
        assert result["intent_source"] == "error"

    @pytest.mark.asyncio
    async def test_no_classifier_defaults_to_chat(self):
        """When intent_classifier=None (Phase 2.9 minimal wiring),
        every request goes to chat. Useful for smoke testing."""
        registry, transport = _build_registry([
            _MockResponse(content="hello"),
        ])
        graph = build_supervisor_graph(registry, intent_classifier=None)

        result = await graph.ainvoke(_initial_state("anything"), config=_config())

        assert result["final_output"] == "hello"
        assert result["mode"] == "chat"
        assert result["intent_source"] == "none"

    @pytest.mark.asyncio
    async def test_mode_not_registered_falls_back_to_chat(self):
        """Intent maps to mode=rag, but rag is still a stub (unavailable).
        handoff_node falls back to chat."""
        # Use a classifier that returns an intent mapped to "rag" via
        # the _INTENT_TO_MODE table — but rag isn't in the default
        # mapping, so we go via knowledge_query → tool_react instead.
        # To force this test, we monkey-patch the mapping.
        from paradise.core import supervisor as sup_mod
        original = sup_mod._INTENT_TO_MODE.copy()
        sup_mod._INTENT_TO_MODE["knowledge_query"] = "rag"  # rag is stub
        try:
            registry, transport = _build_registry([
                _MockResponse(content="fallback ok"),
            ])
            clf = _ScriptedClassifier([
                _IntentResult(_Intent.TOOL_SEARCH, 0.8, "llm"),
            ])
            # Force TOOL_SEARCH → "rag" too for this test
            sup_mod._INTENT_TO_MODE["tool_search"] = "rag"
            graph = build_supervisor_graph(registry, intent_classifier=clf)

            result = await graph.ainvoke(
                _initial_state("search docs"),
                config=_config(),
            )

            # rag unavailable → fallback to chat → "fallback ok"
            assert result["final_output"] == "fallback ok"
        finally:
            sup_mod._INTENT_TO_MODE.clear()
            sup_mod._INTENT_TO_MODE.update(original)


# ─────────────────────────────────────────────────────────────────────
# Retry logic
# ─────────────────────────────────────────────────────────────────────


class TestSupervisorRetry:
    @pytest.mark.asyncio
    async def test_subgraph_error_triggers_chat_retry(self):
        """tool_react subgraph fails (transport raises) → reflect_node
        schedules retry in chat mode → chat succeeds."""
        # First response raises (tool_react transport call fails)
        # Second response is chat's successful reply
        registry, transport = _build_registry([
            RuntimeError("tool_react transport dead"),  # used by tool_react
            _MockResponse(content="recovered via chat"),
        ])
        clf = _ScriptedClassifier([_IntentResult(_Intent.TOOL_SEARCH, 0.9, "llm")])
        graph = build_supervisor_graph(registry, intent_classifier=clf)

        result = await graph.ainvoke(
            _initial_state("complex task"),
            config=_config(),
        )

        assert result["final_output"] == "recovered via chat"
        assert result["retry_count"] == 1
        # transport called twice (once for failed tool_react, once for chat)
        assert len(transport.calls) == 2

    @pytest.mark.asyncio
    async def test_retry_exhausted_surfaces_friendly_message(self):
        """Both tool_react AND chat fail → user sees graceful error."""
        registry, transport = _build_registry([
            RuntimeError("tool_react dead"),
            RuntimeError("chat also dead"),
        ])
        clf = _ScriptedClassifier([_IntentResult(_Intent.TOOL_SEARCH, 0.9, "llm")])
        graph = build_supervisor_graph(registry, intent_classifier=clf)

        result = await graph.ainvoke(
            _initial_state("doomed"),
            config=_config(),
        )

        assert "Sorry" in result["final_output"] or "issue" in result["final_output"]
        assert result["handoff_response"].status == HANDOFF_ERROR

    @pytest.mark.asyncio
    async def test_no_retry_when_already_in_chat_mode(self):
        """If chat itself fails on first attempt, no retry (would loop)."""
        registry, transport = _build_registry([
            RuntimeError("chat dead"),
        ])
        clf = _ScriptedClassifier([_IntentResult(_Intent.CHITCHAT, 0.95, "rule")])
        graph = build_supervisor_graph(registry, intent_classifier=clf)

        result = await graph.ainvoke(_initial_state("hi"), config=_config())

        # Should NOT have retried — chat is already the default
        assert result["retry_count"] == 0
        assert result["handoff_response"].status == HANDOFF_ERROR
        # Only one transport call (no retry)
        assert len(transport.calls) == 1


# ─────────────────────────────────────────────────────────────────────
# Contract: HandoffResponse propagation
# ─────────────────────────────────────────────────────────────────────


class TestSupervisorHandoffContract:
    @pytest.mark.asyncio
    async def test_handoff_request_echoes_session_id(self):
        """HandoffRequest carries session_id/trace_id from supervisor
        state — subgraph MUST see these."""
        registry, transport = _build_registry([
            _MockResponse(content="ok"),
        ])
        clf = _ScriptedClassifier([_IntentResult(_Intent.CHITCHAT, 0.95, "rule")])
        graph = build_supervisor_graph(registry, intent_classifier=clf)

        result = await graph.ainvoke(_initial_state("hi", trace_id="abc-123"),
                                     config=_config("abc-123"))

        req = result["handoff_request"]
        resp = result["handoff_response"]
        assert req.session_id == "sess-1"
        assert req.trace_id == "abc-123"
        assert req.depth == 1
        assert resp.session_id == "sess-1"
        assert resp.trace_id == "abc-123"

    @pytest.mark.asyncio
    async def test_handoff_request_depth_is_one(self):
        """Supervisor is depth=0; subgraphs are depth=1. Nesting
        (depth=2) only happens if subgraphs themselves spawn further
        subgraphs (Phase 2.12)."""
        registry, transport = _build_registry([
            _MockResponse(content="ok"),
        ])
        graph = build_supervisor_graph(registry, intent_classifier=None)
        result = await graph.ainvoke(_initial_state("hi"), config=_config())
        assert result["handoff_request"].depth == 1


# ─────────────────────────────────────────────────────────────────────
# Phase 2.8: parallel mode_judge fusion
# ─────────────────────────────────────────────────────────────────────


class TestSupervisorParallelModeJudge:
    """intent_node runs intent classifier + mode_judge in parallel and
    fuses results. Policy:
      * mode_judge conf > 0.7 → mode_judge wins (intent_source gets
        "+llmjudge" suffix for observability).
      * else → intent → mode mapping (Phase 2.9 behavior).
      * mode_judge None / raising → intent-only routing.
    """

    @pytest.mark.asyncio
    async def test_mode_judge_does_not_override_intent(self):
        """ModeJudge is observed for logging but never overrides intent
        mapping. Intent tool_search → tool_react wins even when judge
        says chat with 0.9 confidence."""
        registry, transport = _build_registry([
            _MockResponse(content="tool reply", tool_calls=None),
        ])
        clf = _ScriptedClassifier([
            _IntentResult(_Intent.TOOL_SEARCH, 0.7, "llm"),
        ])
        judge = _ScriptedModeJudge([_ModeResult("chat", 0.9)])
        graph = build_supervisor_graph(
            registry, intent_classifier=clf, mode_judge=judge,
        )

        result = await graph.ainvoke(_initial_state("hi"), config=_config())

        # Intent mapping wins — judge no longer overrides
        assert result["mode"] == "tool_react"
        assert result["final_output"] == "tool reply"
        assert result["intent_source"] == "llm"
        # Both classifiers were consulted in parallel
        assert clf.calls == ["hi"]
        assert judge.calls == ["hi"]

    @pytest.mark.asyncio
    async def test_mode_judge_low_conf_falls_back_to_intent(self):
        """mode_judge conf < 0.7 → intent mapping wins."""
        registry, transport = _build_registry([
            _MockResponse(content="tool reply", tool_calls=None),
        ])
        clf = _ScriptedClassifier([
            _IntentResult(_Intent.TOOL_SEARCH, 0.9, "llm"),
        ])
        # Low-confidence mode_judge — should NOT override
        judge = _ScriptedModeJudge([_ModeResult("chat", 0.4)])
        graph = build_supervisor_graph(
            registry, intent_classifier=clf, mode_judge=judge,
        )

        result = await graph.ainvoke(_initial_state("hi"), config=_config())

        # intent=tool_search → tool_react wins despite judge's chat pick
        assert result["mode"] == "tool_react"
        assert result["final_output"] == "tool reply"
        # intent_source should NOT include llmjudge suffix
        assert "llmjudge" not in result["intent_source"]
        assert result["intent_source"] == "llm"

    @pytest.mark.asyncio
    async def test_mode_judge_observed_but_not_applied(self):
        """intent_classifier=None, mode_judge provided → judge is
        consulted for observability but does NOT drive routing.
        Intent stays "unknown" → maps to tool_react."""
        registry, transport = _build_registry([
            _MockResponse(content="ok"),
        ])
        judge = _ScriptedModeJudge([_ModeResult("chat", 0.9)])
        graph = build_supervisor_graph(
            registry, intent_classifier=None, mode_judge=judge,
        )

        result = await graph.ainvoke(_initial_state("hi"), config=_config())

        # unknown → tool_react (not chat, not judge's pick)
        assert result["mode"] == "tool_react"
        assert result["final_output"] == "ok"
        assert result["intent"] == "unknown"
        assert result["intent_source"] == "none"
        assert judge.calls == ["hi"]

    @pytest.mark.asyncio
    async def test_mode_judge_returns_none_falls_back_to_intent(self):
        """mode_judge.classify returns None (LLM down) → intent-only
        routing. Phase 2.9 intent_source contract preserved."""
        registry, transport = _build_registry([
            _MockResponse(content="ok"),
        ])
        clf = _ScriptedClassifier([
            _IntentResult(_Intent.CHITCHAT, 0.95, "rule"),
        ])
        # None in queue simulates LLM failure
        judge = _ScriptedModeJudge([None])
        graph = build_supervisor_graph(
            registry, intent_classifier=clf, mode_judge=judge,
        )

        result = await graph.ainvoke(_initial_state("hi"), config=_config())

        assert result["mode"] == "chat"
        assert result["intent_source"] == "rule"
        assert "llmjudge" not in result["intent_source"]

    @pytest.mark.asyncio
    async def test_mode_judge_raises_caught(self):
        """mode_judge.classify raises → asyncio.gather(return_exceptions)
        catches it → intent-only routing used. Verifies the
        return_exceptions=True path in intent_node."""
        registry, transport = _build_registry([
            _MockResponse(content="ok"),
        ])
        clf = _ScriptedClassifier([
            _IntentResult(_Intent.CHITCHAT, 0.95, "rule"),
        ])
        graph = build_supervisor_graph(
            registry, intent_classifier=clf,
            mode_judge=_CrashingModeJudge(),
        )

        result = await graph.ainvoke(_initial_state("hi"), config=_config())

        assert result["mode"] == "chat"
        assert result["intent"] == "chitchat"
        assert result["intent_source"] == "rule"

    @pytest.mark.asyncio
    async def test_both_none_preserves_phase_2_9_contract(self):
        """intent_classifier=None AND mode_judge=None → mode=chat with
        intent_source="none". This is the Phase 2.9 wiring baseline —
        the Phase 2.8 fan-out must not regress it."""
        registry, transport = _build_registry([_MockResponse(content="ok")])
        graph = build_supervisor_graph(registry)  # both default None

        result = await graph.ainvoke(_initial_state("hi"), config=_config())

        assert result["mode"] == "chat"
        assert result["intent"] == "unknown"
        assert result["intent_source"] == "none"

    @pytest.mark.asyncio
    async def test_intent_classifier_raises_with_mode_judge(self):
        """Intent classifier raises → intent=unknown → tool_react.
        ModeJudge is consulted but does not drive routing."""
        registry, transport = _build_registry([
            _MockResponse(content="ok"),
        ])
        judge = _ScriptedModeJudge([_ModeResult("chat", 0.9)])
        graph = build_supervisor_graph(
            registry, intent_classifier=_FailingClassifier(),
            mode_judge=judge,
        )

        result = await graph.ainvoke(_initial_state("hi"), config=_config())

        # Classifier failed → unknown → tool_react (judge doesn't override)
        assert result["mode"] == "tool_react"
        assert result["final_output"] == "ok"
        assert result["intent"] == "unknown"
        assert result["intent_source"] == "error"

    @pytest.mark.asyncio
    async def test_empty_message_short_circuits_before_parallel(self):
        """Empty user_message → immediate chat fallback, no classifier
        or judge invocation. Saves a redundant LLM call."""
        registry, transport = _build_registry([_MockResponse(content="ok")])
        clf = _ScriptedClassifier([])
        judge = _ScriptedModeJudge([])
        graph = build_supervisor_graph(
            registry, intent_classifier=clf, mode_judge=judge,
        )

        result = await graph.ainvoke(
            {"user_message": "", "session_id": "s", "trace_id": "t"},
            config=_config(),
        )

        assert result["mode"] == "chat"
        assert result["intent_source"] == "none"
        # Neither was called
        assert clf.calls == []
        assert judge.calls == []


# ─────────────────────────────────────────────────────────────────────
# Phase 2.12: depth cap + nested subgraph
# ─────────────────────────────────────────────────────────────────────


class TestSupervisorDepthCap:
    """invoke_subgraph enforces _MAX_DEPTH=2 to prevent runaway nested
    calls. The cap is checked FIRST, before any subgraph resolution, so
    a depth=3 request fails fast without invoking the target subgraph.
    """

    @pytest.mark.asyncio
    async def test_depth_within_cap_passes_through(self):
        """depth=1 (supervisor → top-level subgraph) → normal invocation."""
        registry, _ = _build_registry([_MockResponse(content="ok")])
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t",
            message="hi", depth=1,
        )
        resp = await invoke_subgraph(registry, "chat", req)
        assert resp.status == HANDOFF_OK
        assert resp.output == "ok"

    @pytest.mark.asyncio
    async def test_depth_at_max_allowed(self):
        """depth=2 (e.g., plan_execute → tool_react) is the deepest allowed."""
        registry, _ = _build_registry([_MockResponse(content="nested ok")])
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t",
            message="hi", depth=2,
        )
        resp = await invoke_subgraph(registry, "chat", req)
        assert resp.status == HANDOFF_OK
        assert resp.output == "nested ok"

    @pytest.mark.asyncio
    async def test_depth_over_cap_returns_depth_capped(self):
        """depth=3 → HANDOFF_DEPTH_CAPPED, no subgraph invocation.

        Critical: the cap must trigger BEFORE the subgraph runs, so a
        runaway chain doesn't burn budget cascading through patterns.
        """
        registry, transport = _build_registry([_MockResponse(content="should not run")])
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t",
            message="hi", depth=3,
        )
        resp = await invoke_subgraph(registry, "chat", req)

        assert resp.status == HANDOFF_DEPTH_CAPPED
        assert resp.output == ""
        assert "depth 3" in (resp.error or "")
        # Verify the subgraph was NOT invoked — transport.calls stays empty
        assert len(transport.calls) == 0

    @pytest.mark.asyncio
    async def test_depth_capped_for_unknown_mode_too(self):
        """Even if the mode is bogus, depth check fires first.

        This matters because building/looking up a stub for an unknown
        mode could have side effects. The early-return avoids them.
        """
        registry, _ = _build_registry([])
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t",
            message="hi", depth=99,
        )
        resp = await invoke_subgraph(registry, "totally_unknown_mode", req)
        assert resp.status == HANDOFF_DEPTH_CAPPED

    @pytest.mark.asyncio
    async def test_depth_capped_response_carries_request_trace(self):
        """depth_capped response must echo session_id/trace_id from req
        so observability can correlate the rejected call."""
        registry, _ = _build_registry([])
        req = HandoffRequest(
            session_id="sess-abc", user_id="u", trace_id="trace-xyz",
            message="hi", depth=5,
        )
        resp = await invoke_subgraph(registry, "chat", req)
        assert resp.session_id == "sess-abc"
        assert resp.trace_id == "trace-xyz"

    @pytest.mark.asyncio
    async def test_normal_supervisor_path_unaffected_by_cap(self):
        """End-to-end: a normal chat request via the supervisor graph
        still works — depth cap doesn't break top-level dispatch."""
        registry, transport = _build_registry([_MockResponse(content="hi")])
        clf = _ScriptedClassifier([_IntentResult(_Intent.CHITCHAT, 0.95, "rule")])
        graph = build_supervisor_graph(registry, intent_classifier=clf)

        result = await graph.ainvoke(_initial_state("hello"), config=_config())

        assert result["final_output"] == "hi"
        assert result["handoff_request"].depth == 1  # supervisor → depth=1
        assert result["handoff_response"].status == HANDOFF_OK
