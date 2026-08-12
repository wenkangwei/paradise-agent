"""Intent module — abstract strategy contract + shared data types.

This module isolates intent classification from the rest of the agent loop.
Multiple strategies can be chained (cascade): rule → embedding → LLM → C++.
Each strategy is independently testable and replaceable; the C++ kernel
(when added) drops in as one more Strategy implementation.

Design goals
------------
* **Strategy pattern**: every classifier implements the same `classify()` API,
  so the cascade orchestrator doesn't care what's underneath.
* **Fast path first**: cheap strategies (rules) short-circuit the cascade.
  Misses fall through to slower, more accurate ones.
* **C++ acceleration ready**: a future `CppStrategy` (pybind11 / cython .so)
  slots in without touching anything outside this module.
* **No hard deps**: every concrete strategy degrades gracefully when its
  backend is unavailable (rules always work; embedding returns None if
  Qdrant unreachable; LLM returns None if ollama down).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    """Canonical intent set.

    Adding a new intent:
      1. Add the enum value here.
      2. Add a routing entry in graph.py::route_by_intent.
      3. Add example patterns to rules.py / prompts to llm.py.

    The string values (not enum names) are what strategies return and what
    the router consumes — this keeps the wire format stable across serialisations
    (JSON, LangGraph state, logs).
    """

    CHITCHAT = "chitchat"                # greeting / farewell / small talk
    KNOWLEDGE_QA = "knowledge_qa"        # factual question, no tool needed
    TOOL_SEARCH = "tool_search"          # explicit web search request
    TOOL_WEATHER = "tool_weather"        # weather query
    TOOL_REMINDER = "tool_reminder"      # schedule / reminder
    TOOL_TRANSLATE = "tool_translate"    # translation request
    CREATIVE = "creative"                # writing / generation / brainstorm
    TASK_COMPLEX = "task_complex"        # multi-step → candidate for delegation (W2)
    UNSAFE = "unsafe"                    # jailbreak / policy violation
    UNKNOWN = "unknown"                  # nothing matched; fallback to default path


@dataclass
class IntentResult:
    """Outcome of one classification attempt.

    `confidence` is in [0.0, 1.0]; `source` names the strategy that produced
    the result (for metrics / debugging). `latency_ms` is optional — strategies
    fill it if they self-time; the orchestrator adds its own timing anyway.

    Not frozen: the orchestrator augments `meta` with cascade trace info.
    Use a new dataclass if you need hashability.
    """

    intent: Intent
    confidence: float
    source: str               # e.g. "rule", "embedding", "llm", "cpp"
    latency_ms: float = 0.0
    meta: dict | None = None  # free-form payload (matched pattern, hits, etc.)

    def is_high_confidence(self, threshold: float = 0.85) -> bool:
        return self.confidence >= threshold


class IntentStrategy(ABC):
    """Abstract base for intent classification strategies.

    Subclasses set `name` (used as `IntentResult.source`) and implement
    `classify()`. They MUST NOT raise — on internal failure, return None so
    the cascade can fall through. The orchestrator handles logging/metrics.
    """

    name: str = "abstract"

    @abstractmethod
    async def classify(self, message: str) -> Optional[IntentResult]:
        """Return an IntentResult, or None to let the cascade continue."""
        raise NotImplementedError

    async def warmup(self) -> None:
        """Optional preloading (model weights, regex compile, etc.).

        Called once at startup by the orchestrator. Default no-op.
        Strategies that take >1s to warm up should do it here, off the
        request path.
        """
        return None
