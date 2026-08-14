"""ComplexityAssessor — binary simple/complex query classification.

Implements the "是否复杂意图?" decision diamond from the reference
agent pipeline (agent-flow.png):

    意图识别 → 是否复杂意图?
      ├─ 否 (simple) → RAG / tool agent (直接返回)
      └─ 是 (complex) → thinking → Action → agent编排 subflow

Complexity definition (from the reference diagram):
    complex = 有具体目标 + 需要多步拆解执行 + 或要求精度高

Two-tier assessment:

  L1 Rules (0ms, no LLM):
    * intent == "task_complex"            → complex
    * intent in {chitchat, creative}      → simple
    * long message + sequencing conjunctions (然后/接着/之后/先...再) → complex
    * multiple question marks (≥2)        → complex

  L2 LLM fallback (only when rules are inconclusive):
    One small-LLM binary classification with a tight timeout.
    Fail-soft: timeout / error / unparseable → simple (default).

The assessor never raises. It returns ComplexityResult with a source
field ("rule" | "llm" | "default") so observability can tell which
tier made the call.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("paradise.complexity")

_COMPLEXITY_SYSTEM_PROMPT = (
    "You classify user requests by complexity for an agent router.\n"
    "COMPLEX means: has a concrete goal, needs multi-step decomposition, "
    "requires multiple roles/perspectives, or demands high precision.\n"
    "SIMPLE means: a single direct answer, one tool call, or casual chat "
    "can fully satisfy it.\n\n"
    "Output ONLY JSON: {\"complex\": true} or {\"complex\": false}"
)

# Intents that map to a definite complexity — no LLM needed.
_SIMPLE_INTENTS = {"chitchat", "creative"}
_COMPLEX_INTENTS = {"task_complex"}

# Sequencing / dependency conjunctions (zh + en) that signal multi-step.
_SEQUENCING_MARKERS = (
    "然后", "接着", "之后", "先", "再", "其次", "最后", "其次",
    "then", "after that", "next", "first", "finally", "step by step",
)

# Long messages containing a sequencing marker are complex.
_LONG_MESSAGE_THRESHOLD = 50

# LLM fallback timeout — complexity routing must never block the turn.
_LLM_TIMEOUT_SECONDS = 3.0


@dataclass
class ComplexityResult:
    """Outcome of one assess() call.

    Attributes:
        is_complex: the binary decision. Defaults False (simple) on
            any failure — a false "simple" degrades to the existing
            intent-based routing, a false "complex" wastes an
            expensive orchestration, so simple is the safe default.
        reason: short human-readable justification (observability).
        source: which tier decided — "rule", "llm", or "default"
            (LLM attempted but failed → default simple).
    """
    is_complex: bool
    reason: str
    source: str


class ComplexityAssessor:
    """Rule-first, LLM-fallback complexity classifier.

    Construction is cheap; the transport is only hit when L1 rules
    are inconclusive. Stateless — safe to share across turns.
    """

    def __init__(self, transport: Any = None, llm_config: Any = None) -> None:
        self._transport = transport
        self._llm_config = llm_config

    async def assess(self, message: str, intent: str) -> ComplexityResult:
        """Classify a (message, intent) pair. Never raises.

        Args:
            message: the (already corrected) user message.
            intent: the intent label from intent_node (str value).
        """
        # ── L1: Rules ───────────────────────────────────────────────
        rule_result = self._apply_rules(message, intent)
        if rule_result is not None:
            return rule_result

        # ── L2: LLM fallback ────────────────────────────────────────
        if self._transport is None or self._llm_config is None:
            return ComplexityResult(
                is_complex=False,
                reason="rules inconclusive, no LLM wired",
                source="default",
            )
        return await self._assess_via_llm(message)

    # ── L1 rules ────────────────────────────────────────────────────

    def _apply_rules(self, message: str, intent: str) -> ComplexityResult | None:
        """Return a definitive result, or None when inconclusive."""
        msg = (message or "").strip()

        if intent in _COMPLEX_INTENTS:
            return ComplexityResult(
                is_complex=True,
                reason=f"intent={intent} is complex by definition",
                source="rule",
            )
        if intent in _SIMPLE_INTENTS and "?" not in msg and "？" not in msg:
            return ComplexityResult(
                is_complex=False,
                reason=f"intent={intent} is simple by definition",
                source="rule",
            )

        # Multiple question marks → multiple sub-questions → complex.
        q_count = msg.count("?") + msg.count("？")
        if q_count >= 2:
            return ComplexityResult(
                is_complex=True,
                reason=f"{q_count} questions in one message",
                source="rule",
            )

        # Long + sequencing marker → multi-step pipeline → complex.
        # Short messages with "然后" are usually one clarifying step,
        # not a real pipeline — require the length gate.
        if len(msg) >= _LONG_MESSAGE_THRESHOLD:
            lowered = msg.lower()
            for marker in _SEQUENCING_MARKERS:
                if marker in lowered:
                    return ComplexityResult(
                        is_complex=True,
                        reason=f"long message with sequencing marker '{marker}'",
                        source="rule",
                    )

        return None  # inconclusive → caller falls through to LLM

    # ── L2 LLM fallback ─────────────────────────────────────────────

    async def _assess_via_llm(self, message: str) -> ComplexityResult:
        try:
            result = await self._llm_call_with_timeout(message)
            content = (
                result.content if hasattr(result, "content") else str(result)
            )
            complex_flag = _parse_complexity_json(content)
            return ComplexityResult(
                is_complex=complex_flag,
                reason="llm classification",
                source="llm",
            )
        except TimeoutError:
            logger.warning(
                "complexity LLM timed out — defaulting to simple: %r",
                message[:60],
            )
        except Exception as exc:
            logger.warning(
                "complexity LLM failed (%s) — defaulting to simple", exc,
            )
        return ComplexityResult(
            is_complex=False,
            reason="llm unavailable, safe default",
            source="default",
        )

    async def _llm_call_with_timeout(self, message: str) -> Any:
        """Transport.chat wrapped in asyncio.timeout. Raises TimeoutError."""
        import asyncio

        kwargs = self._build_kwargs(message)
        async with asyncio.timeout(_LLM_TIMEOUT_SECONDS):
            return await self._transport.chat(**kwargs)

    def _build_kwargs(self, message: str) -> dict:
        cfg = self._llm_config
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": _COMPLEXITY_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": message}],
            "temperature": 0.0,
        }
        if hasattr(self._transport, "api_mode"):
            mode = self._transport.api_mode
            if mode == "ollama_native":
                kwargs["api_url"] = cfg.api_url
            else:
                kwargs["api_url"] = cfg.api_url
                kwargs["api_key"] = cfg.api_key
                kwargs["max_tokens"] = 20  # just true/false JSON
        return kwargs


def _parse_complexity_json(content: str) -> bool:
    """Parse '{"complex": true|false}'. Defaults False on parse failure."""
    text = (content or "").strip().strip("`")
    if not text.startswith("{"):
        lo = text.find("{")
        hi = text.rfind("}")
        if lo == -1 or hi == -1 or hi <= lo:
            return False
        text = text[lo:hi + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Some models answer bare "true"/"false" — handle that too.
        lowered = (content or "").strip().lower()
        if lowered.startswith("true"):
            return True
        return False
    val = obj.get("complex")
    return bool(val) if isinstance(val, bool) else False


__all__ = ["ComplexityResult", "ComplexityAssessor"]
