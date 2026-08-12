"""Phase 5 tests — Redis state provider, Qdrant semantic provider, semantic cache.

These tests don't require real Redis or Qdrant running — they verify:
1. Provider classes are constructible
2. Graceful no-op when deps missing or connection fails
3. Semantic cache logic with mocked Qdrant
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ── RedisStateProvider ────────────────────────────────────────────

def test_redis_provider_constructible():
    from paradise.memory.redis_provider import RedisStateProvider
    p = RedisStateProvider(redis_url="redis://localhost:6379/0", ttl_seconds=60)
    assert p._ttl == 60


@pytest.mark.asyncio
async def test_redis_get_returns_none_when_unavailable():
    """Missing redis package or unreachable server must NOT raise."""
    from paradise.memory.redis_provider import RedisStateProvider, _HAS_REDIS
    p = RedisStateProvider(redis_url="redis://nonexistent:6379/0", ttl_seconds=60)
    # Even if redis package is installed, connecting to nonexistent host fails.
    # Provider must swallow and return None.
    result = await p.get("any-key")
    # Either None (no-op) or None (connection failed) — both correct
    assert result is None


@pytest.mark.asyncio
async def test_redis_set_does_not_raise_when_unavailable():
    from paradise.memory.redis_provider import RedisStateProvider
    p = RedisStateProvider(redis_url="redis://nonexistent:6379/0")
    # Must not raise even when connection fails
    await p.set("k", {"v": 1})


# ── QdrantSemanticProvider ────────────────────────────────────────

def test_qdrant_provider_constructible():
    from paradise.memory.qdrant_provider import QdrantSemanticProvider
    p = QdrantSemanticProvider(qdrant_url="http://localhost:6333")
    assert p._url == "http://localhost:6333"


@pytest.mark.asyncio
async def test_qdrant_search_returns_empty_when_unavailable():
    from paradise.memory.qdrant_provider import QdrantSemanticProvider
    p = QdrantSemanticProvider(qdrant_url="http://nonexistent:6333")
    # Must not raise; return empty list
    results = await p.search("query", agent_id="a1")
    assert results == []


# ── SemanticCache logic with mocks ────────────────────────────────

@pytest.mark.asyncio
async def test_semantic_cache_hit_returns_payload():
    from paradise.memory.semantic_cache import SemanticCache

    fake_qdrant = MagicMock()
    fake_qdrant.search = AsyncMock(return_value=[
        {"text": "...", "score": 0.95, "events": [{"type": "done", "content": "cached!"}]}
    ])
    cache = SemanticCache(fake_qdrant, threshold=0.92)

    result = await cache.lookup("any query", "agent-1")
    assert result is not None
    assert result["score"] == 0.95
    assert cache.hits == 1
    assert cache.misses == 0


@pytest.mark.asyncio
async def test_semantic_cache_miss_below_threshold():
    from paradise.memory.semantic_cache import SemanticCache

    fake_qdrant = MagicMock()
    fake_qdrant.search = AsyncMock(return_value=[
        {"text": "...", "score": 0.75}  # below threshold
    ])
    cache = SemanticCache(fake_qdrant, threshold=0.92)

    result = await cache.lookup("query", "agent-1")
    assert result is None
    assert cache.hits == 0
    assert cache.misses == 1


@pytest.mark.asyncio
async def test_semantic_cache_miss_on_empty_results():
    from paradise.memory.semantic_cache import SemanticCache

    fake_qdrant = MagicMock()
    fake_qdrant.search = AsyncMock(return_value=[])
    cache = SemanticCache(fake_qdrant, threshold=0.92)

    result = await cache.lookup("query", "agent-1")
    assert result is None
    assert cache.misses == 1


@pytest.mark.asyncio
async def test_semantic_cache_store_extracts_response_from_done_event():
    from paradise.memory.semantic_cache import SemanticCache

    fake_qdrant = MagicMock()
    fake_qdrant.store_with_payload = AsyncMock()
    cache = SemanticCache(fake_qdrant, threshold=0.92)

    events = [
        {"type": "content", "content": "Hello "},
        {"type": "content", "content": "world"},
        {"type": "done", "content": "Hello world", "thinking": ""},
    ]
    await cache.store("Hi there", events, "agent-1", turn=1)
    fake_qdrant.store_with_payload.assert_awaited_once()
    # Check the response text was extracted
    args, kwargs = fake_qdrant.store_with_payload.call_args
    # store_with_payload(text, agent_id, turn, payload={...})
    assert "Hello world" in args[0]  # payload_text contains response


def test_semantic_cache_stats():
    from paradise.memory.semantic_cache import SemanticCache
    cache = SemanticCache(MagicMock(), threshold=0.9)
    cache.hits = 3
    cache.misses = 7
    stats = cache.stats()
    assert stats["hits"] == 3
    assert stats["misses"] == 7
    assert stats["hit_rate"] == 0.3
