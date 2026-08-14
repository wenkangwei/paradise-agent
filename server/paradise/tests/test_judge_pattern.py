"""Phase 5 Step 5 — JudgePattern SubgraphPattern tests.

Verifies:
  1. High score → immediate OK (no retry)
  2. Low score → retry, then best-of-N kept
  3. Judge LLM failure → fail-soft (response still returned)
  4. Max retries exhausted → returns best_output with judge_below_threshold
  5. register_judge_pattern replaces existing registration

Run:
    cd server && python -m pytest paradise/tests/test_judge_pattern.py -v
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from paradise.core.handoff import HANDOFF_OK, HandoffRequest
from paradise.core.patterns import (
    JudgePattern, register_all, register_basic_patterns,
    register_judge_pattern,
)
from paradise.core.registry import SubgraphRegistry


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
    """Returns one canned response per chat() call, cycling through a list."""
    api_mode = "openai_compat"

    def __init__(self, responses: list[_MockResponse]):
        self._responses = list(responses)
        self._idx = 0
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return self._responses[-1]


def _build_judge_graph(
    gen_content: str = "generated reply",
    judge_content: str = "SCORE: 0.9\nFEEDBACK: great",
    threshold: float = 0.6,
    max_retries: int = 1,
):
    """Build a JudgePattern graph with canned responses."""
    gen_transport = _ScriptedTransport([_MockResponse(content=gen_content)])
    judge_transport = _ScriptedTransport([_MockResponse(content=judge_content)])

    pattern = JudgePattern(
        transport=gen_transport,
        judge_transport=judge_transport,
        threshold=threshold,
        max_retries=max_retries,
    )
    registry = SubgraphRegistry()
    graph = pattern.build(registry)
    return graph, gen_transport, judge_transport


def _make_request(message: str = "hello") -> HandoffRequest:
    return HandoffRequest(
        session_id="sess-judge-1",
        user_id="user-1",
        trace_id="trace-judge-1",
        message=message,
    )


# ─────────────────────────────────────────────────────────────────────
# 1. High score → immediate OK
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_high_score_immediate_ok():
    """Score above threshold → HANDOFF_OK on first attempt, no retry."""
    graph, gen_t, judge_t = _build_judge_graph(
        gen_content="The answer is 42.",
        judge_content="SCORE: 0.95\nFEEDBACK: accurate and concise",
    )
    result = await graph.ainvoke({"handoff": _make_request("what is 6*7?")})

    hr = result["handoff_response"]
    assert hr.status == HANDOFF_OK
    assert hr.output == "The answer is 42."
    assert hr.artifacts["judge_score"] == 0.95
    assert hr.artifacts["judge_feedback"] == "accurate and concise"
    # Only 1 generate call (no retry)
    assert len(gen_t.calls) == 1
    assert len(judge_t.calls) == 1


# ─────────────────────────────────────────────────────────────────────
# 2. Low score → retry, best-of-N kept
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_low_score_triggers_retry_best_of_n():
    """Score below threshold → regenerate, higher-scoring response kept."""
    # First gen: poor answer. Second gen (retry): better answer.
    gen_transport = _ScriptedTransport([
        _MockResponse(content="idk"),
        _MockResponse(content="The answer is 42."),
    ])
    # First judge: low score. Second judge: high score.
    judge_transport = _ScriptedTransport([
        _MockResponse(content="SCORE: 0.2\nFEEDBACK: unhelpful"),
        _MockResponse(content="SCORE: 0.9\nFEEDBACK: correct"),
    ])

    pattern = JudgePattern(
        transport=gen_transport,
        judge_transport=judge_transport,
        threshold=0.6,
        max_retries=1,
    )
    registry = SubgraphRegistry()
    graph = pattern.build(registry)

    result = await graph.ainvoke({"handoff": _make_request("what is 6*7?")})

    hr = result["handoff_response"]
    assert hr.status == HANDOFF_OK
    # Best output should be the higher-scoring response
    assert hr.output == "The answer is 42."
    assert hr.artifacts["judge_score"] == 0.9
    assert len(gen_transport.calls) == 2


# ─────────────────────────────────────────────────────────────────────
# 3. Judge LLM failure → fail-soft
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_judge_llm_failure_fail_soft():
    """When the judge LLM call raises, response is still returned."""
    class _BoomTransport:
        api_mode = "openai_compat"

        async def chat(self, **kwargs):
            raise RuntimeError("judge endpoint down")

    gen_transport = _ScriptedTransport([_MockResponse(content="hello there")])
    pattern = JudgePattern(
        transport=gen_transport,
        judge_transport=_BoomTransport(),
        threshold=0.6,
        max_retries=1,
    )
    registry = SubgraphRegistry()
    graph = pattern.build(registry)

    result = await graph.ainvoke({"handoff": _make_request("hi")})

    hr = result["handoff_response"]
    # Judge failed → score defaults to 0.0 → below threshold → retry
    # But gen_transport only has 1 response, so retry gets the same.
    # After max_retries exhausted, best_output (the only one) is returned.
    assert hr.status == HANDOFF_OK
    assert hr.output == "hello there"


# ─────────────────────────────────────────────────────────────────────
# 4. Max retries exhausted → best_output returned
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retries_exhausted_returns_best():
    """All scores below threshold, retries run out → return best."""
    gen_transport = _ScriptedTransport([
        _MockResponse(content="bad answer 1"),
        _MockResponse(content="slightly better answer"),
    ])
    judge_transport = _ScriptedTransport([
        _MockResponse(content="SCORE: 0.3\nFEEDBACK: poor"),
        _MockResponse(content="SCORE: 0.5\nFEEDBACK: still below"),
    ])

    pattern = JudgePattern(
        transport=gen_transport,
        judge_transport=judge_transport,
        threshold=0.8,
        max_retries=1,
    )
    registry = SubgraphRegistry()
    graph = pattern.build(registry)

    result = await graph.ainvoke({"handoff": _make_request("explain X")})

    hr = result["handoff_response"]
    assert hr.status == HANDOFF_OK
    # Best score was 0.5 (second attempt)
    assert hr.artifacts["judge_score"] == 0.5
    assert hr.artifacts.get("judge_below_threshold") is True
    # Best output is the one scored 0.5
    assert hr.output == "slightly better answer"


# ─────────────────────────────────────────────────────────────────────
# 5. register_judge_pattern integration
# ─────────────────────────────────────────────────────────────────────


def test_register_judge_pattern_into_registry():
    """register_judge_pattern adds JudgePattern to a registry."""
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=_ScriptedTransport(
        [_MockResponse(content="x")]
    ))

    # "judge" is not registered by default
    modes_before = registry.list_modes()
    assert "judge" not in modes_before

    register_judge_pattern(
        registry,
        transport=_ScriptedTransport([_MockResponse(content="x")]),
        threshold=0.7,
    )

    modes_after = registry.list_modes()
    assert "judge" in modes_after

    spec = registry.get("judge")
    assert spec is not None
    assert spec.pattern.name == "judge"


def test_register_judge_pattern_idempotent():
    """Calling register_judge_pattern twice doesn't crash."""
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=_ScriptedTransport(
        [_MockResponse(content="x")]
    ))

    register_judge_pattern(
        registry,
        transport=_ScriptedTransport([_MockResponse(content="x")]),
    )
    # Second call should replace, not crash
    register_judge_pattern(
        registry,
        transport=_ScriptedTransport([_MockResponse(content="x")]),
        threshold=0.9,
    )

    assert "judge" in registry.list_modes()
    spec = registry.get("judge")
    assert spec.pattern._threshold == 0.9


# ─────────────────────────────────────────────────────────────────────
# 6. _parse_judge_output edge cases
# ─────────────────────────────────────────────────────────────────────


def test_parse_judge_output_valid():
    from paradise.core.patterns.judge import _parse_judge_output
    score, fb = _parse_judge_output("SCORE: 0.85\nFEEDBACK: good stuff")
    assert score == 0.85
    assert fb == "good stuff"


def test_parse_judge_output_no_score_line():
    from paradise.core.patterns.judge import _parse_judge_output
    score, fb = _parse_judge_output("some random text without score")
    assert score == 0.5  # default fallback
    assert "some random" in fb


def test_parse_judge_output_score_clamped():
    from paradise.core.patterns.judge import _parse_judge_output
    score, _ = _parse_judge_output("SCORE: 1.5\nFEEDBACK: over")
    assert score == 1.0
    score, _ = _parse_judge_output("SCORE: -0.3\nFEEDBACK: under")
    assert score == 0.0
