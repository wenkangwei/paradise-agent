"""Redis-backed session state provider — L1 hot state.

Phase 5 — Memory stack (L1).

Replaces the in-memory `_agents` dict in ParadiseRuntimeManager for prod mode.
TTL'd (default 30min) to bound memory usage. Falls back to no-op when Redis
is unavailable (so tests/local dev still work).

Main-branch code never imports this module.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("paradise.memory.redis")

try:
    import redis.asyncio as aioredis  # type: ignore
    _HAS_REDIS = True
except ImportError:
    aioredis = None  # type: ignore
    _HAS_REDIS = False


class RedisStateProvider:
    """Async Redis-backed key-value store for session state.

    Values are JSON-serialised. TTL applied on every set (sliding window).
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0", ttl_seconds: int = 1800):
        self._url = redis_url
        self._ttl = int(ttl_seconds)
        self._client = None
        if not _HAS_REDIS:
            logger.warning("redis package not installed — RedisStateProvider will be no-op")

    async def _ensure_client(self):
        if self._client is None and _HAS_REDIS:
            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def get(self, key: str) -> dict | None:
        """Fetch a JSON-serialised state dict. Returns None on miss."""
        client = await self._ensure_client()
        if client is None:
            return None
        try:
            raw = await client.get(f"paradise:state:{key}")
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Redis get failed for %s: %s", key, exc)
            return None

    async def set(self, key: str, value: dict) -> None:
        """Store a state dict with sliding TTL."""
        client = await self._ensure_client()
        if client is None:
            return
        try:
            await client.set(
                f"paradise:state:{key}",
                json.dumps(value, ensure_ascii=False, default=str),
                ex=self._ttl,
            )
        except Exception as exc:
            logger.warning("Redis set failed for %s: %s", key, exc)

    async def delete(self, key: str) -> None:
        client = await self._ensure_client()
        if client is None:
            return
        try:
            await client.delete(f"paradise:state:{key}")
        except Exception as exc:
            logger.warning("Redis delete failed for %s: %s", key, exc)

    async def ping(self) -> bool:
        """Health check."""
        client = await self._ensure_client()
        if client is None:
            return False
        try:
            return bool(await client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
