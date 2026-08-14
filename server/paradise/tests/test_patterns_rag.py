"""Phase 2.11 — RagPattern tests.

Run:
    cd server && python -m pytest paradise/tests/test_patterns_rag.py -v

Scope:
  * build() returns a compiled graph
  * Happy path: retriever returns hits → context folded into prompt → LLM reply
  * Retriever missing (lazy-resolve fails) → chat-fallback, rag_used=False
  * Retriever raises → chat-fallback, rag_used=False
  * Retriever returns [] → rag_used=True, but empty context in prompt
  * LLM call fails → HANDOFF_ERROR with artifacts preserved
  * artifacts.retrieved shape (content / source / score)
  * Context formatter handles missing content / source
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from paradise.core.handoff import (
    HANDOFF_ERROR,
    HANDOFF_OK,
    HandoffRequest,
    HandoffResponse,
)
from paradise.core.patterns import RagPattern, Retriever
from paradise.core.patterns.rag import _format_context
from paradise.core.registry import SubgraphRegistry


# ─────────────────────────────────────────────────────────────────────
# Mocks
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _MockResponse:
    content: str | None
    tool_calls: list | None = None
    finish_reason: str = "stop"
    usage: Any | None = None


class _ScriptedTransport:
    """Transport mock that pops responses from a queue."""
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


class _ScriptedRetriever:
    """Retriever mock implementing the Retriever protocol.

    Each call to .retrieve() pops the next pre-loaded result. Results
    can be list[dict] (success) or Exception (simulate failure).
    """
    def __init__(self, results: list):
        self._results = list(results)
        self.calls: list[tuple[str, int]] = []

    async def retrieve(self, query: str, top_k: int = 3):
        self.calls.append((query, top_k))
        if not self._results:
            return []
        r = self._results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _NoRetriever:
    """Has no .retrieve method — exercises the lazy-resolve path."""


def _req(message: str = "what is RAG?", trace_id: str = "t1") -> HandoffRequest:
    return HandoffRequest(
        session_id="s1", user_id="u1", trace_id=trace_id, message=message,
    )


# ─────────────────────────────────────────────────────────────────────
# Pattern metadata
# ─────────────────────────────────────────────────────────────────────


class TestRagPatternMetadata:
    def test_name(self):
        assert RagPattern.name == "rag"

    def test_category_basic(self):
        assert RagPattern.category == "basic"

    def test_availability_true(self):
        """RagPattern is always available — chat-fallback covers missing retriever."""
        p = RagPattern(transport=object())
        assert p.availability() is True

    def test_max_turns(self):
        assert RagPattern.max_turns == 2

    def test_cost_budget(self):
        assert RagPattern.cost_budget_usd == 0.03


# ─────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────


class TestRagPatternBuild:
    def test_build_returns_compiled_graph(self):
        p = RagPattern(
            transport=_ScriptedTransport([_MockResponse(content="ok")]),
            retriever=_ScriptedRetriever([[]]),
        )
        graph = p.build(SubgraphRegistry())
        assert graph is not None
        assert hasattr(graph, "ainvoke")

    def test_retriever_protocol_satisfied_by_scripted(self):
        """ScriptedRetriever should be recognized as a Retriever."""
        r = _ScriptedRetriever([[]])
        # runtime_checkable Protocol — isinstance works
        assert isinstance(r, Retriever)

    def test_no_retrieve_method_fails_protocol(self):
        """_NoRetriever (no .retrieve) should NOT satisfy the protocol."""
        # This verifies the runtime_checkable decorator is doing its job
        assert not isinstance(_NoRetriever(), Retriever)


# ─────────────────────────────────────────────────────────────────────
# Invoke — happy path + fail-soft matrix
# ─────────────────────────────────────────────────────────────────────


class TestRagPatternInvoke:
    @pytest.mark.asyncio
    async def test_happy_path_with_retrieved_context(self):
        """Retriever returns 2 hits → context formatted into prompt → reply."""
        transport = _ScriptedTransport([_MockResponse(content="RAG is retrieval-augmented generation")])
        retriever = _ScriptedRetriever([[
            {"content": "RAG stands for Retrieval-Augmented Generation.", "source": "docs/rag.md"},
            {"content": "It reduces hallucination by grounding.", "source": "docs/rag.md"},
        ]])
        p = RagPattern(transport=transport, retriever=retriever)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp: HandoffResponse = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.output == "RAG is retrieval-augmented generation"
        assert resp.artifacts["rag_used"] is True
        assert len(resp.artifacts["retrieved"]) == 2
        # Retriever was called with the user message
        assert retriever.calls == [("what is RAG?", 3)]
        # Synthesis prompt contains context
        sys_prompt = transport.calls[0]["system_prompt"]
        assert "Retrieval-Augmented Generation" in sys_prompt
        assert "docs/rag.md" in sys_prompt

    @pytest.mark.asyncio
    async def test_retriever_missing_falls_back_to_chat(self):
        """No retriever wired AND lazy-resolve fails → chat-fallback path.

        We force lazy-resolve failure by NOT injecting a retriever. The
        default _resolve_retriever tries qdrant_provider which isn't
        installed in the test env, so returns None.
        """
        transport = _ScriptedTransport([_MockResponse(content="parametric answer")])
        p = RagPattern(transport=transport)  # no retriever
        # Stub _resolve_retriever to return None — simulates qdrant absent
        p._resolve_retriever = lambda: None  # type: ignore[assignment]
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.output == "parametric answer"
        assert resp.artifacts["rag_used"] is False
        assert resp.artifacts["retrieved"] == []
        # Fallback prompt was used
        assert "no context retrieved" in transport.calls[0]["system_prompt"]

    @pytest.mark.asyncio
    async def test_retriever_raises_falls_back_to_chat(self):
        """Retriever raises → caught → chat-fallback (rag_used=False)."""
        transport = _ScriptedTransport([_MockResponse(content="fallback reply")])
        retriever = _ScriptedRetriever([RuntimeError("qdrant exploded")])
        p = RagPattern(transport=transport, retriever=retriever)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.output == "fallback reply"
        assert resp.artifacts["rag_used"] is False

    @pytest.mark.asyncio
    async def test_empty_hits_rag_used_true(self):
        """Retriever returns [] → rag_used=True (retrieval happened, just empty).

        Distinguishes from retriever-missing case: observability can tell
        "qdrant returned 0 hits" apart from "qdrant was unreachable".
        """
        transport = _ScriptedTransport([_MockResponse(content="I don't know")])
        retriever = _ScriptedRetriever([[]])
        p = RagPattern(transport=transport, retriever=retriever)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp = result["handoff_response"]
        assert resp.artifacts["rag_used"] is True
        assert resp.artifacts["retrieved"] == []
        # Empty hits → fallback prompt path (no context to ground on),
        # but the prompt wording is "no context retrieved" rather than
        # claiming retrieval was unavailable.
        sys_prompt = transport.calls[0]["system_prompt"]
        assert "no context retrieved" in sys_prompt
        # And the user message is preserved
        assert transport.calls[0]["messages"][0]["content"] == "what is RAG?"

    @pytest.mark.asyncio
    async def test_synthesize_failure_returns_handoff_error(self):
        """LLM call raises → HANDOFF_ERROR with artifacts preserved."""
        transport = _ScriptedTransport([RuntimeError("LLM down")])
        retriever = _ScriptedRetriever([[
            {"content": "some context", "source": "docs/x.md"},
        ]])
        p = RagPattern(transport=transport, retriever=retriever)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_ERROR
        assert "LLM down" in (resp.error or "")
        # Artifacts still populated for debugging
        assert resp.artifacts["rag_used"] is True
        assert len(resp.artifacts["retrieved"]) == 1


# ─────────────────────────────────────────────────────────────────────
# Context formatter — unit tests
# ─────────────────────────────────────────────────────────────────────


class TestFormatContext:
    def test_single_hit_with_source(self):
        out = _format_context([
            {"content": "hello world", "source": "a.md"},
        ])
        assert "[1]" in out
        assert "(source: a.md)" in out
        assert "hello world" in out

    def test_hit_without_source(self):
        out = _format_context([{"content": "just text"}])
        assert "[1]" in out
        # No source attribution should appear
        assert "source:" not in out
        assert "just text" in out

    def test_score_is_not_in_output(self):
        """Score should be hidden from LLM to avoid anchoring."""
        out = _format_context([
            {"content": "x", "source": "y", "score": 0.99},
        ])
        assert "0.99" not in out

    def test_empty_content_skipped(self):
        """Empty-content hit is dropped; the next valid hit is renumbered to [1]."""
        out = _format_context([
            {"content": "", "source": "empty.md"},
            {"content": "real", "source": "real.md"},
        ])
        # "real" should be [1] (renumbered), NOT [2] — no gaps in numbering
        assert "[1] (source: real.md) real" in out
        assert "[2]" not in out
        assert "empty.md" not in out  # the empty hit's source dropped

    def test_all_empty_returns_placeholder(self):
        out = _format_context([{"content": ""}, {"content": "   "}])
        assert "no context" in out.lower()

    def test_numbered_correctly(self):
        out = _format_context([
            {"content": "a"}, {"content": "b"}, {"content": "c"},
        ])
        assert "[1]" in out and "[2]" in out and "[3]" in out
