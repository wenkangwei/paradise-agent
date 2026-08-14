"""ChatPattern — minimal single-shot LLM reply pattern.

This is the simplest possible SubgraphPattern: one node, one LLM call,
no tools, no loops. Used as the default fallback when:
  * mode_judge classifies intent as chitchat / simple Q&A
  * any other pattern fails to compile (registry returns compiled=None)
  * supervisor receives an unknown mode from intent_node

Design choices:
  * Single node, no conditional edges. Keeps the graph trivially
    debuggable — if chat goes wrong, the bug is in the LLM call kwargs.
  * Transport is constructor-injected. Tests pass a mock; production
    lazy-resolves via paradise.factory.build_transport on first use.
    This decouples the pattern class from the runtime config, which
    matters because SubgraphRegistry.register() happens at startup
    before config is fully resolved.
  * Errors become HandoffResponse(status=HANDOFF_ERROR) rather than
    exceptions. Supervisor treats this as soft failure and shows
    graceful degradation to the user.

Phase 2.10.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

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

logger = logging.getLogger("paradise.patterns.chat")


# Minimal system prompt — kept short to save tokens. The supervisor's
# INTENT node has already classified this as chitchat, so we don't need
# the full system prompt here. Tools / planning instructions would be
# actively harmful (they'd push the LLM toward tool calls in a path
# that has no tool execution wired up).
_CHAT_SYSTEM_PROMPT = "You are a helpful, concise assistant. Reply directly."


class ChatState(TypedDict, total=False):
    """Chat subgraph state.

    Intentionally tiny: handoff in, handoff_response out. No need to
    carry history within the chat subgraph — if multi-turn context is
    required, the supervisor passes it via HandoffRequest.context.
    """
    handoff: HandoffRequest
    handoff_response: HandoffResponse


class ChatPattern(SubgraphPattern):
    """Single-shot LLM reply."""

    name: ClassVar[str] = "chat"
    description: ClassVar[str] = (
        "单次 LLM 回复 — 闲聊 / 简单 Q&A / 通用兜底"
    )
    category: ClassVar[str] = "basic"
    cost_budget_usd: ClassVar[float] = 0.01
    max_turns: ClassVar[int] = 1

    def __init__(
        self,
        transport: Any | None = None,
        llm_config: "LLMConfig | None" = None,
    ) -> None:
        """
        Args:
            transport: LLM transport instance (has .chat method). When None,
                build() lazy-resolves via paradise.factory on first invoke.
                Tests MUST inject a mock — the lazy path requires a fully
                configured ParadiseConfig and a live LLM endpoint.
            llm_config: model/api_url/api_key config. When None, lazy-resolves
                to ParadiseConfig().llm at invoke time.
        """
        self._transport = transport
        self._llm_config = llm_config

    def build(self, registry: "SubgraphRegistry") -> Any:
        """Compile the chat graph: entry_node → END."""
        async def chat_node(state: ChatState) -> dict:
            req: HandoffRequest = state["handoff"]
            try:
                transport = self._resolve_transport()
                cfg = self._resolve_llm_config()
                kwargs = self._build_kwargs(cfg, req)
                result = await transport.chat(**kwargs)
                content = (
                    result.content if hasattr(result, "content")
                    else str(result)
                )
                logger.debug(
                    "chat reply trace=%s len=%d", req.trace_id, len(content)
                )
                return {"handoff_response": HandoffResponse(
                    session_id=req.session_id,
                    trace_id=req.trace_id,
                    output=content,
                    status=HANDOFF_OK,
                )}
            except Exception as exc:
                # Catch broadly: transport errors, JSON encode failures,
                # missing config. All map to HANDOFF_ERROR — the supervisor
                # surfaces a graceful message rather than crashing.
                logger.exception(
                    "ChatPattern LLM call failed trace=%s", req.trace_id
                )
                return {"handoff_response": HandoffResponse(
                    session_id=req.session_id,
                    trace_id=req.trace_id,
                    output="",
                    status=HANDOFF_ERROR,
                    error=f"{type(exc).__name__}: {exc}",
                )}

        graph = StateGraph(ChatState)
        graph.add_node("chat", chat_node)
        graph.set_entry_point("chat")
        graph.add_edge("chat", END)
        return graph.compile()

    # ── Lazy resolution helpers ───────────────────────────────────────

    def _resolve_transport(self) -> Any:
        """Return injected transport, or lazy-resolve from factory.

        Lazy path requires ParadiseConfig to be loadable. In tests,
        inject transport via constructor to avoid this.
        """
        if self._transport is not None:
            return self._transport
        from paradise.factory import build_transport
        from paradise.config import ParadiseConfig
        # build_transport reads config.prod.yaml when mode="prod";
        # the dev fallback uses default LLMConfig which points at
        # localhost ollama — fine for a best-effort chat path.
        return build_transport(ParadiseConfig())

    def _resolve_llm_config(self) -> "LLMConfig":
        if self._llm_config is not None:
            return self._llm_config
        from paradise.config import ParadiseConfig
        return ParadiseConfig().llm

    def _build_kwargs(self, cfg: "LLMConfig", req: HandoffRequest) -> dict:
        """Translate HandoffRequest → transport.chat kwargs.

        Phase 3-D: uses context_builder to inject persona/memory/profile
        into system prompt and prepend conversation history. When context
        is empty (tests, first turn), behavior is identical to Phase 2.10.
        """
        from paradise.core.patterns.context_builder import (
            build_subgraph_system_prompt,
            extract_history_messages,
        )

        system_prompt = build_subgraph_system_prompt(req, _CHAT_SYSTEM_PROMPT)
        history = extract_history_messages(req)
        messages = history + [{"role": "user", "content": req.message}]

        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": system_prompt,
            "messages": messages,
            "temperature": 0.7,
        }
        # Provider-specific kwargs — mirrors agent.py's _think_phase pattern.
        if hasattr(self._resolve_transport(), "api_mode"):
            mode = self._resolve_transport().api_mode
            if mode == "ollama_native":
                kwargs["api_url"] = cfg.api_url
            else:
                kwargs["api_url"] = cfg.api_url
                kwargs["api_key"] = cfg.api_key
                kwargs["max_tokens"] = cfg.max_tokens or 512
        return kwargs


__all__ = ["ChatPattern", "ChatState"]
