"""ResilientTransport — wraps a ProviderTransport with retry, circuit breaker, cost budget.

Phase 3 — production hardening for LLM inference layer.

Design: proxy pattern. The wrapper exposes the same interface as the inner
transport (api_mode, chat, stream_chat, etc.) so agent code (which uses
duck typing) doesn't know the difference. When a method is called:

  1. Cost budget check (estimate token cost upfront)
  2. Circuit breaker (skip call if open)
  3. Tenacity retry with exponential backoff
  4. Forward to inner transport
  5. On success: record cost; on failure: record failure

Only active when PARADISE_MODE=prod. Main branch instantiates transports
directly via resolve_transport() — no ResilientTransport involved.

Configuration (from config.prod.yaml::resilience):
  max_retries: 3
  retry_backoff_max_seconds: 10
  circuit_failure_threshold: 3
  circuit_recovery_seconds: 30
  cost_budget_per_request_usd: 0.05  (also enforced by Guardrails middleware)
"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncGenerator

try:
    from tenacity import (
        AsyncRetrying,
        RetryError,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
        before_sleep_log,
    )
except ImportError:  # pragma: no cover
    AsyncRetrying = None  # type: ignore

logger = logging.getLogger("paradise.transports.resilient")


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open and the call is rejected."""


class CostBudgetExceeded(Exception):
    """Raised when the estimated cost of a request exceeds the budget."""


class CircuitBreaker:
    """Simple async circuit breaker — three-state (CLOSED, OPEN, HALF_OPEN).

    Tracks consecutive failures. After `failure_threshold` failures within
    a recovery window, the circuit OPENS and rejects calls for
    `recovery_seconds`. After recovery, one trial call is allowed
    (HALF_OPEN). On success, circuit CLOSES. On failure, re-OPENS.

    Single-process only — for multi-node, swap with redis-circuit-breaker.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 3, recovery_seconds: int = 30) -> None:
        self.failure_threshold = int(failure_threshold)
        self.recovery_seconds = int(recovery_seconds)
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            # Check if recovery window elapsed
            if time.monotonic() - self._opened_at >= self.recovery_seconds:
                self._state = self.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """Return True if call is allowed; False if circuit is OPEN."""
        return self.state != self.OPEN

    def record_success(self) -> None:
        if self._state in (self.HALF_OPEN, self.OPEN):
            logger.info("Circuit: recovery successful → CLOSED")
        self._state = self.CLOSED
        self._failures = 0

    def record_failure(self) -> None:
        self._failures += 1
        if self._state == self.HALF_OPEN:
            # Trial failed — re-open
            self._trip()
            return
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = self.OPEN
        self._opened_at = time.monotonic()
        logger.warning(
            "Circuit tripped OPEN after %d failures (recovery in %ds)",
            self._failures, self.recovery_seconds,
        )


class ResilientTransport:
    """Proxy that wraps an inner ProviderTransport with resilience policies.

    Duck-types the inner transport: any attribute access not on this class
    is forwarded via __getattr__ (so api_mode, convert_messages, build_kwargs,
    etc. all pass through unchanged).
    """

    def __init__(
        self,
        inner: Any,
        max_retries: int = 3,
        retry_backoff_max_seconds: int = 10,
        circuit_failure_threshold: int = 3,
        circuit_recovery_seconds: int = 30,
        cost_budget_per_request_usd: float = 0.05,
    ) -> None:
        self._inner = inner
        self._max_retries = max_retries
        self._backoff_max = retry_backoff_max_seconds
        self._breaker = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            recovery_seconds=circuit_recovery_seconds,
        )
        self._cost_budget = float(cost_budget_per_request_usd)
        # Rough per-token cost — refined in Phase 6 with provider-specific rates
        self._usd_per_token = 0.000002  # $2/M tokens blended estimate

    # ── Attribute pass-through ──────────────────────────────────────
    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails — forward to inner.
        return getattr(self._inner, name)

    # ── Cost estimation ─────────────────────────────────────────────
    def _estimate_cost_usd(self, messages: list[dict], max_tokens: int) -> float:
        """Rough cost estimate — 4 chars ≈ 1 token, blended rate."""
        chars = sum(
            len(m.get("content", ""))
            for m in (messages or [])
            if isinstance(m, dict)
        )
        prompt_tokens = max(1, chars // 4)
        completion_tokens = max_tokens or 1024
        return (prompt_tokens + completion_tokens) * self._usd_per_token

    def _check_cost_budget(self, messages: list[dict], max_tokens: int) -> None:
        est = self._estimate_cost_usd(messages, max_tokens)
        if est > self._cost_budget:
            raise CostBudgetExceeded(
                f"estimated ${est:.4f} > budget ${self._cost_budget:.4f}"
            )

    # ── Non-streaming chat ──────────────────────────────────────────
    async def chat(self, **kwargs):
        """Wrap inner.chat() with circuit + retry + cost check."""
        messages = kwargs.get("messages", [])
        max_tokens = kwargs.get("max_tokens", 1024)
        self._check_cost_budget(messages, max_tokens)

        if not self._breaker.allow():
            raise CircuitOpenError(
                f"circuit open for {self._inner.__class__.__name__}"
            )

        if AsyncRetrying is None:
            return await self._inner.chat(**kwargs)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_retries),
                wait=wait_exponential(multiplier=1, max=self._backoff_max),
                retry=retry_if_exception_type((ConnectionError, TimeoutError)),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=True,
            ):
                with attempt:
                    result = await self._inner.chat(**kwargs)
                    self._breaker.record_success()
                    return result
        except Exception as exc:
            # Any non-retryable or exhausted retries
            self._breaker.record_failure()
            raise

    # ── Streaming chat ──────────────────────────────────────────────
    async def stream_chat(self, **kwargs) -> AsyncGenerator[Any, None]:
        """Wrap inner.stream_chat() with circuit + cost check.

        Retries don't apply to streaming (partial output can't be rewound).
        If the stream fails mid-flight, the caller sees the error.
        """
        messages = kwargs.get("messages", [])
        max_tokens = kwargs.get("max_tokens", 1024)
        self._check_cost_budget(messages, max_tokens)

        if not self._breaker.allow():
            raise CircuitOpenError(
                f"circuit open for {self._inner.__class__.__name__}"
            )

        try:
            async for chunk in self._inner.stream_chat(**kwargs):
                yield chunk
            self._breaker.record_success()
        except Exception:
            self._breaker.record_failure()
            raise

    # ── Introspection ───────────────────────────────────────────────
    def circuit_state(self) -> str:
        """Expose circuit state for observability (Phase 6)."""
        return self._breaker.state


def wrap_transport(inner: Any, resilience_config: dict | None = None) -> ResilientTransport:
    """Factory helper — wrap an existing transport instance with resilience.

    Args:
        inner: a concrete ProviderTransport (openai_compat / anthropic / ollama_native)
        resilience_config: dict from config.prod.yaml::resilience (optional)

    Returns:
        ResilientTransport proxy. Caller uses it as if it were the inner transport.
    """
    cfg = resilience_config or {}
    return ResilientTransport(
        inner=inner,
        max_retries=int(cfg.get("max_retries", 3)),
        retry_backoff_max_seconds=int(cfg.get("retry_backoff_max_seconds", 10)),
        circuit_failure_threshold=int(cfg.get("circuit_failure_threshold", 3)),
        circuit_recovery_seconds=int(cfg.get("circuit_recovery_seconds", 30)),
        cost_budget_per_request_usd=float(cfg.get("cost_budget_per_request_usd", 0.05)),
    )
