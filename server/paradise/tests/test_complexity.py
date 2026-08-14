"""Phase 6 — ComplexityAssessor tests.

Verifies:
  1. task_complex intent → complex (rule)
  2. chitchat/creative intent → simple (rule)
  3. Long message + sequencing marker → complex (rule)
  4. Multiple question marks → complex (rule)
  5. Inconclusive → LLM fallback decides
  6. LLM failure/timeout → simple (default)
  7. Supervisor routing: complex + agent_team registered → agent_team

Run:
    cd server && python -m pytest paradise/tests/test_complexity.py -v
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from paradise.core.complexity import ComplexityAssessor, ComplexityResult
from paradise.core.patterns import (
    register_all, register_basic_patterns, register_agent_team_pattern,
)
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

    def __init__(self, response: _MockResponse | None = None,
                 delay: float = 0.0):
        self._response = response
        self._delay = delay
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._response is None:
            raise RuntimeError("no scripted response")
        return self._response


class _MockLLMConfig:
    model = "test-model"
    api_url = "http://localhost:11434"
    api_key = ""
    max_tokens = 32


class _MockIntentResult:
    def __init__(self, intent: str, confidence: float = 0.9):
        from paradise.core.intent.base import Intent
        self.intent = Intent(intent)
        self.confidence = confidence
        self.source = "mock"
        self.latency_ms = 0
        self.meta = {}


# ─────────────────────────────────────────────────────────────────────
# L1 rules
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_complex_intent_is_complex():
    assessor = ComplexityAssessor()  # no transport — rules only
    result = await assessor.assess("any message", "task_complex")
    assert result.is_complex is True
    assert result.source == "rule"


@pytest.mark.asyncio
async def test_chitchat_is_simple():
    assessor = ComplexityAssessor()
    result = await assessor.assess("hello there friend", "chitchat")
    assert result.is_complex is False
    assert result.source == "rule"


@pytest.mark.asyncio
async def test_long_message_with_sequencing_marker_is_complex():
    assessor = ComplexityAssessor()
    msg = (
        "帮我先调研一下 LangGraph 和 AutoGen 各自的架构特点和优缺点，"
        "然后对比两者的差异，最后写一份总结报告"
    )
    result = await assessor.assess(msg, "unknown")
    assert result.is_complex is True
    assert result.source == "rule"


@pytest.mark.asyncio
async def test_multiple_questions_is_complex():
    assessor = ComplexityAssessor()
    result = await assessor.assess("什么是RAG？它和微调有什么区别？", "knowledge_qa")
    assert result.is_complex is True
    assert "questions" in result.reason


@pytest.mark.asyncio
async def test_short_unknown_query_falls_to_llm():
    """Inconclusive rules + LLM wired → LLM decides."""
    transport = _ScriptedTransport(_MockResponse(content='{"complex": true}'))
    assessor = ComplexityAssessor(transport, _MockLLMConfig())
    result = await assessor.assess("介绍一下量子计算的基本原理", "knowledge_qa")
    # knowledge_qa isn't in the simple/complex sets, no "?", short — LLM
    assert result.is_complex is True
    assert result.source == "llm"
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_llm_timeout_defaults_simple():
    # 3s timeout in assessor; transport sleeps 10s → TimeoutError → simple
    transport = _ScriptedTransport(_MockResponse(content='{"complex": true}'),
                                   delay=10.0)
    assessor = ComplexityAssessor(transport, _MockLLMConfig())
    result = await assessor.assess("介绍一下量子计算的基本原理", "knowledge_qa")
    assert result.is_complex is False
    assert result.source == "default"


@pytest.mark.asyncio
async def test_llm_error_defaults_simple():
    transport = _ScriptedTransport(None)  # raises
    assessor = ComplexityAssessor(transport, _MockLLMConfig())
    result = await assessor.assess("介绍一下量子计算的基本原理", "knowledge_qa")
    assert result.is_complex is False
    assert result.source == "default"


@pytest.mark.asyncio
async def test_no_llm_wired_defaults_simple():
    assessor = ComplexityAssessor()  # no transport
    result = await assessor.assess("介绍一下量子计算的基本原理", "knowledge_qa")
    assert result.is_complex is False
    assert result.source == "default"


# ─────────────────────────────────────────────────────────────────────
# Supervisor routing integration
# ─────────────────────────────────────────────────────────────────────


class _AlwaysComplexAssessor:
    """Test double — classifies everything complex."""
    async def assess(self, message: str, intent: str) -> ComplexityResult:
        return ComplexityResult(True, "forced", "rule")


class _AlwaysSimpleAssessor:
    """Test double — classifies everything simple."""
    async def assess(self, message: str, intent: str) -> ComplexityResult:
        return ComplexityResult(False, "forced", "rule")


class _MockClassifier:
    """Returns a fixed intent."""
    def __init__(self, intent: str):
        self._intent = intent

    async def classify(self, message: str):
        return _MockIntentResult(self._intent)


def _build_registry(content: str = "response"):
    transport = _ScriptedTransport(_MockResponse(content=content))
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=transport,
                            llm_config=_MockLLMConfig())
    return registry


@pytest.mark.asyncio
async def test_complex_routes_to_agent_team_when_registered():
    registry = _build_registry()
    register_agent_team_pattern(
        registry,
        transport=_ScriptedTransport(_MockResponse(content="team output")),
        llm_config=_MockLLMConfig(),
    )
    graph = build_supervisor_graph(
        registry,
        intent_classifier=_MockClassifier("knowledge_qa"),
        complexity_assessor=_AlwaysComplexAssessor(),
    )
    state = await graph.ainvoke(
        {"user_message": "some query", "session_id": "s", "trace_id": "t",
         "user_id": "u", "user_tier": "free"},
        config={"configurable": {"thread_id": "complex-route-1"}},
    )
    assert state.get("mode") == "agent_team"
    assert state.get("complexity") == "complex"
    assert state.get("final_output") == "team output"


@pytest.mark.asyncio
async def test_complex_routes_to_plan_execute_without_agent_team():
    registry = _build_registry()
    graph = build_supervisor_graph(
        registry,
        intent_classifier=_MockClassifier("knowledge_qa"),  # → chat normally
        complexity_assessor=_AlwaysComplexAssessor(),
    )
    state = await graph.ainvoke(
        {"user_message": "some query", "session_id": "s", "trace_id": "t",
         "user_id": "u", "user_tier": "free"},
        config={"configurable": {"thread_id": "complex-route-2"}},
    )
    # agent_team not registered → chat mode upgraded to plan_execute
    assert state.get("mode") == "plan_execute"
    assert state.get("complexity") == "complex"


@pytest.mark.asyncio
async def test_simple_keeps_intent_mode():
    registry = _build_registry()
    graph = build_supervisor_graph(
        registry,
        intent_classifier=_MockClassifier("chitchat"),  # → chat
        complexity_assessor=_AlwaysSimpleAssessor(),
    )
    state = await graph.ainvoke(
        {"user_message": "hello", "session_id": "s", "trace_id": "t",
         "user_id": "u", "user_tier": "free"},
        config={"configurable": {"thread_id": "simple-route-1"}},
    )
    assert state.get("mode") == "chat"
    assert state.get("complexity") == "simple"


@pytest.mark.asyncio
async def test_no_assessor_leaves_complexity_unknown():
    registry = _build_registry()
    graph = build_supervisor_graph(
        registry,
        intent_classifier=_MockClassifier("chitchat"),
        complexity_assessor=None,
    )
    state = await graph.ainvoke(
        {"user_message": "hello", "session_id": "s", "trace_id": "t",
         "user_id": "u", "user_tier": "free"},
        config={"configurable": {"thread_id": "no-assessor-1"}},
    )
    assert state.get("complexity") == "unknown"
    assert state.get("mode") == "chat"
