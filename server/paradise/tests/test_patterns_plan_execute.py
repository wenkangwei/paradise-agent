"""Phase 2.11 / 2.12 — PlanExecutePattern tests.

Run:
    cd server && python -m pytest paradise/tests/test_patterns_plan_execute.py -v

Scope:
  * Pattern metadata (name / category / max_turns / cost_budget)
  * build() returns a compiled graph
  * Happy path: planner produces 3 steps → execute each → synthesize
  * Planner returns invalid JSON → single-step fallback (plan_used=False)
  * Planner raises → single-step fallback
  * Step execution failure → recorded in artifacts, loop continues
  * Plan exceeds budget → HANDOFF_MAX_TURNS, partial synthesis
  * Synthesizer fails → HANDOFF_ERROR with artifacts preserved
  * _parse_plan_json lenient JSON extraction
  * Kwargs builders (planner / step / synthesis) shape messages correctly

Phase 2.12 added:
  * nested_mode="tool_react" → execute_node delegates each step to
    tool_react subgraph via invoke_subgraph (depth+1)
  * Nested depth cap (depth>2) → execute_node falls back to chat for
    that step, artifacts.step_results[i].nested_fallback=True
  * Nested subgraph error → same fallback behavior
"""
from __future__ import annotations

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
from paradise.core.patterns import PlanExecutePattern
from paradise.core.patterns.plan_execute import _parse_plan_json
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
    """Transport mock that pops responses from a queue.

    Each call to .chat() pops the next response. Order matters:
    for plan_execute the sequence is [planner, step1, step2, ..., synth].
    """
    api_mode = "openai_compat"

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError(
                f"ScriptedTransport queue empty — got {len(self.calls)} calls"
            )
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _req(message: str = "compare React vs Vue",
         trace_id: str = "t1") -> HandoffRequest:
    return HandoffRequest(
        session_id="s1", user_id="u1", trace_id=trace_id, message=message,
    )


# ─────────────────────────────────────────────────────────────────────
# Pattern metadata
# ─────────────────────────────────────────────────────────────────────


class TestPlanExecuteMetadata:
    def test_name(self):
        assert PlanExecutePattern.name == "plan_execute"

    def test_category_basic(self):
        assert PlanExecutePattern.category == "basic"

    def test_availability_true(self):
        p = PlanExecutePattern(transport=object())
        assert p.availability() is True

    def test_max_turns(self):
        assert PlanExecutePattern.max_turns == 5

    def test_cost_budget(self):
        assert PlanExecutePattern.cost_budget_usd == 0.10


# ─────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────


class TestPlanExecuteBuild:
    def test_returns_compiled_graph(self):
        p = PlanExecutePattern(
            transport=_ScriptedTransport([_MockResponse(content='{"steps":[]}')]),
        )
        graph = p.build(SubgraphRegistry())
        assert graph is not None
        assert hasattr(graph, "ainvoke")


# ─────────────────────────────────────────────────────────────────────
# Invoke — happy path + fail-soft matrix
# ─────────────────────────────────────────────────────────────────────


