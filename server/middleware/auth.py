"""API Key authentication middleware.

Validates the caller's API key. Two header formats are accepted:
  1. `X-API-Key: <key>`            (service-friendly, explicit)
  2. `Authorization: Bearer <key>` (OpenAI-compatible — what most clients send)

Excludes health/docs/openapi paths from auth.

Phase 1 — Ingress layer of the production stack.
Only active when PARADISE_MODE=prod.
"""
from __future__ import annotations

import logging
from typing import Iterable

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger("middleware.auth")

# Paths that skip auth (health checks, introspection, metrics)
_DEFAULT_EXCLUDE = frozenset({
    "/api/health",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/metrics",
})


class AuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid X-API-Key header.

    Keys come from config.prod.yaml::ingress.api_keys (env-substituted).
    The first key in the list is the "primary" client key; additional keys
    are for service-to-service or rotated keys.
    """

    def __init__(
        self,
        app,
        api_keys: Iterable[str],
        exclude_paths: Iterable[str] | None = None,
    ) -> None:
        super().__init__(app)
        # Filter empty keys (env not yet substituted, etc.)
        self._keys = {k.strip() for k in api_keys if k and k.strip()}
        self._exclude = frozenset(exclude_paths or _DEFAULT_EXCLUDE)
        if not self._keys:
            logger.warning("AuthMiddleware initialised with zero keys — all requests will 401")

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._exclude:
            return await call_next(request)

        key = self._extract_key(request)
        if not key:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "missing_api_key",
                    "detail": "Auth required via X-API-Key header or Authorization: Bearer <key>",
                },
            )
        if key not in self._keys:
            logger.warning("Invalid API key attempt from %s", request.client.host if request.client else "?")
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_api_key"},
            )
        return await call_next(request)

    @staticmethod
    def _extract_key(request: Request) -> str:
        """Pull the API key from either supported header. Empty string if absent."""
        explicit = request.headers.get("X-API-Key", "").strip()
        if explicit:
            return explicit
        bearer = request.headers.get("Authorization", "").strip()
        if bearer.lower().startswith("bearer "):
            return bearer[7:].strip()
        return ""
