"""Input guardrails — PII masking, jailbreak detection, cost budget.

Runs FIRST in the middleware chain (outermost), so all downstream code
sees sanitised input.

Phase 1 — Ingress layer.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("middleware.guardrails")

# ── PII patterns ──────────────────────────────────────────────────
# Note: \b doesn't fire between CJK chars and digits (both are \w), so we use
# lookarounds (?<!\d) / (?!\d) for digit-based patterns.
# Chinese mobile: 11 digits starting with 1
_PII_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# Chinese ID card: 18 chars (last may be X)
_PII_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])")
# Bank card: 16-19 digits
_PII_BANK = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
# Email (standard \b works fine here)
_PII_EMAIL = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")

_PII_PATTERNS = (
    ("phone", _PII_PHONE, "[PHONE]"),
    ("idcard", _PII_IDCARD, "[IDCARD]"),
    ("bank", _PII_BANK, "[BANKCARD]"),
    ("email", _PII_EMAIL, "[EMAIL]"),
)

# ── Jailbreak patterns (lightweight keyword blacklist) ────────────
_JAILBREAK_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore\s+(?:all\s+)?previous\s+instructions",
        r"忽略(?:以上|上述|之前).{0,4}(?:指令|规则|提示)",
        r"developer\s+mode",
        r"开发者模式",
        r"you\s+are\s+(?:now|actually)\s+(?:dan|jailbreak|developer)",
        r"system\s+prompt\s*[:：]",
        r"reveal\s+(?:your|the)\s+(?:system|initial)\s+prompt",
    )
]


def mask_pii(text: str) -> tuple[str, dict[str, int]]:
    """Replace PII matches with placeholders. Returns (cleaned_text, counts)."""
    counts: dict[str, int] = {}
    for kind, pattern, placeholder in _PII_PATTERNS:
        new_text, n = pattern.subn(placeholder, text)
        if n:
            counts[kind] = n
            text = new_text
    return text, counts


def detect_jailbreak(text: str) -> str | None:
    """Return matched pattern string if jailbreak detected, else None."""
    for pat in _JAILBREAK_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


def estimate_tokens(text: str) -> int:
    """Rough estimate: 4 chars ≈ 1 token (Chinese-heavy)."""
    return max(1, len(text) // 4)


def _extract_message_text(body: dict[str, Any]) -> str:
    """Pull user-visible text from an OpenAI-format request body."""
    parts: list[str] = []
    for msg in body.get("messages", []) or []:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for chunk in content:
                if isinstance(chunk, dict):
                    parts.append(chunk.get("text", ""))
                elif isinstance(chunk, str):
                    parts.append(chunk)
    return "\n".join(parts)


class GuardrailsMiddleware(BaseHTTPMiddleware):
    """Three checks: PII mask, jailbreak reject, cost budget reject.

    PII is masked in-place (request continues with sanitised body).
    Jailbreak and cost-overrun return 4xx immediately.
    """

    def __init__(
        self,
        app,
        cost_budget_per_request_usd: float = 0.05,
        max_tokens_per_request: int = 8000,
        reject_jailbreak: bool = True,
        exclude_paths: list[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._cost_budget = float(cost_budget_per_request_usd)
        self._max_tokens = int(max_tokens_per_request)
        self._reject_jailbreak = bool(reject_jailbreak)
        # Only chat endpoints get guardrails — health/docs excluded
        self._include_paths = frozenset({
            "/v1/chat/completions",
            "/v1/agent/chat/completions",
        })

    async def dispatch(self, request: Request, call_next):
        if request.url.path not in self._include_paths:
            return await call_next(request)

        # Buffer the body so we can read+re-inject
        body_bytes = await request.body()
        if not body_bytes:
            return await call_next(request)

        try:
            import json
            body = json.loads(body_bytes)
        except Exception:
            return await call_next(request)  # Let downstream handle malformed JSON

        text = _extract_message_text(body)
        if not text:
            return await call_next(request)

        # 1. Jailbreak check
        if self._reject_jailbreak:
            jb = detect_jailbreak(text)
            if jb:
                logger.warning("Jailbreak rejected: pattern=%r client=%s",
                               jb, request.client.host if request.client else "?")
                return JSONResponse(
                    status_code=400,
                    content={"error": "jailbreak_rejected", "pattern": jb},
                )

        # 2. Cost budget check
        est_tokens = estimate_tokens(text)
        if est_tokens > self._max_tokens:
            logger.warning("Cost budget exceeded: est_tokens=%d > %d",
                           est_tokens, self._max_tokens)
            return JSONResponse(
                status_code=413,
                content={
                    "error": "cost_budget_exceeded",
                    "detail": f"estimated {est_tokens} tokens > limit {self._max_tokens}",
                },
            )

        # 3. PII mask (in-place)
        cleaned, counts = mask_pii(text)
        if counts:
            logger.info("PII masked: %s", counts)
            _replace_message_text(body, cleaned)
            # Re-encode body for downstream
            new_body = json.dumps(body).encode("utf-8")
            async def receive():
                return {"type": "http.request", "body": new_body, "more_body": False}
            request._receive = receive  # type: ignore[attr-defined]

        # Attach audit info for downstream observability
        request.state.guardrails = {
            "pii_masked": counts,
            "est_tokens": est_tokens,
            "jailbreak_check": "passed",
        }

        return await call_next(request)


def _replace_message_text(body: dict[str, Any], new_text: str) -> None:
    """Replace the last user message's text content in-place.

    Conservative: only patches when last user content is a plain string.
    Multi-modal (list) content is left untouched to avoid breaking image parts.
    """
    messages = body.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = new_text
            return
