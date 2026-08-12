"""Intent recognition module — fast, pluggable message classification.

The module is standalone on purpose: it has no dependency on the agent loop,
LangGraph, or any transport. It exports a single entry point
(`IntentClassifier`) plus the building blocks for tests and factory wiring.

Typical usage (from `paradise.factory`):

    from paradise.core.intent import (
        IntentClassifier, ClassifierConfig,
        RuleStrategy, EmbeddingStrategy, LLMStrategy, CppStrategy,
    )

    classifier = IntentClassifier(
        strategies=[
            RuleStrategy(),
            EmbeddingStrategy(qdrant_provider),
            LLMStrategy(base_url=ollama_url),
            CppStrategy(),
        ],
        config=ClassifierConfig(),
    )
    result = await classifier.classify(user_message)
    # result.intent → routing decision

C++ acceleration
----------------
`CppStrategy` is a no-op stub until the pybind11 kernel is built. The
cascade handles its absence transparently — wire it in unconditionally
today; when `cpp/paradise_intent_ext.so` lands, L4 lights up with zero
code changes here.
"""
from paradise.core.intent.base import (
    Intent,
    IntentResult,
    IntentStrategy,
)
from paradise.core.intent.rules import RuleStrategy, DEFAULT_RULES
from paradise.core.intent.embedding import EmbeddingStrategy
from paradise.core.intent.llm import LLMStrategy
from paradise.core.intent.cpp import CppStrategy
from paradise.core.intent.classifier import (
    IntentClassifier,
    ClassifierConfig,
)

__all__ = [
    # Core types
    "Intent",
    "IntentResult",
    "IntentStrategy",
    # Concrete strategies
    "RuleStrategy",
    "EmbeddingStrategy",
    "LLMStrategy",
    "CppStrategy",
    "DEFAULT_RULES",
    # Orchestrator
    "IntentClassifier",
    "ClassifierConfig",
]
