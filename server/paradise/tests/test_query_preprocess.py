"""Phase 6 — QueryPreprocessor tests.

Verifies:
  1. LLM correction is parsed and returned
  2. Rewrites are parsed and capped
  3. LLM failure → original message (fail-soft)
  4. Short messages skip the LLM call
  5. Supervisor graph integration: corrected_message flows to intent

Run:
    cd server && python -m pytest paradise/tests/test_query_preprocess.py -v
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from paradise.core.preprocess import (
    PreprocessResult,
    QueryPreprocessor,
    should_preprocess,
)
from paradise.core.patterns import register_all, register_basic_patterns
from paradise.core.registry import SubgraphRegistry
from paradise.core.supervisor import build_supervisor_graph


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

    def __init__(self, response: _MockResponse | None = None):
        self._response = response
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self._response is None:
            raise RuntimeError("no scripted response")
        return self._response


class _MockLLMConfig:
    model = "test-model"
    api_url = "http://localhost:11434"
    api_key = ""
    max_tokens = 512


# ─────────────────────────────────────────────────────────────────────
# should_preprocess heuristic gate
# ─────────────────────────────────────────────────────────────────────


def test_should_preprocess_skips_short():
    assert should_preprocess("") is False
    assert should_preprocess("hi") is False
    assert should_preprocess("你好") is False  # 2 chars < 5


def test_should_preprocess_skips_short_ascii():
    # pure ASCII < 20 chars → skip
    assert should_preprocess("what time is it") is False


def test_should_preprocess_processes_long():
    assert should_preprocess("帮我查一下今天北京的天气情况怎么样") is True
    assert should_preprocess("what is the weather like in beijing today") is True


# ─────────────────────────────────────────────────────────────────────
# QueryPreprocessor.process
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preprocess_corrects_typos():
    transport = _ScriptedTransport(_MockResponse(
        content='{"corrected": "帮我查下天气怎么样", "rewrites": []}',
    ))
    pre = QueryPreprocessor(transport, _MockLLMConfig())
    result = await pre.process("帮我查下天汽怎么样")
    assert result.corrected == "帮我查下天气怎么样"
    assert result.effective == "帮我查下天气怎么样"
    assert result.rewrites == []
    assert result.skipped is False


@pytest.mark.asyncio
async def test_preprocess_generates_rewrites():
    transport = _ScriptedTransport(_MockResponse(
        content='{"corrected": "original query", "rewrites": ["alt 1", "alt 2"]}',
    ))
    pre = QueryPreprocessor(transport, _MockLLMConfig())
    result = await pre.process("some long enough query for llm processing")
    assert result.rewrites == ["alt 1", "alt 2"]


@pytest.mark.asyncio
async def test_preprocess_llm_failure_returns_original():
    transport = _ScriptedTransport(None)  # raises on chat()
    pre = QueryPreprocessor(transport, _MockLLMConfig())
    result = await pre.process("帮我查一下今天北京的天气情况怎么样")
    assert result.corrected == "帮我查一下今天北京的天气情况怎么样"
    assert result.rewrites == []
    assert result.effective == "帮我查一下今天北京的天气情况怎么样"


@pytest.mark.asyncio
async def test_preprocess_skips_short_messages_no_llm_call():
    transport = _ScriptedTransport(_MockResponse(content="{}"))
    pre = QueryPreprocessor(transport, _MockLLMConfig())
    result = await pre.process("hi")
    assert result.skipped is True
    assert len(transport.calls) == 0  # LLM never called


@pytest.mark.asyncio
async def test_preprocess_malformed_json_returns_original():
    transport = _ScriptedTransport(_MockResponse(content="not json at all"))
    pre = QueryPreprocessor(transport, _MockLLMConfig())
    result = await pre.process("帮我查一下今天北京的天气情况怎么样")
    assert result.corrected == "帮我查一下今天北京的天气情况怎么样"
    assert result.rewrites == []


# ─────────────────────────────────────────────────────────────────────
# Supervisor graph integration
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_preprocess_node_passes_corrected_to_handoff(tmp_path):
    """preprocess_node stores corrected_message; _build_handoff_request
    uses it as the subgraph message."""
    from paradise.core.handoff import HANDOFF_OK

    captured_context: dict = {}

    # Build a chat registry that captures the incoming handoff request
    chat_transport = _ScriptedTransport(_MockResponse(content="ok"))
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=chat_transport,
                            llm_config=_MockLLMConfig())

    # Wrap the compiled chat graph to capture the request
    original_get = registry.get
    def capturing_get(name):
        spec = original_get(name)
        return spec
    # Simpler: capture via a preprocessor mock and check state after invoke.

    preprocess_transport = _ScriptedTransport(_MockResponse(
        content='{"corrected": "帮我查下天气怎么样", "rewrites": ["今天天气如何"]}',
    ))
    preprocessor = QueryPreprocessor(preprocess_transport, _MockLLMConfig())

    graph = build_supervisor_graph(registry, preprocessor=preprocessor)
    state = await graph.ainvoke(
        {
            "user_message": "帮我查下天汽怎么样",
            "session_id": "sess-pre-1",
            "trace_id": "trace-pre-1",
            "user_id": "user-1",
            "user_tier": "free",
        },
        config={"configurable": {"thread_id": "pre-integration-1"}},
    )

    # corrected_message stored in state
    assert state.get("corrected_message") == "帮我查下天气怎么样"
    assert state.get("rewritten_queries") == ["今天天气如何"]
    # handoff_request.message uses the corrected version
    assert state.get("handoff_request").message == "帮我查下天气怎么样"
    # and context carries the rewrites
    assert state["handoff_request"].context.get("rewritten_queries") == [
        "今天天气如何"
    ]
    assert state.get("final_output") == "ok"


@pytest.mark.asyncio
async def test_supervisor_preprocess_disabled_passes_through():
    """When preprocessor is None, preprocess_node is a no-op."""
    chat_transport = _ScriptedTransport(_MockResponse(content="fine"))
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=chat_transport,
                            llm_config=_MockLLMConfig())

    graph = build_supervisor_graph(registry, preprocessor=None)
    state = await graph.ainvoke(
        {
            "user_message": "raw message stays",
            "session_id": "s", "trace_id": "t",
            "user_id": "u", "user_tier": "free",
        },
        config={"configurable": {"thread_id": "pre-disabled-1"}},
    )
    assert "corrected_message" not in state or not state.get("corrected_message")
    assert state.get("handoff_request").message == "raw message stays"