class TestPlanExecuteInvoke:
    @pytest.mark.asyncio
    async def test_happy_path_three_steps(self):
        """Planner returns 3 steps → 3 step LLM calls → 1 synthesis call.

        Total transport.chat calls: 1 (plan) + 3 (steps) + 1 (synth) = 5.
        """
        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({
                "steps": ["step A", "step B", "step C"],
            })),
            _MockResponse(content="result A"),
            _MockResponse(content="result B"),
            _MockResponse(content="result C"),
            _MockResponse(content="synthesized final answer"),
        ])
        p = PlanExecutePattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp: HandoffResponse = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.output == "synthesized final answer"
        assert resp.artifacts["plan_used"] is True
        assert len(resp.artifacts["plan_steps"]) == 3
        assert len(resp.artifacts["step_results"]) == 3
        # 5 total transport calls
        assert len(transport.calls) == 5

    @pytest.mark.asyncio
    async def test_planner_invalid_json_single_step_fallback(self):
        """Planner returns garbage → fallback to 1-step plan (plan_used=False).

        The single step is the user's original message — pattern still
        produces a coherent answer rather than crashing.
        """
        transport = _ScriptedTransport([
            _MockResponse(content="sorry I cannot do that"),  # planner
            _MockResponse(content="single-step output"),     # 1 step
            _MockResponse(content="final synthesis"),        # synth
        ])
        p = PlanExecutePattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req(message="hi")})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.output == "final synthesis"
        assert resp.artifacts["plan_used"] is False
        assert resp.artifacts["plan_steps"] == ["hi"]
        assert len(resp.artifacts["step_results"]) == 1

    @pytest.mark.asyncio
    async def test_planner_transport_failure_single_step_fallback(self):
        """Planner LLM call raises → fallback to 1-step plan."""
        transport = _ScriptedTransport([
            RuntimeError("planner LLM dead"),                # planner
            _MockResponse(content="step output"),            # 1 step
            _MockResponse(content="synthesis"),              # synth
        ])
        p = PlanExecutePattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.artifacts["plan_used"] is False

    @pytest.mark.asyncio
    async def test_step_execution_failure_continues_loop(self):
        """Step 2 transport raises → recorded as error in artifacts,
        loop continues to step 3 + synthesis."""
        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({
                "steps": ["s1", "s2", "s3"],
            })),                                              # planner
            _MockResponse(content="result 1"),                # step 1
            RuntimeError("step 2 crashed"),                   # step 2
            _MockResponse(content="result 3"),                # step 3
            _MockResponse(content="synthesis with partial"),  # synth
        ])
        p = PlanExecutePattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.output == "synthesis with partial"
        step_results = resp.artifacts["step_results"]
        assert len(step_results) == 3
        assert step_results[1].get("error")  # step 2 recorded error
        assert step_results[1].get("output") == ""

    @pytest.mark.asyncio
    async def test_plan_exceeds_budget_returns_max_turns(self):
        """Plan has 4 steps but max_executions=3 → step 4 skipped,
        status=HANDOFF_MAX_TURNS, partial synthesis produced."""
        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({
                "steps": ["s1", "s2", "s3", "s4"],
            })),                                              # planner
            _MockResponse(content="r1"),                      # step 1
            _MockResponse(content="r2"),                      # step 2
            _MockResponse(content="r3"),                      # step 3
            # step 4 should NOT execute — budget exhausted
            _MockResponse(content="partial synthesis"),       # synth
        ])
        p = PlanExecutePattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_MAX_TURNS
        assert resp.output == "partial synthesis"
        # Only 3 steps executed (max_turns=5, plan=1, synth=1, exec=3)
        assert len(resp.artifacts["step_results"]) == 3
        # Total: 1 plan + 3 exec + 1 synth = 5 calls
        assert len(transport.calls) == 5

    @pytest.mark.asyncio
    async def test_synthesizer_failure_returns_handoff_error(self):
        """Synthesis call raises → HANDOFF_ERROR with artifacts preserved."""
        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({"steps": ["s1"]})),  # planner
            _MockResponse(content="step output"),                  # step 1
            RuntimeError("synth LLM dead"),                        # synth
        ])
        p = PlanExecutePattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_ERROR
        assert "synth failed" in (resp.error or "").lower() or "synthesis failed" in (resp.error or "").lower()
        # Artifacts preserved for debugging
        assert len(resp.artifacts["step_results"]) == 1
        assert resp.artifacts["plan_used"] is True

    @pytest.mark.asyncio
    async def test_empty_steps_list_single_step_fallback(self):
        """Planner returns {"steps: []} → fallback to 1-step plan."""
        transport = _ScriptedTransport([
            _MockResponse(content='{"steps": []}'),          # planner
            _MockResponse(content="single output"),          # step 1
            _MockResponse(content="synthesis"),              # synth
        ])
        p = PlanExecutePattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req(message="hi")})

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.artifacts["plan_used"] is False
        assert resp.artifacts["plan_steps"] == ["hi"]

    @pytest.mark.asyncio
    async def test_planner_with_markdown_fence(self):
        """Planner wraps JSON in ```json fence → still parses."""
        transport = _ScriptedTransport([
            _MockResponse(content='```json\n{"steps": ["a", "b"]}\n```'),
            _MockResponse(content="ra"),
            _MockResponse(content="rb"),
            _MockResponse(content="synthesis"),
        ])
        p = PlanExecutePattern(transport=transport)
        graph = p.build(SubgraphRegistry())

        result = await graph.ainvoke({"handoff": _req()})

        resp = result["handoff_response"]
        assert resp.artifacts["plan_used"] is True
        assert resp.artifacts["plan_steps"] == ["a", "b"]


