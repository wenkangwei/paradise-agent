"""QueryPreprocessor — query correction + parallel rewriting.

Implements the first stage of the reference agent pipeline
(agent-flow.png):

    用户输入 → 1. query纠正  2. 并行query改写(N≤3) → 意图识别 → ...

One LLM call performs BOTH correction and rewriting (single round-trip).
Fail-soft everywhere: any exception returns the original message with
empty rewrites so the supervisor falls through to normal intent flow.

Heuristic gate: short messages (< 5 chars) and short ASCII messages
(< 20 chars) skip the LLM call entirely — they're almost never worth
correcting, and skipping keeps the hot path (greetings, one-word
commands) at zero added latency.

Design note: rewrites are stored on SupervisorState and passed to
subgraphs via HandoffRequest.context["rewritten_queries"]. Patterns
that benefit from multi-query coverage (rag, agent_team compose) can
consume them; patterns that don't simply ignore them.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("paradise.preprocess")

_PREPROCESS_SYSTEM_PROMPT = (
    "You are a query preprocessing assistant. Given a user message:\n"
    "1. Correct typos, grammar errors, and unclear phrasing\n"
    "2. Generate up to {max_rewrites} alternative rewrites that preserve "
    "the original intent but use different wording\n\n"
    "Output ONLY JSON:\n"
    '{{"corrected": "...", "rewrites": ["...", "..."]}}\n\n'
    "Rules:\n"
    "- Same language as the input\n"
    "- If the original is already clear and correct, \"corrected\" must "
    "equal the original exactly\n"
    "- Rewrites must NOT change the intent — only rephrase\n"
    "- 0 to {max_rewrites} rewrites; omit the key or use [] if none\n"
    "- No commentary, no markdown, just the JSON"
)

# Messages shorter than this skip the LLM call (greetings, one-worders).
_MIN_LLM_LENGTH = 5
# Short ASCII messages (English one-liners) also skip — typo correction
# on "what time is it" adds latency without value.
_MIN_ASCII_LLM_LENGTH = 20


@dataclass
class PreprocessResult:
    """Outcome of one preprocess() call.

    Attributes:
        corrected: typo-fixed version of the original. Equals the
            original when no correction was needed (or on LLM failure).
        rewrites: ≤ max_rewrites alternative phrasings. Empty on skip
            or failure — never None.
        effective: the query downstream nodes should use for intent
            classification. Currently == corrected (kept as a separate
            field so future scoring logic can pick a rewrite instead
            without touching call sites).
        skipped: True when the heuristic gate bypassed the LLM.
    """
    corrected: str = ""
    rewrites: list[str] = field(default_factory=list)
    effective: str = ""
    skipped: bool = False


def should_preprocess(message: str) -> bool:
    """Heuristic gate — True when the message is worth an LLM pass.

    Skips (returns False):
      * empty / whitespace-only
      * < 5 characters (any script) — "hi", "你好", "ok"
      * pure-ASCII and < 20 characters — "what time is it"
    """
    msg = (message or "").strip()
    if not msg:
        return False
    if len(msg) < _MIN_LLM_LENGTH:
        return False
    if msg.isascii() and len(msg) < _MIN_ASCII_LLM_LENGTH:
        return False
    return True


class QueryPreprocessor:
    """One-shot query corrector + rewriter backed by a shared transport.

    Construction is cheap (no I/O); the LLM call happens per process().
    Thread-safe: stateless beyond the injected transport/config.
    """

    def __init__(
        self,
        transport: Any,
        llm_config: Any,
        max_rewrites: int = 2,
    ) -> None:
        self._transport = transport
        self._llm_config = llm_config
        self._max_rewrites = max(0, min(max_rewrites, 3))  # plan cap: N≤3

    async def process(self, message: str) -> PreprocessResult:
        """Correct + rewrite a user message. Never raises.

        Fail-soft: returns PreprocessResult(corrected=message,
        effective=message) on any LLM/parse failure so the caller
        (supervisor preprocess_node) can pass through unchanged.
        """
        if not should_preprocess(message):
            return PreprocessResult(
                corrected=message,
                rewrites=[],
                effective=message,
                skipped=True,
            )

        try:
            result = await self._transport.chat(**self._build_kwargs(message))
            content = (
                result.content if hasattr(result, "content") else str(result)
            )
            corrected, rewrites = _parse_preprocess_json(
                content, message, self._max_rewrites,
            )
            return PreprocessResult(
                corrected=corrected,
                rewrites=rewrites,
                effective=corrected,
            )
        except Exception as exc:
            logger.warning(
                "query preprocess LLM call failed (%s) — using original",
                exc,
            )
            return PreprocessResult(
                corrected=message,
                rewrites=[],
                effective=message,
            )

    # ── Internal ─────────────────────────────────────────────────────

    def _build_kwargs(self, message: str) -> dict:
        cfg = self._llm_config
        system_prompt = _PREPROCESS_SYSTEM_PROMPT.format(
            max_rewrites=self._max_rewrites,
        )
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": system_prompt,
            "messages": [{"role": "user", "content": message}],
            "temperature": 0.1,  # deterministic correction
        }
        if hasattr(self._transport, "api_mode"):
            mode = self._transport.api_mode
            if mode == "ollama_native":
                kwargs["api_url"] = cfg.api_url
            else:
                kwargs["api_url"] = cfg.api_url
                kwargs["api_key"] = cfg.api_key
                kwargs["max_tokens"] = cfg.max_tokens or 400
        return kwargs


def _parse_preprocess_json(
    content: str,
    original: str,
    max_rewrites: int,
) -> tuple[str, list[str]]:
    """Parse '{"corrected": ..., "rewrites": [...]}' from LLM output.

    Lenient: strips markdown fences, finds the first {...} block.
    Returns (original, []) on any parse failure — caller treats it as
    "no correction needed".
    """
    text = (content or "").strip().strip("`")
    if not text.startswith("{"):
        lo = text.find("{")
        hi = text.rfind("}")
        if lo == -1 or hi == -1 or hi <= lo:
            return original, []
        text = text[lo:hi + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("preprocess JSON parse failed: %r", content[:120])
        return original, []

    corrected = obj.get("corrected")
    if not isinstance(corrected, str) or not corrected.strip():
        corrected = original
    corrected = corrected.strip()
    # Guard against the LLM "correcting" into a different intent via
    # a wildly different length — trust the original in that case.
    if original.strip() and len(corrected) > 3 * max(len(original), 8):
        corrected = original

    raw_rewrites = obj.get("rewrites")
    rewrites: list[str] = []
    if isinstance(raw_rewrites, list):
        for r in raw_rewrites:
            if isinstance(r, str) and r.strip():
                rewrites.append(r.strip())
            if len(rewrites) >= max_rewrites:
                break

    return corrected, rewrites


__all__ = [
    "PreprocessResult",
    "QueryPreprocessor",
    "should_preprocess",
]
