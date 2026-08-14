"""ToolReactPattern — ReAct loop with tool calling.

ReAct = Reason + Act. The pattern loops:
    1. LLM is given the user message + tool definitions + scratchpad
    2. LLM either returns final text (→ exit) or tool_calls (→ step 3)
    3. Each tool_call is dispatched via ToolRegistry.dispatch_async
    4. Tool results are appended to messages as role="tool"
    5. Loop back to step 1, with the tool results now in context

Bounded by max_turns (default 3). Exceeding max_turns returns the
latest assistant content as a best-effort answer with status=
HANDOFF_MAX_TURNS, so the user sees something useful rather than an error.

Design notes:
  * Single node + conditional edge (self-loop). Cleaner than separate
    think/act nodes because the LLM call and the tool dispatch are
    tightly coupled — splitting them would require serialising
    intermediate state between nodes for no benefit.
  * Cost cap is enforced BEFORE the LLM call by checking accumulated
    turns, not via actual USD tracking (which would need token-level
    accounting, deferred to Phase 6.3 metrics). When max_turns is hit,
    the loop exits with HANDOFF_MAX_TURNS.
  * Tools come from ToolRegistry — same instance the rest of paradise
    uses. No tool definitions are baked into the pattern, so adding a
    new tool requires zero changes here.

Phase 2.10.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from langgraph.graph import END, StateGraph

from paradise.core.handoff import (
    HANDOFF_ERROR,
    HANDOFF_MAX_TURNS,
    HANDOFF_OK,
    HandoffRequest,
    HandoffResponse,
)
from paradise.core.patterns.base import SubgraphPattern

if TYPE_CHECKING:
    from paradise.config import LLMConfig
    from paradise.core.registry import SubgraphRegistry
    from paradise.tools.registry import ToolRegistry

logger = logging.getLogger("paradise.patterns.tool_react")


# ReAct system prompt — biased toward action. The "if no tool needed,
# answer directly" clause is critical: without it the LLM will sometimes
# loop calling tools forever even when it has enough info.
_REACT_SYSTEM_PROMPT = (
    "You are a helpful assistant that can call tools when needed. "
    "Reason step by step about whether a tool is necessary. "
    "If a tool helps, call it; if you already have enough information, "
    "answer the user directly without calling any tool."
)


class ToolReactState(TypedDict, total=False):
    """ReAct subgraph state.

    Fields:
        handoff:          incoming request.
        messages:         OpenAI-format message list. Seeded with the user
                          message; grows as the LLM produces assistant turns
                          and tool results.
        iterations:       number of ReAct loops completed. Bounded by
                          pattern.max_turns.
        handoff_response: emitted on exit (final answer or error).
    """
    handoff: HandoffRequest
    messages: list[dict]
    iterations: int
    handoff_response: HandoffResponse


class ToolReactPattern(SubgraphPattern):
    """ReAct loop with bounded iterations."""

    name: ClassVar[str] = "tool_react"
    description: ClassVar[str] = (
        "ReAct 循环 — 工具调用 / 用户请求执行操作"
    )
    category: ClassVar[str] = "basic"
    cost_budget_usd: ClassVar[float] = 0.05
    max_turns: ClassVar[int] = 3

    def __init__(
        self,
        transport: Any | None = None,
        llm_config: "LLMConfig | None" = None,
        tool_registry: "ToolRegistry | None" = None,
    ) -> None:
        """
        Args:
            transport: LLM transport. None → lazy resolve via factory.
            llm_config: model config. None → lazy resolve.
            tool_registry: ToolRegistry instance. None → lazy resolve
                via paradise.tools.registry.registry singleton.
        """
        self._transport = transport
        self._llm_config = llm_config
        self._tool_registry = tool_registry

    def build(self, registry: "SubgraphRegistry") -> Any:
        """Compile the ReAct graph: think_act node self-loops on tool_calls."""
        # Capture self at graph-build time so the node closure has stable
        # references even if the pattern instance is mutated later.
        pattern = self

        async def think_act_node(state: ToolReactState) -> dict:
            req: HandoffRequest = state["handoff"]
            messages: list[dict] = list(state.get("messages") or [])
            iterations: int = state.get("iterations", 0)

            # Seed messages on first iteration
            if not messages:
                from paradise.core.patterns.context_builder import (
                    extract_history_messages,
                )
                history = extract_history_messages(req)
                messages = history + [{"role": "user", "content": req.message}]

            # Bounded loop exit
            if iterations >= pattern.max_turns:
                logger.warning(
                    "tool_react hit max_turns=%d trace=%s",
                    pattern.max_turns, req.trace_id,
                )
                return {
                    "handoff_response": _build_response(
                        req,
                        output=_extract_last_assistant_text(messages),
                        status=HANDOFF_MAX_TURNS,
                        error=f"exceeded max_turns={pattern.max_turns}",
                        turns_used=iterations,
                    )
                }

            try:
                transport = pattern._resolve_transport()
                cfg = pattern._resolve_llm_config()
                tool_reg = pattern._resolve_tool_registry()
                tools = _collect_tool_definitions(tool_reg)
                kwargs = pattern._build_kwargs(cfg, messages, tools, req)
                result = await transport.chat(**kwargs)
            except Exception as exc:
                logger.exception(
                    "tool_react LLM call failed trace=%s iter=%d",
                    req.trace_id, iterations,
                )
                return {
                    "handoff_response": _build_response(
                        req,
                        output=_extract_last_assistant_text(messages),
                        status=HANDOFF_ERROR,
                        error=f"{type(exc).__name__}: {exc}",
                        turns_used=iterations,
                    )
                }

            tool_calls = getattr(result, "tool_calls", None) or []
            content = (
                result.content if hasattr(result, "content")
                else str(result)
            )

            if not tool_calls:
                # No tool calls = LLM has a final answer. Exit.
                return {
                    "iterations": iterations + 1,
                    "handoff_response": _build_response(
                        req,
                        output=content or "",
                        status=HANDOFF_OK,
                        turns_used=iterations + 1,
                        artifacts={"messages": messages + (
                            [{"role": "assistant", "content": content}]
                            if content else []
                        )},
                    ),
                }

            # Execute each tool call, append assistant turn + tool results
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": tc.id or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            }
            messages.append(assistant_msg)

            for i, tc in enumerate(tool_calls):
                try:
                    args = json.loads(tc.arguments) if tc.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                t0 = time.monotonic()
                try:
                    tool_result = await tool_reg.dispatch_async(tc.name, args)
                except Exception as exc:
                    # Tool dispatch failures become structured error strings
                    # so the LLM can react to them on the next loop.
                    tool_result = json.dumps(
                        {"error": f"tool {tc.name} failed: {exc}"}
                    )
                elapsed_ms = int((time.monotonic() - t0) * 1000)

                # Phase 3-C: stream tool execution to caller in real-time.
                # The sink is bound via ContextVar by run_via_supervisor
                # (avoids msgpack-serialization issues at the checkpoint
                # boundary). Shape matches dev-path _tool_phase so
                # agent_handler SSE formatting is identical.
                from paradise.core.streaming import get_event_sink

                event_sink = get_event_sink()
                if event_sink is not None:
                    try:
                        await event_sink.put({
                            "type": "tool_call",
                            "name": tc.name,
                            "arguments": args,
                            "result": tool_result[:500],
                            "duration_ms": elapsed_ms,
                        })
                    except Exception as exc:  # never break the loop
                        logger.debug(
                            "event_sink tool_call put failed: %s", exc
                        )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id or f"call_{i}",
                    "content": tool_result,
                })

            logger.debug(
                "tool_react loop trace=%s iter=%d tools=%s",
                req.trace_id,
                iterations + 1,
                [tc.name for tc in tool_calls],
            )
            return {"messages": messages, "iterations": iterations + 1}

        def should_continue(state: ToolReactState) -> str:
            """Route after think_act: exit when we have a response, else loop."""
            if state.get("handoff_response") is not None:
                return END
            return "think_act"

        graph = StateGraph(ToolReactState)
        graph.add_node("think_act", think_act_node)
        graph.set_entry_point("think_act")
        graph.add_conditional_edges(
            "think_act",
            should_continue,
            {END: END, "think_act": "think_act"},
        )
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
        # tool_llm has lower temperature — better for structured tool calls
        return ParadiseConfig().tool_llm

    def _resolve_tool_registry(self) -> "ToolRegistry":
        if self._tool_registry is not None:
            return self._tool_registry
        from paradise.tools.registry import registry as global_registry
        # Discover builtin tools on first use. Idempotent — re-importing
        # already-registered tools is a no-op (registry rejects shadows).
        from paradise.tools.registry import discover_builtin_tools
        discover_builtin_tools()
        return global_registry

    def _build_kwargs(
        self,
        cfg: "LLMConfig",
        messages: list[dict],
        tools: list[dict],
        req: HandoffRequest | None = None,
    ) -> dict:
        """Translate to transport.chat kwargs.

        Phase 3-D: when ``req`` is passed, uses context_builder to inject
        persona/memory into the system prompt. When req is None (backward
        compat with tests that don't pass it), falls back to bare prompt.

        Separate method for testability — tests can verify kwargs without
        invoking transport.
        """
        if req is not None:
            from paradise.core.patterns.context_builder import (
                build_subgraph_system_prompt,
            )
            system_prompt = build_subgraph_system_prompt(req, _REACT_SYSTEM_PROMPT)
        else:
            system_prompt = _REACT_SYSTEM_PROMPT

        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": system_prompt,
            "messages": messages,
            "temperature": 0.3,
        }
        if tools:
            kwargs["tools"] = tools
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


def _build_response(
    req: HandoffRequest,
    *,
    output: str,
    status: str,
    turns_used: int = 0,
    error: str | None = None,
    artifacts: dict | None = None,
) -> HandoffResponse:
    """Single construction point for HandoffResponse — ensures session_id
    / trace_id always echo the request."""
    return HandoffResponse(
        session_id=req.session_id,
        trace_id=req.trace_id,
        output=output,
        status=status,
        turns_used=turns_used,
        error=error,
        artifacts=artifacts or {},
    )


def _extract_last_assistant_text(messages: list[dict]) -> str:
    """Best-effort: pull text from the last assistant message.

    Used as graceful-degradation output when the loop exits via
    max_turns or error — better to show partial progress than nothing.
    """
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content:
                return content
    return ""


def _collect_tool_definitions(tool_reg: "ToolRegistry") -> list[dict]:
    """Return OpenAI-format tool definitions for all available tools.

    Filters by check_fn() just like agent.py does — unavailable tools
    (missing deps / disabled env) are hidden from the LLM so it can't
    attempt to call them.
    """
    entries = tool_reg._snapshot_entries()
    names = {entry.name for entry in entries}
    if not names:
        return []
    return tool_reg.get_definitions(names)


__all__ = ["ToolReactPattern", "ToolReactState"]