# ─────────────────────────────────────────────────────────────────────
# _parse_plan_json — unit tests
# ─────────────────────────────────────────────────────────────────────


class TestParsePlanJson:
    def test_plain_json(self):
        steps = _parse_plan_json('{"steps": ["a", "b", "c"]}')
        assert steps == ["a", "b", "c"]

    def test_fenced_json(self):
        text = '```json\n{"steps": ["x"]}\n```'
        assert _parse_plan_json(text) == ["x"]

    def test_embedded_json(self):
        text = 'Sure! {"steps": ["1", "2"]} done.'
        assert _parse_plan_json(text) == ["1", "2"]

    def test_empty_steps_list(self):
        assert _parse_plan_json('{"steps": []}') == []

    def test_missing_steps_key(self):
        assert _parse_plan_json('{"foo": "bar"}') == []

    def test_steps_not_a_list(self):
        assert _parse_plan_json('{"steps": "not a list"}') == []

    def test_invalid_json(self):
        assert _parse_plan_json("not json") == []
        assert _parse_plan_json("") == []
        assert _parse_plan_json("{") == []

    def test_filters_empty_strings(self):
        assert _parse_plan_json('{"steps": ["", "real", "   "]}') == ["real"]

    def test_caps_at_four(self):
        """Planner says 1-4 steps; if it returns 5, we cap at 4."""
        steps = _parse_plan_json('{"steps": ["a","b","c","d","e"]}')
        assert steps == ["a", "b", "c", "d"]

    def test_coerces_non_strings(self):
        """Numbers in the list get stringified."""
        assert _parse_plan_json('{"steps": [1, 2, 3]}') == ["1", "2", "3"]


# ─────────────────────────────────────────────────────────────────────
# Kwargs builders — verify message shape without invoking transport
# ─────────────────────────────────────────────────────────────────────


class TestPlanExecuteKwargsBuilders:
    def test_planner_kwargs_uses_user_message(self):
        from paradise.tests.test_patterns_basic import _LLMConfigStub
        p = PlanExecutePattern(transport=_ScriptedTransport([]))
        cfg = _LLMConfigStub()
        kwargs = p._build_planner_kwargs(cfg, _req(message="my request"))
        assert kwargs["messages"] == [
            {"role": "user", "content": "my request"}
        ]
        assert "decompose" in kwargs["system_prompt"].lower()

    def test_step_kwargs_carries_prior_results(self):
        from paradise.tests.test_patterns_basic import _LLMConfigStub
        p = PlanExecutePattern(transport=_ScriptedTransport([]))
        cfg = _LLMConfigStub()
        kwargs = p._build_step_kwargs(
            cfg,
            _req(message="orig"),
            steps=["s1", "s2"],
            prior_results=[{"step": "s1", "output": "r1"}],
            current_step="s2",
        )
        msg = kwargs["messages"][0]["content"]
        assert "orig" in msg
        assert "s1" in msg and "s2" in msg
        assert "r1" in msg  # prior result carried through

    def test_synthesis_kwargs_includes_all_step_results(self):
        from paradise.tests.test_patterns_basic import _LLMConfigStub
        p = PlanExecutePattern(transport=_ScriptedTransport([]))
        cfg = _LLMConfigStub()
        kwargs = p._build_synthesis_kwargs(
            cfg,
            _req(message="orig"),
            results=[
                {"step": "s1", "output": "r1"},
                {"step": "s2", "output": "r2"},
            ],
        )
        msg = kwargs["messages"][0]["content"]
        assert "r1" in msg and "r2" in msg
        assert "s1" in msg and "s2" in msg


# ─────────────────────────────────────────────────────────────────────
# Phase 2.12: nested_mode — delegate steps to a subgraph
# ─────────────────────────────────────────────────────────────────────


