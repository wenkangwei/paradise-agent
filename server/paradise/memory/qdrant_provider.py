"""Qdrant-backed semantic recall provider — L2 semantic memory.

Phase 5 — Memory stack (L2).

Stores conversation turns as embeddings for later semantic recall. Used by
SemanticCache (query → embed → similar hit → short-circuit LLM call).

Uses BAAI/bge-m3 via sentence-transformers for embeddings (1024-dim).
Falls back to no-op when qdrant-client or sentence-transformers missing.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("paradise.memory.qdrant")

try:
    from qdrant_client import AsyncQdrantClient  # type: ignore
    from qdrant_client.http import models as qmodels  # type: ignore
    _HAS_QDRANT = True
except ImportError:
    AsyncQdrantClient = None  # type: ignore
    qmodels = None  # type: ignore
    _HAS_QDRANT = False

_EMBED_MODEL = None  # cached sentence-transformers model


def _get_embed_model():
    """Lazy-load the embedding model (BAAI/bge-m3, ~2GB)."""
    global _EMBED_MODEL
    if _EMBED_MODEL is not None:
        return _EMBED_MODEL
    try:
        from sentence_transformers import SentenceTransformer
        _EMBED_MODEL = SentenceTransformer("BAAI/bge-m3")
        return _EMBED_MODEL
    except Exception as exc:
        logger.warning("Failed to load embedding model: %s", exc)
        return None


class QdrantSemanticProvider:
    """Async Qdrant-backed semantic store.

    Collection schema:
        point_id: uuid
        vector:   1024-dim (bge-m3)
        payload:  {"text": ..., "agent_id": ..., "ts": ..., "turn": ...}
    """

    COLLECTION = "paradise_semantic"

    def __init__(self, qdrant_url: str = "http://localhost:6333", vector_size: int = 1024):
        self._url = qdrant_url
        self._size = vector_size
        self._client = None
        if not _HAS_QDRANT:
            logger.warning("qdrant-client not installed — QdrantSemanticProvider will be no-op")

    async def _ensure_client(self):
        if self._client is None and _HAS_QDRANT:
            self._client = AsyncQdrantClient(url=self._url)
            try:
                await self._client.create_collection(
                    collection_name=self.COLLECTION,
                    vectors_config=qmodels.VectorParams(
                        size=self._size, distance=qmodels.Distance.COSINE,
                    ),
                )
                logger.info("Created Qdrant collection %s", self.COLLECTION)
            except Exception:
                # Already exists — ignore
                pass
        return self._client

    async def store(self, text: str, agent_id: str, turn: int = 0) -> None:
        """Embed and store a turn's text."""
        await self.store_with_payload(text, agent_id, turn, payload=None)

    async def store_with_payload(
        self,
        text: str,
        agent_id: str,
        turn: int,
        payload: dict | None = None,
    ) -> None:
        """Embed text and store with arbitrary payload dict attached."""
        client = await self._ensure_client()
        if client is None:
            return
        model = _get_embed_model()
        if model is None:
            return
        try:
            embedding = model.encode(text).tolist()
            body = {
                "text": text[:4000],
                "agent_id": agent_id,
                "turn": turn,
                "ts": time.time(),
            }
            if payload:
                body.update(payload)
            await client.upsert(
                collection_name=self.COLLECTION,
                points=[
                    qmodels.PointStruct(
                        id=f"{agent_id}:{turn}:{int(time.time()*1000)}",
                        vector=embedding,
                        payload=body,
                    )
                ],
            )
        except Exception as exc:
            logger.warning("Qdrant store failed: %s", exc)

    async def search(self, query: str, agent_id: str | None = None, limit: int = 3):
        """Return top-k similar texts. Returns list of {"text":, "score":} dicts."""
        client = await self._ensure_client()
        if client is None:
            return []
        model = _get_embed_model()
        if model is None:
            return []
        try:
            embedding = model.encode(query).tolist()
            must = [qmodels.FieldCondition(key="agent_id", match=qmodels.MatchValue(value=agent_id))] if agent_id else None
            results = await client.search(
                collection_name=self.COLLECTION,
                query_vector=embedding,
                limit=limit,
                query_filter=qmodels.Filter(must=must) if must else None,
            )
            return [
                {"text": r.payload.get("text", ""), "score": r.score}
                for r in results
            ]
        except Exception as exc:
            logger.warning("Qdrant search failed: %s", exc)
            return []

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
