"""JudgePattern — LLM-as-judge quality-gated response generation.

An independent SubgraphPattern (like ChatPattern, ToolReactPattern) that
wraps response generation with a quality-evaluation step:

    1. Generate a response to the user message (single LLM call)
    2. Call a small judge LLM to score the response 0.0-1.0
    3. If score < threshold, regenerate (up to max_retries)
    4. Return the best response + judge score in artifacts

This pattern is NOT registered by default. It is registered only when
``config.reflection.judge_enabled=True`` (typically when harvesting
training data). See ``register_judge_pattern()`` in patterns/__init__.py.

Design choices:
  * Two-node graph (generate → judge) with a self-loop for retries.
    Cleaner than a single node because the generation and evaluation
    prompts are fundamentally different concerns.
  * Judge LLM can be a smaller/cheaper model than the generator.
    Pass a separate ``judge_transport`` to use a different model;
    when None, reuses the generator transport.
  * Best-of-N: on retry, the judge scores the NEW response and keeps
    whichever (old or new) scored higher. This avoids quality
    regression from a bad retry.
  * Fail-soft: if the judge LLM call itself fails, the response is
    returned as-is with score=0.0 and an error note in artifacts.
    Never blocks the user from getting an answer.

Phase 5 — Step 5.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from langgraph.graph import END, StateGraph

from paradise.core.handoff import (
    HANDOFF_ERROR,
    HANDOFF_OK,
    HandoffRequest,
    HandoffResponse,
)
from paradise.core.patterns.base import SubgraphPattern

if TYPE_CHECKING:
    from paradise.config import LLMConfig
    from paradise.core.registry import SubgraphRegistry

logger = logging.getLogger("paradise.patterns.judge")


_GENERATE_SYSTEM_PROMPT = "You are a helpful, concise assistant. Reply directly."

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict quality evaluator. Score the assistant's response "
    "on a scale of 0.0 to 1.0 based on: relevance, accuracy, completeness, "
    "and helpfulness. Respond in EXACTLY this format:\n"
    "SCORE: <float between 0.0 and 1.0>\n"
    "FEEDBACK: <one sentence explaining the score>\n\n"
    "Examples:\n"
    "SCORE: 0.9\nFEEDBACK: Directly answers the question with accurate detail.\n"
    "SCORE: 0.3\nFEEDBACK: Vague and doesn't address what was asked."
)


class JudgeState(TypedDict, total=False):
    """Judge subgraph state."""
    handoff: HandoffRequest
    current_output: str
    best_output: str
    best_score: float
    judge_score: float
    judge_feedback: str
    retry_count: int
    handoff_response: HandoffResponse


class JudgePattern(SubgraphPattern):
    """Response generation gated by LLM-as-judge quality scoring."""

    name: ClassVar[str] = "judge"
    description: ClassVar[str] = (
        "LLM-as-judge 质量评估 — 生成回复后评分，低分重试（训练数据采集时启用）"
    )
    category: ClassVar[str] = "basic"
    cost_budget_usd: ClassVar[float] = 0.08
    max_turns: ClassVar[int] = 3

    def __init__(
        self,
        transport: Any | None = None,
        llm_config: "LLMConfig | None" = None,
        judge_transport: Any | None = None,
        judge_llm_config: "LLMConfig | None" = None,
        threshold: float = 0.6,
        max_retries: int = 1,
    ) -> None:
        self._transport = transport
        self._llm_config = llm_config
        self._judge_transport = judge_transport
        self._judge_llm_config = judge_llm_config
        self._threshold = threshold
        self._max_retries = max_retries

    def build(self, registry: "SubgraphRegistry") -> Any:
        pattern = self

        async def generate_node(state: JudgeState) -> dict:
            req: HandoffRequest = state["handoff"]
            try:
                transport = pattern._resolve_transport()
                cfg = pattern._resolve_llm_config()
                kwargs = pattern._build_generate_kwargs(cfg, req)
                result = await transport.chat(**kwargs)
                content = (
                    result.content if hasattr(result, "content")
                    else str(result)
                )
                return {"current_output": content or ""}
            except Exception as exc:
                logger.exception(
                    "judge generate failed trace=%s", req.trace_id
                )
                return {
                    "handoff_response": HandoffResponse(
                        session_id=req.session_id,
                        trace_id=req.trace_id,
                        output=state.get("best_output", ""),
                        status=HANDOFF_ERROR,
                        error=f"generate: {type(exc).__name__}: {exc}",
                        artifacts={"judge_score": state.get("best_score", 0.0)},
                    )
                }

        async def judge_node(state: JudgeState) -> dict:
            req: HandoffRequest = state["handoff"]
            output = state.get("current_output", "")
            retry_count = state.get("retry_count", 0)
            best_output = state.get("best_output", "")
            best_score = state.get("best_score", 0.0)

            score, feedback = await pattern._judge(req, output)

            # Keep the best of (current, previous-best)
            if score >= best_score:
                best_output = output
                best_score = score

            if score >= pattern._threshold:
                return {
                    "best_output": best_output,
                    "best_score": best_score,
                    "judge_score": score,
                    "judge_feedback": feedback,
                    "retry_count": retry_count,
                    "handoff_response": HandoffResponse(
                        session_id=req.session_id,
                        trace_id=req.trace_id,
                        output=best_output,
                        status=HANDOFF_OK,
                        artifacts={
                            "judge_score": best_score,
                            "judge_feedback": feedback,
                        },
                    ),
                }

            # Score below threshold — retry if budget allows
            if retry_count >= pattern._max_retries:
                logger.info(
                    "judge score=%.2f below threshold=%.2f, retries exhausted "
                    "trace=%s — using best_output (score=%.2f)",
                    score, pattern._threshold, req.trace_id, best_score,
                )
                return {
                    "best_output": best_output,
                    "best_score": best_score,
                    "judge_score": score,
                    "judge_feedback": feedback,
                    "retry_count": retry_count,
                    "handoff_response": HandoffResponse(
                        session_id=req.session_id,
                        trace_id=req.trace_id,
                        output=best_output,
                        status=HANDOFF_OK,
                        artifacts={
                            "judge_score": best_score,
                            "judge_feedback": feedback,
                            "judge_below_threshold": True,
                        },
                    ),
                }

            logger.info(
                "judge score=%.2f below threshold=%.2f, retrying trace=%s",
                score, pattern._threshold, req.trace_id,
            )
            return {
                "best_output": best_output,
                "best_score": best_score,
                "judge_score": score,
                "judge_feedback": feedback,
                "retry_count": retry_count + 1,
            }

        def should_continue(state: JudgeState) -> str:
            if state.get("handoff_response") is not None:
                return END
            return "generate"

        graph = StateGraph(JudgeState)
        graph.add_node("generate", generate_node)
        graph.add_node("judge", judge_node)
        graph.set_entry_point("generate")
        graph.add_edge("generate", "judge")
        graph.add_conditional_edges(
            "judge",
            should_continue,
            {END: END, "generate": "generate"},
        )
        return graph.compile()

    # ── Internal helpers ──────────────────────────────────────────────

    async def _judge(
        self, req: HandoffRequest, response: str
    ) -> tuple[float, str]:
        """Score a response via the judge LLM. Returns (score, feedback).

        Fail-soft: returns (0.0, "") on any error so the caller can
        still return the generated response to the user.
        """
        if not response:
            return 0.0, "empty response"
        try:
            transport = self._resolve_judge_transport()
            cfg = self._resolve_judge_llm_config()
            kwargs = self._build_judge_kwargs(cfg, req, response)
            result = await transport.chat(**kwargs)
            content = (
                result.content if hasattr(result, "content")
                else str(result)
            )
            return _parse_judge_output(content)
        except Exception as exc:
            logger.warning(
                "judge LLM call failed trace=%s: %s — treating as score=0.0",
                req.trace_id, exc,
            )
            return 0.0, f"judge error: {exc}"

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

    def _resolve_judge_transport(self) -> Any:
        if self._judge_transport is not None:
            return self._judge_transport
        return self._resolve_transport()

    def _resolve_judge_llm_config(self) -> "LLMConfig":
        if self._judge_llm_config is not None:
            return self._judge_llm_config
        from paradise.config import ParadiseConfig
        return ParadiseConfig().reflection.llm

    def _build_generate_kwargs(
        self, cfg: "LLMConfig", req: HandoffRequest
    ) -> dict:
        from paradise.core.patterns.context_builder import (
            build_subgraph_system_prompt,
            extract_history_messages,
        )
        system_prompt = build_subgraph_system_prompt(req, _GENERATE_SYSTEM_PROMPT)
        history = extract_history_messages(req)
        messages = history + [{"role": "user", "content": req.message}]
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": system_prompt,
            "messages": messages,
            "temperature": 0.7,
        }
        transport = self._resolve_transport()
        if hasattr(transport, "api_mode"):
            if transport.api_mode == "ollama_native":
                kwargs["api_url"] = cfg.api_url
            else:
                kwargs["api_url"] = cfg.api_url
                kwargs["api_key"] = cfg.api_key
                kwargs["max_tokens"] = cfg.max_tokens or 512
        return kwargs

    def _build_judge_kwargs(
        self, cfg: "LLMConfig", req: HandoffRequest, response: str
    ) -> dict:
        messages = [
            {"role": "user", "content": (
                f"User asked: {req.message}\n\n"
                f"Assistant responded: {response}\n\n"
                f"Score the response."
            )},
        ]
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": _JUDGE_SYSTEM_PROMPT,
            "messages": messages,
            "temperature": 0.1,
        }
        transport = self._resolve_judge_transport()
        if hasattr(transport, "api_mode"):
            if transport.api_mode == "ollama_native":
                kwargs["api_url"] = cfg.api_url
            else:
                kwargs["api_url"] = cfg.api_url
                kwargs["api_key"] = cfg.api_key
                kwargs["max_tokens"] = cfg.max_tokens or 256
        return kwargs


# ── Module-level helpers ──────────────────────────────────────────────


def _parse_judge_output(content: str) -> tuple[float, str]:
    """Parse 'SCORE: 0.8\\nFEEDBACK: ...' into (float, str).

    Fail-soft: returns (0.5, content[:200]) when parsing fails so the
    pattern doesn't crash on malformed judge output.
    """
    score = 0.5
    feedback = ""
    for line in content.strip().split("\n"):
        line = line.strip()
        if line.upper().startswith("SCORE:"):
            try:
                val = float(line.split(":", 1)[1].strip())
                score = max(0.0, min(1.0, val))
            except ValueError:
                pass
        elif line.upper().startswith("FEEDBACK:"):
            feedback = line.split(":", 1)[1].strip()
    if not feedback:
        feedback = content.strip()[:200]
    return score, feedback


__all__ = ["JudgePattern", "JudgeState"]
