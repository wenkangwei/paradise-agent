"""PlanExecutePattern — Planner + sequential step executor.

Three-node graph:
    plan_node → execute_node (self-loops over remaining steps) → synthesize_node → END

  * plan_node:        one LLM call that decomposes the user message into
                      a JSON list of step descriptions. JSON parse
                      failures fall back to a single-step plan
                      (plan_used=False), so the pattern still produces
                      output.
  * execute_node:     one LLM call per step. Step results accumulate in
                      state.step_results. Self-loops until steps are
                      exhausted or max_turns is hit (HANDOFF_MAX_TURNS).
  * synthesize_node:  final LLM call that merges step results into a
                      single coherent answer.

Phase 2.12 nesting:
  When constructed with `nested_mode=<pattern_name>` (typically
  "tool_react"), execute_node delegates each step to that subgraph via
  the Handoff Protocol instead of calling transport.chat directly. The
  nested subgraph runs at depth=req.depth+1; supervisor.invoke_subgraph
  enforces _MAX_DEPTH=2, so a plan_execute running at depth=1 can
  spawn depth=2 sub-tasks but those sub-tasks cannot recurse further.

  Fail-soft: if nested invocation returns HANDOFF_DEPTH_CAPPED or
  HANDOFF_ERROR, execute_node falls back to transport.chat for that
  step (artifacts.step_results[i].nested_fallback=True) so the user
  sees partial progress rather than an abort.

Why a synthesize_node at the end instead of just concatenating steps:
  * LLM-produced prose often repeats itself across steps; synthesis
    de-duplicates and reflows into natural language.
  * The synthesis call has access to ALL step_results at once, so it
    can spot cross-step patterns (e.g., step 2 contradicts step 1).
  * artifacts.step_results preserves the raw per-step output for ATIF
    export / debugging — synthesis is purely for user-facing polish.

Phase 2.11 / 2.12.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from langgraph.graph import END, StateGraph

from paradise.core.handoff import (
    HANDOFF_ERROR,
    HANDOFF_MAX_TURNS,
    HANDOFF_OK,
    HandoffRequest,
    HandoffResponse,
)
from paradise.core.patterns.base import SubgraphPattern

if TYPE_CHECKING:
    from paradise.config import LLMConfig
    from paradise.core.registry import SubgraphRegistry

logger = logging.getLogger("paradise.patterns.plan_execute")


# Planner prompt — biases toward small step counts. The "if the request
# is genuinely single-step, output a 1-element list" clause is critical:
# without it the planner over-decomposes trivial requests ("hi" → 3 steps).
_PLANNER_SYSTEM_PROMPT = (
    "You decompose a user request into a small ordered list of steps. "
    "Output ONLY a JSON object: {\"steps\": [\"step 1\", \"step 2\", ...]}. "
    "Rules:\n"
    "- Each step is a single concrete sub-task (1 sentence).\n"
    "- 1 to 4 steps. If the request is genuinely simple, output a "
    "1-element list.\n"
    "- Do NOT include a final 'summarize' step — synthesis is automatic.\n"
    "- No commentary, no markdown, just the JSON."
)

# Step executor prompt — runs once per step. Carries prior step results
# so the LLM can build on them (avoids re-asking the same sub-question).
_STEP_EXECUTOR_SYSTEM_PROMPT = (
    "You are executing one step of a larger plan. Given the original "
    "request, the full plan, and the results of prior steps, produce "
    "a concise answer for YOUR step only. Do not anticipate future steps."
)

# Synthesis prompt — final pass over all step results.
_SYNTHESIS_SYSTEM_PROMPT = (
    "You merge a sequence of step results into a single coherent answer "
    "for the user. Preserve all factual content from the steps; remove "
    "redundancy; reflow into natural prose. Do not invent new information."
)


class PlanExecuteState(TypedDict, total=False):
    """Plan-Execute subgraph state.

    Fields:
        handoff:          incoming request.
        plan_steps:       list of step description strings. Set by
                          plan_node. Empty list = plan failed, single
                          ad-hoc step will be used.
        step_results:     list of {step: str, output: str, error: str?}
                          dicts. Appended one per execute_node call.
        iterations:       execute_node invocations completed. Bounded
                          by pattern.max_turns - 1 (plan + synth don't
                          count against execute budget).
        plan_used:        True iff plan_node produced a real multi-step
                          plan. False when JSON parse failed and we
                          fell back to a single-step plan.
        handoff_response: emitted by synthesize_node.
    """
    handoff: HandoffRequest
    plan_steps: list[str]
    step_results: list[dict]
    iterations: int
    plan_used: bool
    handoff_response: HandoffResponse


class PlanExecutePattern(SubgraphPattern):
    """Plan-then-Execute with bounded step count."""

    name: ClassVar[str] = "plan_execute"
    description: ClassVar[str] = (
        "Plan-then-Execute — 多步任务 / '先 X 再 Y'"
    )
    category: ClassVar[str] = "basic"
    cost_budget_usd: ClassVar[float] = 0.10
    max_turns: ClassVar[int] = 5

    def __init__(
        self,
        transport: Any | None = None,
        llm_config: "LLMConfig | None" = None,
        nested_mode: str | None = None,
    ) -> None:
        """
        Args:
            transport: LLM transport. None → lazy resolve via factory.
            llm_config: model config. None → lazy resolve.
            nested_mode: Phase 2.12 — when set (typically "tool_react"),
                execute_node delegates each step to this subgraph via
                the Handoff Protocol + invoke_subgraph, instead of
                calling transport.chat directly. The nested subgraph
                runs at depth+1, bounded by supervisor._MAX_DEPTH.
                None preserves Phase 2.11 behavior (flat chat per step).
        """
        self._transport = transport
        self._llm_config = llm_config
        self._nested_mode = nested_mode
        self._registry: "SubgraphRegistry | None" = None

    def build(self, registry: "SubgraphRegistry") -> Any:
        """Compile the plan-execute-synthesize graph.

        Saves the registry reference so execute_node can invoke nested
        subgraphs when nested_mode is set. The reference is read-only —
        patterns must not mutate the registry mid-flight.
        """
        self._registry = registry
        pattern = self

        # ── Node: PLAN ───────────────────────────────────────────────
        async def plan_node(state: PlanExecuteState) -> dict:
            """One LLM call to produce the step list.

            Fail-soft: JSON parse failure, transport failure, or empty
            steps list all collapse to a single ad-hoc step equal to
            the original message. The pattern still runs end-to-end,
            just without the planning benefit.
            """
            req: HandoffRequest = state["handoff"]
            try:
                transport = pattern._resolve_transport()
                cfg = pattern._resolve_llm_config()
                kwargs = pattern._build_planner_kwargs(cfg, req)
                result = await transport.chat(**kwargs)
                content = (
                    result.content if hasattr(result, "content")
                    else str(result)
                )
                steps = _parse_plan_json(content)
            except Exception as exc:
                logger.warning(
                    "plan_execute planner failed (%s) — single-step fallback "
                    "trace=%s",
                    exc, req.trace_id,
                )
                return {
                    "plan_steps": [req.message],
                    "plan_used": False,
                    "step_results": [],
                    "iterations": 0,
                }

            if not steps:
                logger.debug(
                    "plan_execute: planner returned 0 steps → fallback trace=%s",
                    req.trace_id,
                )
                return {
                    "plan_steps": [req.message],
                    "plan_used": False,
                    "step_results": [],
                    "iterations": 0,
                }

            logger.debug(
                "plan_execute: %d steps trace=%s plan_used=True",
                len(steps), req.trace_id,
            )
            return {
                "plan_steps": steps,
                "plan_used": True,
                "step_results": [],
                "iterations": 0,
            }

        # ── Node: EXECUTE (self-loop) ────────────────────────────────
        async def execute_node(state: PlanExecuteState) -> dict:
            """Run the next pending step. Self-loops via should_continue.

            Phase 2.12: when pattern._nested_mode is set, delegates the
            step to that subgraph via invoke_subgraph (depth+1). On any
            nested failure (depth_capped, error, exception), falls back
            to a flat transport.chat call for the step so the plan
            continues to make progress.
            """
            req: HandoffRequest = state["handoff"]
            steps: list[str] = list(state.get("plan_steps") or [])
            results: list[dict] = list(state.get("step_results") or [])
            iterations: int = state.get("iterations", 0)

            # Bounded loop exit: leave room for the final synthesis call.
            # max_turns=5 → up to 3 step executions + 1 plan + 1 synth.
            max_executions = max(1, pattern.max_turns - 2)
            if iterations >= max_executions or iterations >= len(steps):
                return {}  # nothing to do — should_continue will route out

            current_step = steps[iterations]

            # ── Nested path: delegate step to a subgraph ─────────────
            if pattern._nested_mode and pattern._registry is not None:
                content, nested_fallback, nested_error = await pattern._run_nested_step(
                    req, current_step, iterations, results,
                )
                if not nested_fallback and not nested_error:
                    # Nested call succeeded — record and continue.
                    results.append({
                        "step": current_step,
                        "output": content,
                        "nested_mode": pattern._nested_mode,
                    })
                    return {
                        "step_results": results,
                        "iterations": iterations + 1,
                    }
                # Else: fall through to flat chat (nested_fallback path).
                # The fall-through is intentional — we still want to
                # produce *some* output for this step.
                logger.info(
                    "plan_execute step %d nested=%s fell back to chat "
                    "(reason=%s) trace=%s",
                    iterations + 1, pattern._nested_mode,
                    "depth_capped" if nested_fallback else "error",
                    req.trace_id,
                )

            # ── Flat path: direct transport.chat ─────────────────────
            try:
                transport = pattern._resolve_transport()
                cfg = pattern._resolve_llm_config()
                kwargs = pattern._build_step_kwargs(
                    cfg, req, steps, results, current_step,
                )
                result = await transport.chat(**kwargs)
                content = (
                    result.content if hasattr(result, "content")
                    else str(result)
                )
                step_entry: dict[str, Any] = {"step": current_step, "output": content}
                if pattern._nested_mode:
                    # Mark that we intended nested but had to fall back.
                    step_entry["nested_fallback"] = True
                results.append(step_entry)
            except Exception as exc:
                # Step failure does NOT crash the loop — record the error
                # and continue so the user sees partial progress. Final
                # synthesis will work around gaps.
                logger.warning(
                    "plan_execute step %d failed (%s) — continuing trace=%s",
                    iterations + 1, exc, req.trace_id,
                )
                results.append({
                    "step": current_step,
                    "output": "",
                    "error": f"{type(exc).__name__}: {exc}",
                })

            return {"step_results": results, "iterations": iterations + 1}

        # ── Node: SYNTHESIZE ─────────────────────────────────────────
        async def synthesize_node(state: PlanExecuteState) -> dict:
            """Merge step_results into final output via one LLM call."""
            req: HandoffRequest = state["handoff"]
            results: list[dict] = list(state.get("step_results") or [])
            plan_used: bool = state.get("plan_used", False)
            iterations: int = state.get("iterations", 0)

            # If we somehow hit max_turns before completing all steps,
            # surface that in the status (but still synthesize what we have).
            completed_all = iterations >= len(state.get("plan_steps") or [])
            status = HANDOFF_OK if completed_all else HANDOFF_MAX_TURNS

            try:
                transport = pattern._resolve_transport()
                cfg = pattern._resolve_llm_config()
                kwargs = pattern._build_synthesis_kwargs(cfg, req, results)
                result = await transport.chat(**kwargs)
                content = (
                    result.content if hasattr(result, "content")
                    else str(result)
                )
            except Exception as exc:
                logger.exception(
                    "plan_execute synthesis failed trace=%s", req.trace_id,
                )
                return {"handoff_response": HandoffResponse(
                    session_id=req.session_id,
                    trace_id=req.trace_id,
                    output="",
                    status=HANDOFF_ERROR,
                    error=f"synthesis failed: {type(exc).__name__}: {exc}",
                    artifacts={
                        "plan_steps": state.get("plan_steps") or [],
                        "step_results": results,
                        "plan_used": plan_used,
                    },
                )}

            logger.debug(
                "plan_execute synthesized trace=%s steps=%d plan_used=%s",
                req.trace_id, len(results), plan_used,
            )
            return {"handoff_response": HandoffResponse(
                session_id=req.session_id,
                trace_id=req.trace_id,
                output=content,
                status=status,
                turns_used=iterations + (1 if plan_used else 0) + 1,
                artifacts={
                    "plan_steps": state.get("plan_steps") or [],
                    "step_results": results,
                    "plan_used": plan_used,
                },
            )}

        # ── Edges ────────────────────────────────────────────────────
        def should_continue_after_execute(state: PlanExecuteState) -> str:
            """Loop until steps exhausted or budget hit."""
            steps: list[str] = state.get("plan_steps") or []
            iterations: int = state.get("iterations", 0)
            max_executions = max(1, pattern.max_turns - 2)
            if iterations >= len(steps) or iterations >= max_executions:
                return "synthesize"
            return "execute"

        graph = StateGraph(PlanExecuteState)
        graph.add_node("plan", plan_node)
        graph.add_node("execute", execute_node)
        graph.add_node("synthesize", synthesize_node)
        graph.set_entry_point("plan")
        graph.add_edge("plan", "execute")
        graph.add_conditional_edges(
            "execute",
            should_continue_after_execute,
            {"execute": "execute", "synthesize": "synthesize"},
        )
        graph.add_edge("synthesize", END)
        return graph.compile()

    # ── Lazy resolution helpers ───────────────────────────────────────

    async def _run_nested_step(
        self,
        req: HandoffRequest,
        current_step: str,
        iteration: int,
        prior_results: list[dict],
    ) -> tuple[str, bool, bool]:
        """Delegate one step to the nested subgraph via invoke_subgraph.

        Phase 3-D: passes FOCUSED context to the nested sub-agent — the
        overall task goal, prior step results, and persona — but NOT the
        full conversation history. This implements the multi-agent pattern:
        each nested sub-agent focuses only on its assigned step.

        Returns:
            (content, nested_fallback, nested_error)
              content:         output text from the nested subgraph
                               (empty on failure)
              nested_fallback: True if depth cap forced a chat fallback
              nested_error:    True if the nested call raised/errored

        Caller (execute_node) uses these flags to decide whether to
        append a "clean nested" result vs. fall through to flat chat.
        Both flags False → success path; either True → retry as chat.
        """
        # Lazy import — keeps the module importable in test envs that
        # don't need the supervisor (e.g., when nested_mode is None).
        from paradise.core.supervisor import invoke_subgraph

        # Extract prior step outputs for focused context
        prior_outputs = [
            r.get("output", "") for r in (prior_results or [])
            if r.get("output")
        ]

        nested_req = HandoffRequest(
            session_id=req.session_id,
            user_id=req.user_id,
            trace_id=f"{req.trace_id}:step{iteration + 1}",
            user_tier=req.user_tier,
            message=current_step,
            context={
                "parent_step": iteration,
                "parent_trace_id": req.trace_id,
                # Phase 3-D: focused sub-agent context
                "task_goal": (req.context or {}).get("task_goal", req.message),
                "prior_results": prior_outputs,
                "soul_md": (req.context or {}).get("soul_md", ""),
                "focused_mode": True,  # signal to context_builder
            },
            depth=req.depth + 1,  # depth=1 → nested depth=2
            parent_trace_id=req.trace_id,
        )
        try:
            response = await invoke_subgraph(
                self._registry, self._nested_mode, nested_req,  # type: ignore[arg-type]
            )
        except Exception as exc:
            logger.warning(
                "plan_execute nested invoke_subgraph raised (%s) — fallback",
                exc,
            )
            return "", False, True

        # Depth cap → fall back to chat (no retry into the same wall).
        if response.status == "depth_capped":
            return "", True, False

        # Any other non-OK status → fall back to chat.
        if response.status != HANDOFF_OK:
            logger.warning(
                "plan_execute nested step status=%s err=%s — fallback",
                response.status, response.error,
            )
            return "", False, True

        return response.output, False, False

    def _resolve_transport(self) -> Any:
        if self._transport is not None:
            return self._transport
        from paradise.factory import build_transport
        from paradise.config import ParadiseConfig
        return build_transport(ParadiseConfig())

    def _resolve_llm_config(self) -> "LLMConfig":
        if self._llm_config is not None:
            return self._llm_config
        from paradise.config import ParadiseConfig
        return ParadiseConfig().llm

    # ── Per-node kwargs builders (separate for testability) ───────────

    def _build_planner_kwargs(
        self,
        cfg: "LLMConfig",
        req: HandoffRequest,
    ) -> dict:
        from paradise.core.patterns.context_builder import (
            build_subgraph_system_prompt,
        )
        system_prompt = build_subgraph_system_prompt(req, _PLANNER_SYSTEM_PROMPT)
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": system_prompt,
            "messages": [{"role": "user", "content": req.message}],
            "temperature": 0.0,  # deterministic planning
        }
        self._add_provider_kwargs(kwargs, cfg)
        return kwargs

    def _build_step_kwargs(
        self,
        cfg: "LLMConfig",
        req: HandoffRequest,
        steps: list[str],
        prior_results: list[dict],
        current_step: str,
    ) -> dict:
        """Build kwargs for one step execution call.

        The user message carries the current step + index; system prompt
        carries the original request + plan + prior results so the LLM
        has full context without re-asking.

        Phase 3-D: persona injected via context_builder.
        """
        from paradise.core.patterns.context_builder import (
            build_subgraph_system_prompt,
        )
        system_prompt = build_subgraph_system_prompt(
            req, _STEP_EXECUTOR_SYSTEM_PROMPT,
        )

        plan_str = "\n".join(
            f"{i+1}. {s}" for i, s in enumerate(steps)
        )
        prior_str = ""
        if prior_results:
            prior_str = "\n\nPrior step results:\n" + "\n".join(
                f"[step {i+1}] {r.get('output') or r.get('error', '')}"
                for i, r in enumerate(prior_results)
            )

        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Original request: {req.message}\n\n"
                        f"Full plan:\n{plan_str}{prior_str}\n\n"
                        f"Your step: {current_step}"
                    ),
                }
            ],
            "temperature": 0.3,
        }
        self._add_provider_kwargs(kwargs, cfg)
        return kwargs

    def _build_synthesis_kwargs(
        self,
        cfg: "LLMConfig",
        req: HandoffRequest,
        results: list[dict],
    ) -> dict:
        results_str = "\n\n".join(
            f"[step {i+1}: {r.get('step', '')}]\n"
            f"{r.get('output') or r.get('error', '(no output)')}"
            for i, r in enumerate(results)
        )
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": _SYNTHESIS_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Original request: {req.message}\n\n"
                        f"Step results:\n{results_str}"
                    ),
                }
            ],
            "temperature": 0.5,  # mild creativity for reflow
        }
        self._add_provider_kwargs(kwargs, cfg)
        return kwargs

    def _add_provider_kwargs(self, kwargs: dict, cfg: "LLMConfig") -> None:
        """Mutates kwargs in place — centralizes provider-specific logic."""
        transport = self._resolve_transport()
        if not hasattr(transport, "api_mode"):
            return
        mode = transport.api_mode
        if mode == "ollama_native":
            kwargs["api_url"] = cfg.api_url
        else:
            kwargs["api_url"] = cfg.api_url
            kwargs["api_key"] = cfg.api_key
            kwargs["max_tokens"] = cfg.max_tokens or 512


# ── Module-level helpers ──────────────────────────────────────────────


def _parse_plan_json(text: str) -> list[str]:
    """Extract a steps list from the planner LLM's output.

    Lenient JSON parsing — strips markdown fences, finds the first
    {...} block. Returns [] on any parse failure (caller falls back
    to single-step plan).

    Mirrors intent/llm.py::_parse_intent_json and mode_judge.py::
    _parse_mode_json. A future refactor could share this helper, but
    three near-identical 10-line functions is below the bar for a
    shared utility module.
    """
    text = text.strip().strip("`")
    if not text.startswith("{"):
        lo = text.find("{")
        hi = text.rfind("}")
        if lo == -1 or hi == -1 or hi <= lo:
            return []
        text = text[lo:hi + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return []
    steps = obj.get("steps")
    if not isinstance(steps, list):
        return []
    # Coerce to strings; filter empties; cap at 4 (planner prompt says 1-4).
    cleaned = [str(s).strip() for s in steps if str(s).strip()]
    return cleaned[:4]


__all__ = ["PlanExecutePattern", "PlanExecuteState"]
