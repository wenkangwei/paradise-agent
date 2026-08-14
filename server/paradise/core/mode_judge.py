"""ModeJudge — parallel LLM judgment for execution mode.

Runs alongside the intent cascade in supervisor.intent_node. While the
intent classifier answers "what does the user want?", mode_judge answers
"which subgraph pattern should handle this?" — a subtle but useful
distinction:

  * Intent labels are generic (chitchat, tool_search, ...)
  * Mode labels are registry-specific (chat, tool_react, rag, ...)

The intent cascade is fast + cheap (rules → embedding → LLM cascade),
mode_judge is a single LLM call that sees the registry's actual pattern
descriptions. Fusing the two:

  * If mode_judge confidence > _CONFIDENCE_OVERRIDE → trust mode_judge
    (registry-aware, picks the best-matching pattern).
  * Else → use intent → mode mapping (fast, deterministic).

Fail-soft: returns None on network error / malformed JSON / unavailable
LLM / hallucinated mode. Supervisor falls back to intent-only routing.

Design parallels paradise.core.intent.llm.LLMStrategy:
  * Direct httpx call (no ResilientTransport — circuit breaker would
    multiply latency of an already-fast call).
  * Tight timeout (4s) so a dead ollama doesn't block the supervisor.
  * Few-shot examples bias toward the registry's actual mode names.
  * Hallucination guard: any mode not in registry → None.

Phase 2.8.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from paradise.core.registry import SubgraphRegistry

logger = logging.getLogger(__name__)


# Defaults — mirror intent/llm.py for consistency.
_DEFAULT_MODEL = "qwen2.5:0.5b"
_DEFAULT_TIMEOUT_S = 4.0

# When mode_judge confidence exceeds this threshold, its mode choice
# overrides the intent → mode mapping. Below it, intent-derived mode
# wins (cheaper, more deterministic). 0.7 is a deliberate cutoff: low
# enough that a confident LLM can correct a noisy rule-based intent,
# high enough that an uncertain LLM doesn't override a confident rule.
_CONFIDENCE_OVERRIDE = 0.7


@dataclass
class ModeJudgeResult:
    """Outcome of a ModeJudge.classify call.

    Fields mirror IntentResult for symmetry. `source` is always
    "llmjudge" (or "llmjudge:single" when short-circuited by a
    single-mode registry).
    """
    mode: str
    confidence: float
    source: str = "llmjudge"
    latency_ms: float = 0.0
    meta: dict = field(default_factory=dict)


_SYSTEM_PROMPT_TEMPLATE = """你是模式路由器。给定用户消息，从下列模式中选一个。
只输出一行 JSON: {{"mode": "...", "confidence": 0.x}}

{mode_list}

规则：
- confidence ∈ [0.0, 1.0]，越高越确定
- 只能选上面列出的模式之一
- 不确定时给低 confidence (< 0.5)，让上层用规则兜底
- 只输出 JSON，不要解释"""

_FEW_SHOT = [
    {"role": "user", "content": "你好啊"},
    {"role": "assistant", "content": '{"mode": "chat", "confidence": 0.95}'},
    {"role": "user", "content": "今天上海天气怎么样"},
    {"role": "assistant", "content": '{"mode": "tool_react", "confidence": 0.9}'},
]


class ModeJudge:
    """LLM-based execution mode classifier.

    Bypasses ResilientTransport on purpose — same rationale as
    IntentStrategy.LLMStrategy: circuit breaker / retry would multiply
    latency. We call ollama directly via httpx with a tight timeout;
    failures return None and the supervisor falls back to intent-only.

    Lifecycle:
        judge = ModeJudge(registry, base_url="http://localhost:11434")
        result = await judge.classify("weather?")  # → ModeJudgeResult | None

    The mode list is snapshotted at construction. If the registry is
    mutated after ModeJudge creation, the new modes are NOT visible to
    the judge. This is deliberate: SubgraphRegistry is documented as
    static post-startup (see registry.py docstring).
    """

    def __init__(
        self,
        registry: "SubgraphRegistry",
        base_url: str = "http://localhost:11434",
        model: str = _DEFAULT_MODEL,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        temperature: float = 0.0,
        max_tokens: int = 30,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._temperature = temperature
        self._max_tokens = max_tokens

        # Snapshot available modes + build prompt at construct time.
        # registry.list_modes() already filters stubs (availability=False).
        self._modes: list[str] = registry.list_modes(include_unavailable=False)
        self._mode_set: set[str] = set(self._modes)
        self._system_prompt: str = _SYSTEM_PROMPT_TEMPLATE.format(
            mode_list=registry.build_mode_prompt(),
        )

    async def classify(self, message: str) -> Optional[ModeJudgeResult]:
        """Classify message → execution mode.

        Returns:
            ModeJudgeResult on success.
            None when:
              * message is empty
              * registry has zero available modes (nothing to judge)
              * httpx not installed
              * LLM call fails (timeout, connection, HTTP error)
              * response is not valid JSON
              * LLM picks a mode not in the registry (hallucination guard)

        Never raises. All exceptions are caught and logged at debug.
        """
        if not message or not message.strip():
            return None

        # Fast path: only one mode available, no judgment needed.
        # Trusts the registry's filter — if only chat survived (e.g., all
        # tool patterns failed to compile), every request goes to chat.
        if len(self._modes) == 1:
            only = self._modes[0]
            logger.debug("mode_judge: single-mode registry → %s", only)
            return ModeJudgeResult(
                mode=only,
                confidence=1.0,
                source="llmjudge:single",
                latency_ms=0.0,
            )

        if not self._modes:
            # No available patterns at all — let supervisor fall back.
            logger.debug("mode_judge: no available modes → None")
            return None

        t0 = time.perf_counter()
        try:
            import httpx
        except ImportError:
            logger.debug("httpx missing — mode_judge disabled")
            return None

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                *_FEW_SHOT,
                {"role": "user", "content": message},
            ],
            "stream": False,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "options": {"num_predict": self._max_tokens},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.debug("mode_judge LLM miss (%s)", exc)
            return None

        text = (data.get("message") or {}).get("content", "").strip()
        latency_ms = (time.perf_counter() - t0) * 1000
        parsed = _parse_mode_json(text)
        if not parsed:
            logger.debug("mode_judge returned non-JSON: %r", text[:120])
            return None
        mode, conf = parsed

        # Hallucination guard: LLM may invent a mode not in registry.
        # The system prompt constrains it, but defense in depth.
        if mode not in self._mode_set:
            logger.debug(
                "mode_judge returned unknown mode %r (known: %s)",
                mode, sorted(self._mode_set),
            )
            return None

        return ModeJudgeResult(
            mode=mode,
            confidence=float(conf),
            source="llmjudge",
            latency_ms=latency_ms,
            meta={"raw": text[:80]},
        )


def _parse_mode_json(text: str) -> Optional[tuple[str, float]]:
    """Lenient JSON extractor — strips markdown fences, finds first {...}.

    Mirrors intent/llm.py::_parse_intent_json so both classifiers have
    identical failure modes (a future refactor could share this helper).
    """
    text = text.strip().strip("`")
    if not text.startswith("{"):
        lo = text.find("{")
        hi = text.rfind("}")
        if lo == -1 or hi == -1 or hi <= lo:
            return None
        text = text[lo:hi + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    mode = obj.get("mode")
    if not isinstance(mode, str) or not mode:
        return None
    try:
        conf = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        return None
    return mode, conf


__all__ = ["ModeJudge", "ModeJudgeResult"]
