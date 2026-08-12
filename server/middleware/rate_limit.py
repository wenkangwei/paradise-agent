"""Per-API-key token-bucket rate limiter.

In-process (single-server scope per architecture decision). For multi-node,
swap this for a Redis-backed limiter — the interface stays the same.

Phase 1 — Ingress layer.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("middleware.rate_limit")


class _TokenBucket:
    """Threadsafe token bucket — refills continuously."""

    __slots__ = ("capacity", "refill_per_sec", "_tokens", "_last", "_lock")

    def __init__(self, capacity: int, refill_per_sec: float):
        self.capacity = float(capacity)
        self.refill_per_sec = refill_per_sec
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = Lock()

    def try_take(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(
                self.capacity,
                self._tokens + (now - self._last) * self.refill_per_sec,
            )
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-key rate limit; falls back to client IP if no key.

    Config:
        rate_limit_per_minute: int  (e.g. 60 = 1 req/sec sustained, burst=60)
    """

    def __init__(
        self,
        app,
        rate_limit_per_minute: int = 60,
        exclude_paths: list[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._rpm = max(1, int(rate_limit_per_minute))
        self._buckets: dict[str, _TokenBucket] = defaultdict(
            lambda: _TokenBucket(capacity=self._rpm, refill_per_sec=self._rpm / 60.0)
        )
        self._exclude = frozenset(exclude_paths or ["/api/health", "/health", "/metrics"])
        logger.info("RateLimitMiddleware: %d rpm per key", self._rpm)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._exclude:
            return await call_next(request)

        # Identify caller — prefer API key, fall back to IP
        ident = request.headers.get("X-API-Key") or (
            request.client.host if request.client else "unknown"
        )
        bucket = self._buckets[ident]
        if not bucket.try_take():
            logger.warning("Rate limit exceeded for %s on %s", ident, request.url.path)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "detail": f"limit={self._rpm}/min",
                    "retry_after_seconds": int(60 / self._rpm),
                },
                headers={"Retry-After": str(int(60 / self._rpm))},
            )
        return await call_next(request)