class _NestedSubgraphResponse:
    """Stand-in for the response invoke_subgraph returns when called
    by execute_node.

    We patch paradise.core.supervisor.invoke_subgraph directly so we
    don't need to construct a full registry with a real tool_react
    subgraph — the unit under test is PlanExecutePattern's branching
    logic, not tool_react's behavior.
    """
    def __init__(self, responses_per_step: list):
        """responses_per_step: list of HandoffResponse or Exception,
        popped in order as execute_node calls invoke_subgraph."""
        self._responses = list(responses_per_step)
        self.calls: list = []

    async def __call__(self, registry, mode, req):
        self.calls.append({"mode": mode, "req": req})
        if not self._responses:
            raise AssertionError("NestedSubgraphResponse queue empty")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class TestPlanExecuteNestedMode:
    """When nested_mode is set, execute_node delegates each step to that
    subgraph via invoke_subgraph (depth+1) instead of transport.chat.
    """

    @pytest.mark.asyncio
    async def test_nested_step_delegation_happy_path(self):
        """nested_mode="tool_react" → each step calls invoke_subgraph
        with depth=2; transport.chat is NOT called for steps."""
        from paradise.core.handoff import HandoffResponse
        from unittest.mock import AsyncMock, patch

        # Transport queue: only planner + synthesis (no per-step calls)
        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({
                "steps": ["look up X", "look up Y"],
            })),
            _MockResponse(content="final synthesis"),
        ])
        p = PlanExecutePattern(transport=transport, nested_mode="tool_react")
        graph = p.build(SubgraphRegistry())

        nested_resp = _NestedSubgraphResponse([
            HandoffResponse(session_id="s", trace_id="t", output="X=42"),
            HandoffResponse(session_id="s", trace_id="t", output="Y=hello"),
        ])
        with patch(
            "paradise.core.supervisor.invoke_subgraph",
            new=nested_resp,
        ):
            result = await graph.ainvoke({
                "handoff": HandoffRequest(
                    session_id="s", user_id="u", trace_id="t",
                    message="multi", depth=1,
                ),
            })

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.output == "final synthesis"
        # Both steps went through nested path
        assert len(nested_resp.calls) == 2
        # Each nested call carried depth=2 (req.depth+1)
        assert all(c["req"].depth == 2 for c in nested_resp.calls)
        # Transport only saw planner + synthesis (2 calls, NOT 4)
        assert len(transport.calls) == 2
        # Step results record nested_mode
        step_results = resp.artifacts["step_results"]
        assert all(sr.get("nested_mode") == "tool_react" for sr in step_results)
        assert step_results[0]["output"] == "X=42"

    @pytest.mark.asyncio
    async def test_nested_depth_capped_falls_back_to_chat(self):
        """Nested invoke_subgraph returns HANDOFF_DEPTH_CAPPED →
        execute_node falls back to transport.chat for that step.
        Verifies the depth-cap graceful degradation contract."""
        from paradise.core.handoff import HandoffResponse, HANDOFF_DEPTH_CAPPED
        from unittest.mock import patch

        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({"steps": ["step A"]})),
            _MockResponse(content="flat chat output"),  # fallback for step A
            _MockResponse(content="synthesis"),
        ])
        p = PlanExecutePattern(transport=transport, nested_mode="tool_react")
        graph = p.build(SubgraphRegistry())

        nested_resp = _NestedSubgraphResponse([
            HandoffResponse(
                session_id="s", trace_id="t", output="",
                status=HANDOFF_DEPTH_CAPPED, error="depth 3 exceeds max 2",
            ),
        ])
        with patch(
            "paradise.core.supervisor.invoke_subgraph",
            new=nested_resp,
        ):
            result = await graph.ainvoke({
                "handoff": HandoffRequest(
                    session_id="s", user_id="u", trace_id="t",
                    message="x", depth=1,
                ),
            })

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        # Step was attempted nested (1 call), then fell back to chat
        assert len(nested_resp.calls) == 1
        # transport.chat got planner + flat-step + synthesis = 3 calls
        assert len(transport.calls) == 3
        # Artifact marks the fallback
        step_result = resp.artifacts["step_results"][0]
        assert step_result.get("nested_fallback") is True
        assert step_result["output"] == "flat chat output"

    @pytest.mark.asyncio
    async def test_nested_error_falls_back_to_chat(self):
        """Nested invoke_subgraph returns HANDOFF_ERROR → fall back to chat."""
        from paradise.core.handoff import HandoffResponse
        from unittest.mock import patch

        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({"steps": ["step A"]})),
            _MockResponse(content="chat fallback for failed step"),
            _MockResponse(content="synthesis"),
        ])
        p = PlanExecutePattern(transport=transport, nested_mode="tool_react")
        graph = p.build(SubgraphRegistry())

        nested_resp = _NestedSubgraphResponse([
            HandoffResponse(
                session_id="s", trace_id="t", output="",
                status=HANDOFF_ERROR, error="subgraph blew up",
            ),
        ])
        with patch(
            "paradise.core.supervisor.invoke_subgraph",
            new=nested_resp,
        ):
            result = await graph.ainvoke({
                "handoff": HandoffRequest(
                    session_id="s", user_id="u", trace_id="t",
                    message="x", depth=1,
                ),
            })

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        assert resp.artifacts["step_results"][0].get("nested_fallback") is True
        assert "chat fallback" in resp.artifacts["step_results"][0]["output"]

    @pytest.mark.asyncio
    async def test_nested_invoke_raises_falls_back_to_chat(self):
        """invoke_subgraph itself raises → execute_node catches → chat."""
        from unittest.mock import patch

        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({"steps": ["step A"]})),
            _MockResponse(content="chat reply"),
            _MockResponse(content="synthesis"),
        ])
        p = PlanExecutePattern(transport=transport, nested_mode="tool_react")
        graph = p.build(SubgraphRegistry())

        nested_resp = _NestedSubgraphResponse([
            RuntimeError("invoke_subgraph crashed"),
        ])
        with patch(
            "paradise.core.supervisor.invoke_subgraph",
            new=nested_resp,
        ):
            result = await graph.ainvoke({
                "handoff": HandoffRequest(
                    session_id="s", user_id="u", trace_id="t",
                    message="x", depth=1,
                ),
            })

        resp = result["handoff_response"]
        assert resp.status == HANDOFF_OK
        # Fallback fired — transport.chat ran for the step
        assert len(transport.calls) == 3

    @pytest.mark.asyncio
    async def test_nested_mode_none_keeps_phase_2_11_behavior(self):
        """nested_mode=None → no invoke_subgraph call, transport.chat
        handles every step. This is the Phase 2.11 baseline and must
        not regress when Phase 2.12 ships."""
        from unittest.mock import patch

        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({"steps": ["a", "b"]})),
            _MockResponse(content="ra"),
            _MockResponse(content="rb"),
            _MockResponse(content="synthesis"),
        ])
        p = PlanExecutePattern(transport=transport, nested_mode=None)
        graph = p.build(SubgraphRegistry())

        # If execute_node tried to call invoke_subgraph, this mock
        # would intercept and the test would fail differently.
        with patch("paradise.core.supervisor.invoke_subgraph") as mock_invoke:
            result = await graph.ainvoke({
                "handoff": HandoffRequest(
                    session_id="s", user_id="u", trace_id="t",
                    message="x", depth=1,
                ),
            })
            assert mock_invoke.call_count == 0

        resp = result["handoff_response"]
        # All steps went through transport.chat
        assert len(transport.calls) == 4  # plan + 2 steps + synth
        # No nested_mode markers in artifacts
        for sr in resp.artifacts["step_results"]:
            assert "nested_mode" not in sr
            assert "nested_fallback" not in sr

    @pytest.mark.asyncio
    async def test_nested_request_carries_parent_trace(self):
        """The nested HandoffRequest must carry parent_trace_id and a
        step-suffixed trace_id so observability can stitch the chain."""
        from paradise.core.handoff import HandoffResponse
        from unittest.mock import patch

        transport = _ScriptedTransport([
            _MockResponse(content=json.dumps({"steps": ["only step"]})),
            _MockResponse(content="synthesis"),
        ])
        p = PlanExecutePattern(transport=transport, nested_mode="tool_react")
        graph = p.build(SubgraphRegistry())

        nested_resp = _NestedSubgraphResponse([
            HandoffResponse(session_id="s", trace_id="t", output="ok"),
        ])
        with patch(
            "paradise.core.supervisor.invoke_subgraph",
            new=nested_resp,
        ):
            await graph.ainvoke({
                "handoff": HandoffRequest(
                    session_id="sess-1", user_id="u1", trace_id="trace-abc",
                    message="x", depth=1,
                ),
            })

        call = nested_resp.calls[0]
        nested_req = call["req"]
        assert nested_req.depth == 2
        assert nested_req.parent_trace_id == "trace-abc"
        assert "trace-abc" in nested_req.trace_id  # suffixed with :step1
        assert nested_req.session_id == "sess-1"
        assert nested_req.context.get("parent_step") == 0
