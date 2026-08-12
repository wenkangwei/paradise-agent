"""Semantic cache — short-circuit LLM calls on near-duplicate prompts.

Phase 5 — Memory stack (cache layer).

Wraps graph.invoke (or agent.handle_message) with:
  1. Embed the user's prompt
  2. Query Qdrant for nearest cached response (similarity > threshold)
  3. On hit: yield cached response events without invoking the LLM
  4. On miss: forward to the underlying call, store result for future hits

Threshold default 0.92 — conservative; lower for more hits but lower fidelity.
"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncGenerator

from paradise.memory.qdrant_provider import QdrantSemanticProvider

logger = logging.getLogger("paradise.memory.semantic_cache")


class SemanticCache:
    """Async semantic cache for agent responses.

    Usage:
        cache = SemanticCache(qdrant, threshold=0.92)

        async def cached_or_invoke(user_msg, agent_id, invoke_fn):
            hit = await cache.lookup(user_msg, agent_id)
            if hit:
                return cache.yield_cached_events(hit)
            # Miss — actually invoke, capture events, store
            events = []
            async for e in invoke_fn():
                events.append(e)
                yield e
            await cache.store(user_msg, events, agent_id)
    """

    def __init__(self, qdrant: QdrantSemanticProvider, threshold: float = 0.92):
        self._qdrant = qdrant
        self._threshold = float(threshold)
        self.hits = 0
        self.misses = 0

    async def lookup(self, query: str, agent_id: str) -> dict | None:
        """Return cached payload if similarity ≥ threshold, else None."""
        results = await self._qdrant.search(query, agent_id=agent_id, limit=1)
        if not results:
            self.misses += 1
            return None
        best = results[0]
        if best.get("score", 0.0) >= self._threshold:
            self.hits += 1
            logger.info(
                "Semantic cache HIT (score=%.3f, query=%r...)",
                best["score"], query[:50],
            )
            # Qdrant returns payload dict directly
            return best
        self.misses += 1
        return None

    async def store(
        self,
        query: str,
        events: list[dict],
        agent_id: str,
        turn: int = 0,
    ) -> None:
        """Store events paired with query embedding for future hits."""
        response_text = ""
        for event in reversed(events):
            if event.get("type") == "done":
                response_text = event.get("content", "")
                break
        if not response_text:
            return
        payload_text = f"Q: {query}\nA: {response_text[:1000]}"
        await self._qdrant.store_with_payload(payload_text, agent_id, turn, {
            "events": events,
            "query": query,
            "response": response_text,
        })

    def stats(self) -> dict:
        """Hit/miss stats for observability."""
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hits / total if total else 0.0,
        }
