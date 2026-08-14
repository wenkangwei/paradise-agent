"""RagPattern — Retrieval-Augmented Generation.

Two-node linear graph:
    retrieve_node → synthesize_node → END

  * retrieve_node:     call injected Retriever to fetch top-k documents
                       for the user message. On any failure (retriever
                       missing / exception / empty hits), sets
                       rag_used=False and proceeds — synthesize_node
                       then answers from parametric knowledge only.
  * synthesize_node:   single LLM call with the retrieved context
                       formatted into a system_prompt prefix. Failures
                       map to HANDOFF_ERROR like ChatPattern.

Retriever contract (duck-typed Protocol — no inheritance required):
    class Retriever(Protocol):
        async def retrieve(self, query: str, top_k: int = 3) -> list[dict]: ...

Each returned dict has shape:
    {"content": str, "source": str (optional), "score": float (optional)}

Phase 5 will adapt QdrantSemanticProvider to this shape. Tests inject
scripted retrievers.

Why a separate pattern instead of always going through chat:
  * mode_judge + intent classifier can route knowledge queries here
    even when no tools are needed, saving the ReAct reasoning overhead.
  * artifacts.retrieved is a stable contract for ATIF export (Phase 2.13)
    and for downstream UI ("sources used: ...").
  * The chat-fallback path keeps the pattern safe to wire by default —
    no catastrophic failure if qdrant is down at deploy time.

Phase 2.11.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypedDict, runtime_checkable

from langgraph.graph import END, StateGraph

from paradise.core.handoff import (
    HANDOFF_ERROR,
    HANDOFF_OK,
    HandoffRequest,
    HandoffResponse,
)
from paradise.core.patterns.base import SubgraphPattern

if TYPE_CHECKING:
    from paradise.config import LLMConfig
    from paradise.core.registry import SubgraphRegistry

logger = logging.getLogger("paradise.patterns.rag")


# Synthesis prompt template — keeps the retrieved context anchored so
# the LLM doesn't hallucinate beyond it. Empty context falls back to a
# generic system prompt.
_RAG_SYSTEM_PROMPT_WITH_CONTEXT = (
    "You are a helpful assistant answering based on the retrieved context. "
    "Use ONLY the context below; if it does not contain the answer, say you "
    "don't know rather than fabricating.\n\n"
    "Context:\n{context}"
)

_RAG_SYSTEM_PROMPT_FALLBACK = (
    "You are a helpful, concise assistant. Reply directly."
    " (Note: no context retrieved; answering from parametric knowledge.)"
)


@runtime_checkable
class Retriever(Protocol):
    """Duck-typed retriever interface.

    Any object with an async `retrieve(query, top_k)` method works.
    Returns a list of dicts; each dict MUST contain "content" (str).
    "source" / "score" are optional but consumed if present.

    Implementations should never raise — return [] on internal failure
    so the pattern degrades to chat-fallback. RagPattern still wraps
    the call in try/except as defense in depth.
    """

    async def retrieve(self, query: str, top_k: int = 3) -> list[dict]: ...


class RagState(TypedDict, total=False):
    """RAG subgraph state.

    Fields:
        handoff:          incoming request.
        retrieved:        list of context dicts from retrieve_node.
                          Empty list = retriever returned nothing OR
                          retriever was unavailable (check rag_used).
        rag_used:         True iff retrieval actually happened. False
                          when retriever is missing or raised.
        handoff_response: emitted by synthesize_node.
    """
    handoff: HandoffRequest
    retrieved: list[dict]
    rag_used: bool
    handoff_response: HandoffResponse


class RagPattern(SubgraphPattern):
    """Retrieval-augmented generation with chat-fallback."""

    name: ClassVar[str] = "rag"
    description: ClassVar[str] = (
        "检索增强 — 知识查询 / 文档资料询问"
    )
    category: ClassVar[str] = "basic"
    cost_budget_usd: ClassVar[float] = 0.03
    max_turns: ClassVar[int] = 2

    # Default top-k for retrieval. Tunable per-call via context if needed
    # (Phase 2.12+ may surface this as a per-request knob).
    _DEFAULT_TOP_K: ClassVar[int] = 3

    def __init__(
        self,
        transport: Any | None = None,
        llm_config: "LLMConfig | None" = None,
        retriever: Retriever | None = None,
    ) -> None:
        """
        Args:
            transport: LLM transport. None → lazy resolve via factory.
            llm_config: model config. None → lazy resolve.
            retriever: object implementing the Retriever protocol. None →
                lazy resolve via paradise.memory.qdrant_provider. If
                lazy resolve fails (dev without qdrant), synthesize_node
                runs in chat-fallback mode.
        """
        self._transport = transport
        self._llm_config = llm_config
        self._retriever = retriever

    def build(self, registry: "SubgraphRegistry") -> Any:
        """Compile the RAG graph: retrieve → synthesize → END."""
        pattern = self

        async def retrieve_node(state: RagState) -> dict:
            """Call retriever; fail-soft to chat-fallback on any error."""
            req: HandoffRequest = state["handoff"]
            retriever = pattern._resolve_retriever()
            if retriever is None:
                logger.debug(
                    "rag: no retriever wired → chat-fallback trace=%s",
                    req.trace_id,
                )
                return {"retrieved": [], "rag_used": False}

            try:
                hits = await retriever.retrieve(
                    req.message, top_k=pattern._DEFAULT_TOP_K,
                )
            except Exception as exc:
                logger.warning(
                    "rag retriever raised (%s) — chat-fallback trace=%s",
                    exc, req.trace_id,
                )
                return {"retrieved": [], "rag_used": False}

            if not hits:
                logger.debug(
                    "rag: retriever returned 0 hits trace=%s", req.trace_id,
                )
                # rag_used=True even with 0 hits — retrieval happened,
                # just yielded nothing useful. Distinguishes from
                # retriever-missing case for observability.
                return {"retrieved": [], "rag_used": True}

            logger.debug(
                "rag retrieved %d hits trace=%s",
                len(hits), req.trace_id,
            )
            return {"retrieved": list(hits), "rag_used": True}

        async def synthesize_node(state: RagState) -> dict:
            """LLM call with retrieved context formatted into prompt."""
            req: HandoffRequest = state["handoff"]
            retrieved: list[dict] = state.get("retrieved") or []
            rag_used: bool = state.get("rag_used", False)

            try:
                transport = pattern._resolve_transport()
                cfg = pattern._resolve_llm_config()
                kwargs = pattern._build_kwargs(cfg, req, retrieved, rag_used)
                result = await transport.chat(**kwargs)
                content = (
                    result.content if hasattr(result, "content")
                    else str(result)
                )
                logger.debug(
                    "rag reply trace=%s rag_used=%s len=%d",
                    req.trace_id, rag_used, len(content),
                )
                return {"handoff_response": HandoffResponse(
                    session_id=req.session_id,
                    trace_id=req.trace_id,
                    output=content,
                    status=HANDOFF_OK,
                    artifacts={
                        "retrieved": retrieved,
                        "rag_used": rag_used,
                    },
                )}
            except Exception as exc:
                logger.exception(
                    "RagPattern synthesize failed trace=%s", req.trace_id,
                )
                return {"handoff_response": HandoffResponse(
                    session_id=req.session_id,
                    trace_id=req.trace_id,
                    output="",
                    status=HANDOFF_ERROR,
                    error=f"{type(exc).__name__}: {exc}",
                    artifacts={"retrieved": retrieved, "rag_used": rag_used},
                )}

        graph = StateGraph(RagState)
        graph.add_node("retrieve", retrieve_node)
        graph.add_node("synthesize", synthesize_node)
        graph.set_entry_point("retrieve")
        graph.add_edge("retrieve", "synthesize")
        graph.add_edge("synthesize", END)
        return graph.compile()

    # ── Lazy resolution helpers ───────────────────────────────────────

    def _resolve_transport(self) -> Any:
        if self._transport is not None:
            return self._transport
        from paradise.factory import build_transport
        from paradise.config import ParadiseConfig
        return build_transport(ParadiseConfig())

    def _resolve_llm_config(self) -> "LLMConfig":
        if self._llm_config is not None:
            return self._llm_config
        from paradise.config import ParadiseConfig
        return ParadiseConfig().llm

    def _resolve_retriever(self) -> Retriever | None:
        """Return injected retriever, or attempt lazy resolve.

        Lazy resolve tries paradise.memory.qdrant_provider. On any
        import / construction error (typical in dev without qdrant
        running), returns None so synthesize_node runs in chat-fallback.
        """
        if self._retriever is not None:
            return self._retriever
        try:
            import os
            from paradise.memory.qdrant_provider import QdrantSemanticProvider
            url = os.environ.get(
                "QDRANT_URL",
                os.environ.get("QDRANT_API_URL", "http://localhost:6333"),
            )
            return QdrantSemanticProvider(url=url)  # type: ignore[return-value]
        except Exception as exc:
            logger.debug(
                "rag retriever lazy-resolve failed (%s) — chat-fallback",
                exc,
            )
            return None

    def _build_kwargs(
        self,
        cfg: "LLMConfig",
        req: HandoffRequest,
        retrieved: list[dict],
        rag_used: bool,
    ) -> dict:
        """Translate HandoffRequest + retrieved → transport.chat kwargs."""
        if rag_used and retrieved:
            context = _format_context(retrieved)
            system_prompt = _RAG_SYSTEM_PROMPT_WITH_CONTEXT.format(context=context)
        else:
            system_prompt = _RAG_SYSTEM_PROMPT_FALLBACK

        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": system_prompt,
            "messages": [{"role": "user", "content": req.message}],
            "temperature": 0.3,  # lower temp for grounded answers
        }
        transport = self._resolve_transport()
        if hasattr(transport, "api_mode"):
            mode = transport.api_mode
            if mode == "ollama_native":
                kwargs["api_url"] = cfg.api_url
            else:
                kwargs["api_url"] = cfg.api_url
                kwargs["api_key"] = cfg.api_key
                kwargs["max_tokens"] = cfg.max_tokens or 512
        return kwargs


# ── Module-level helpers ──────────────────────────────────────────────


def _format_context(hits: list[dict]) -> str:
    """Render retrieved hits into a numbered text block for the LLM.

    Each hit becomes:
        [1] (source: ...) content...
    Source is omitted when missing. Score is internal — never shown to
    the LLM (avoids anchoring on a number it can't validate).

    Empty-content hits are skipped and numbering is re-flowed so the LLM
    sees a contiguous [1] [2] [3] rather than gaps like [1] [3] [5]
    which could be misread as missing context.
    """
    lines: list[str] = []
    n = 0
    for hit in hits:
        content = str(hit.get("content", "")).strip()
        if not content:
            continue
        n += 1
        source = hit.get("source")
        prefix = f"[{n}]"
        if source:
            prefix += f" (source: {source})"
        lines.append(f"{prefix} {content}")
    return "\n\n".join(lines) if lines else "(no context retrieved)"


__all__ = ["RagPattern", "RagState", "Retriever"]
