"""Embedding-based intent strategy (L2 — semantic cache tier).

Uses Qdrant to find historically similar messages and inherit their intent.
Latency ~20-50ms (bge-m3 embed + kNN search).

Storage model
-------------
The intent cache is just a Qdrant collection with payload `{"intent": str,
"confidence": float, "agent_id": "__intent_cache__"}`. Writes happen when
downstream strategies (LLM, C++) classify a fresh message — we backfill
the cache so future similar queries hit L2 instead of recomputing.

This strategy has NO training step. It's pure retrieval. The richer the
cache grows, the better L2 hit rate.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from paradise.core.intent.base import Intent, IntentResult, IntentStrategy

logger = logging.getLogger(__name__)


class EmbeddingStrategy(IntentStrategy):
    """L2 — Qdrant kNN over past (message → intent) pairs.

    Degrades to None when Qdrant unreachable OR sentence-transformers missing
    OR collection empty. Cascade falls through to next strategy.
    """

    name = "embedding"

    def __init__(
        self,
        qdrant_provider,           # paradise.memory.qdrant_provider.QdrantSemanticProvider
        similarity_threshold: float = 0.95,
        collection: str = "intent_cache",
    ) -> None:
        self._qdrant = qdrant_provider
        self._threshold = similarity_threshold
        self._collection = collection

    async def classify(self, message: str) -> Optional[IntentResult]:
        if not message or not message.strip():
            return None
        t0 = time.perf_counter()
        try:
            results = await self._qdrant.search(
                query=message,
                agent_id=self._collection,
                limit=1,
                score_threshold=self._threshold,
            )
        except Exception as exc:
            logger.debug("Embedding L2 miss (%s)", exc)
            return None
        if not results:
            return None
        hit = results[0]
        payload = getattr(hit, "payload", None) or {}
        intent_str = payload.get("intent")
        if not intent_str:
            return None
        try:
            intent = Intent(intent_str)
        except ValueError:
            logger.warning("Stale intent label in cache: %r", intent_str)
            return None
        latency_ms = (time.perf_counter() - t0) * 1000
        return IntentResult(
            intent=intent,
            confidence=float(payload.get("confidence", hit.score)),
            source=self.name,
            latency_ms=latency_ms,
            meta={"hit_id": getattr(hit, "id", None), "score": getattr(hit, "score", None)},
        )

    async def store(self, message: str, result: IntentResult) -> None:
        """Backfill the cache after a downstream strategy classifies a message.

        Fire-and-forget at the call site (orchestrator wraps in asyncio.create_task).
        Failures are logged at DEBUG — losing a cache write is not a request-path error.
        """
        try:
            await self._qdrant.store_with_payload(
                text=message,
                agent_id=self._collection,
                turn=0,
                payload={
                    "intent": result.intent.value,
                    "confidence": result.confidence,
                    "source": result.source,
                },
            )
        except Exception as exc:
            logger.debug("Embedding L2 store failed (%s)", exc)
