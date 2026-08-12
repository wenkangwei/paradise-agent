"""Intent classifier — cascade orchestrator over multiple strategies.

This is the public entry point of the intent module. It wires concrete
strategies into an ordered cascade:

    L1 rule  ──miss──▶  L2 embedding  ──miss──▶  L3 llm  ──miss──▶  L4 cpp  ──▶ UNKNOWN

Design
------
* **Fail-soft cascade**: each strategy returns `None` on miss; the cascade
  just falls through. No try/except needed at strategy boundaries — the ABC
  contract requires strategies to never raise.
* **Confidence floor**: strategies can return a result with low confidence;
  the orchestrator only short-circuits when `conf >= accept_threshold`.
  Otherwise the result is *recorded* and we keep cascading for a better
  answer (last-wins, so a later tier can override an earlier low-conf guess).
* **Cache backfill**: when L2 misses but L3/L4 produces a confident answer,
  we fire-and-forget a write to the embedding cache so future similar
  queries hit L2 instead of paying L3 latency. Side effect — must never
  block the response path.
* **Metrics**: every tier's latency and hit/miss is logged at DEBUG; the
  returned `IntentResult.meta` carries per-tier timing for downstream OTel.

Concurrency
-----------
`classify()` is safe to call from an async context. Strategies are expected
to be I/O bound (httpx / qdrant / subprocess). The cascade is sequential
on purpose: parallelism would burn compute on slow tiers (L3 LLM) when L1
already has the answer. If L2+L3 latency ever dominates, swap to
`asyncio.gather` over [L2, L3] with first-wins.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from paradise.core.intent.base import (
    Intent,
    IntentResult,
    IntentStrategy,
)
from paradise.core.intent.embedding import EmbeddingStrategy

logger = logging.getLogger(__name__)


@dataclass
class ClassifierConfig:
    """Cascade tuning knobs. All defaults are overridable from app config."""
    accept_threshold: float = 0.80
    """If any tier returns conf >= this, short-circuit immediately."""

    enable_embedding: bool = True
    enable_llm: bool = True
    enable_cpp: bool = True
    """Honored only if the strategy is constructed; false → skipped."""

    embedding_backfill: bool = True
    """After an LLM/C++ hit, write the result back to the L2 cache."""

    llm_timeout_s: float = 4.0
    """Hard ceiling for L3 latency. Exceeding → treated as miss."""


@dataclass
class _TierTrace:
    name: str
    hit: bool
    latency_ms: float
    confidence: Optional[float] = None


@dataclass
class _ClassifyOutcome:
    result: Optional[IntentResult]
    trace: list[_TierTrace] = field(default_factory=list)


class IntentClassifier:
    """Sequential cascade over a list of strategies.

    Constructed once at app startup (see `paradise.factory`) and called per
    request. The cascade ordering is fixed at construction; runtime
    reordering is not supported (would require tearing down the strategy
    pool, not worth the complexity).
    """

    def __init__(
        self,
        strategies: Iterable[IntentStrategy],
        config: Optional[ClassifierConfig] = None,
    ) -> None:
        self._strategies: list[IntentStrategy] = list(strategies)
        self._config = config or ClassifierConfig()

        # Convenience handle to the embedding tier for cache backfill.
        self._embedder: Optional[EmbeddingStrategy] = next(
            (s for s in self._strategies if isinstance(s, EmbeddingStrategy)),
            None,
        )

        if not self._strategies:
            logger.warning("IntentClassifier initialized with no strategies")

    async def classify(self, message: str) -> IntentResult:
        """Run the cascade. Always returns an IntentResult (UNKNOWN if all miss).

        Never raises — internal errors collapse to UNKNOWN so the caller's
        request path stays alive. The router falls through to the default
        LangGraph path on UNKNOWN.
        """
        if not message or not message.strip():
            return self._unknown(trace=[], reason="empty_message")

        outcome = await self._run_cascade(message)
        winner = outcome.result

        # Backfill the L2 cache with the winning result.
        if (
            winner is not None
            and winner.intent != Intent.UNKNOWN
            and self._config.embedding_backfill
            and self._embedder is not None
            and winner.source not in ("rule", "embedding")
        ):
            asyncio.create_task(
                self._safe_backfill(message, winner)
            )

        if winner is None:
            return self._unknown(trace=outcome.trace, reason="all_tiers_missed")

        # Attach per-tier timing so OTel/log can see the cascade shape.
        winner.meta = winner.meta or {}
        winner.meta["cascade"] = [
            {"name": t.name, "hit": t.hit, "ms": round(t.latency_ms, 2),
             "conf": t.confidence}
            for t in outcome.trace
        ]
        return winner

    async def _run_cascade(self, message: str) -> _ClassifyOutcome:
        trace: list[_TierTrace] = []
        best: Optional[IntentResult] = None

        for strategy in self._strategies:
            name = strategy.name
            t0 = time.perf_counter()
            try:
                result = await strategy.classify(message)
            except Exception as exc:
                # ABC promises no raise, but defend against bugs.
                logger.exception("Strategy %s raised (suppressed): %s", name, exc)
                result = None
            latency_ms = (time.perf_counter() - t0) * 1000

            if result is None:
                trace.append(_TierTrace(name, hit=False, latency_ms=latency_ms))
                logger.debug("intent tier=%s miss ms=%.1f", name, latency_ms)
                continue

            trace.append(_TierTrace(
                name, hit=True, latency_ms=latency_ms,
                confidence=result.confidence,
            ))
            logger.debug(
                "intent tier=%s hit intent=%s conf=%.2f ms=%.1f",
                name, result.intent.value, result.confidence, latency_ms,
            )

            # Strong answer — short-circuit.
            if result.confidence >= self._config.accept_threshold:
                return _ClassifyOutcome(result=result, trace=trace)

            # Weak answer — remember as fallback, keep cascading.
            best = result

        return _ClassifyOutcome(result=best, trace=trace)

    async def _safe_backfill(self, message: str, result: IntentResult) -> None:
        """Fire-and-forget cache write. Failures logged, never propagated."""
        try:
            await self._embedder.store(message, result)  # type: ignore[union-attr]
        except Exception as exc:
            logger.debug("cache backfill failed (%s)", exc)

    def _unknown(self, trace: list[_TierTrace], reason: str) -> IntentResult:
        return IntentResult(
            intent=Intent.UNKNOWN,
            confidence=0.0,
            source="cascade",
            latency_ms=0.0,
            meta={"reason": reason, "cascade": [
                {"name": t.name, "hit": t.hit, "ms": round(t.latency_ms, 2)}
                for t in trace
            ]},
        )

    async def warmup(self) -> None:
        """Pre-warm all strategies in parallel at startup."""
        if not self._strategies:
            return
        await asyncio.gather(
            *(s.warmup() for s in self._strategies),
            return_exceptions=True,
        )
