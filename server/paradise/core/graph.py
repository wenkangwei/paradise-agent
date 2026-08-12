"""LangGraph orchestration — prod-only replacement for handle_message().

Coexistence strategy: this module is imported lazily ONLY by
ParadiseAgent.run_via_graph(), which itself is only called when
ParadiseConfig.langgraph_enabled is True. Main-branch code paths
(handle_message) are untouched.

Graph topology (1:1 with the existing 4-phase loop):
    START
      ↓
    TOOL ──→ THINK ──→ RESPOND ──→ REFLECT ──→ END

Each node is a thin async wrapper around the corresponding
ParadiseAgent._xxx_phase() method. State carries LoopContext-equivalent
fields + accumulated events.

Checkpointer: MemorySaver by default (in-process). For cross-restart
persistence, pass SqliteSaver or PostgresSaver from factory.py.

Phase 2 — LangGraph orchestration replacement.
"""
from __future__ import annotations

import logging
from operator import add
from typing import Annotated, Any, AsyncGenerator, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger("paradise.graph")


class AgentState(TypedDict, total=False):
    """State passed between nodes. Only the input fields are required;
    intermediate fields accumulate as the graph progresses."""

    # Input (per-turn)
    user_message: str
    agent_id: str
    session_id: str
    enable_tools: bool

    # Intermediate accumulators
    tool_results: str
    thinking_text: str
    response_text: str

    # Events yielded (for SSE streaming).
    # Annotated with `add` reducer so each node returns ONLY its new events,
    # and LangGraph concatenates them into the accumulated state list.
    events: Annotated[list[dict], add]

    # Cross-turn persistent state (serialisable — survives in checkpointer)
    facts: Annotated[list[str], add]
    user_profile: str


def _rebuild_ctx(agent, state: dict[str, Any]):
    """Reconstruct LoopContext from graph state.

    The full LoopContext isn't trivially serialisable (has Channel ref),
    so the graph only persists the subset needed for cross-turn continuity.
    For per-turn execution, we rebuild a minimal LoopContext that the
    existing _xxx_phase methods can consume.
    """
    from paradise.core.context import LoopContext

    return LoopContext(
        agent_id=state.get("agent_id", agent.agent_id),
        agent_name=agent.agent_id,
        session_id=state.get("session_id", ""),
        user_message=state.get("user_message", ""),
        enable_tools=state.get("enable_tools", True),
    )


def build_agent_graph(agent, checkpointer=None):
    """Compile a LangGraph StateGraph that mirrors handle_message()'s 4 phases.

    Args:
        agent: ParadiseAgent instance — its _xxx_phase methods are wrapped as nodes.
        checkpointer: langgraph Checkpointer (MemorySaver default). For prod,
            pass SqliteSaver or PostgresSaver from factory.py.

    Returns:
        Compiled LangGraph runnable. Invoke via:
            graph.astream(initial_state, config={"configurable": {"thread_id": sid}})
    """
    graph = StateGraph(AgentState)

    # ── Node: TOOL ────────────────────────────────────────────────
    async def tool_node(state: AgentState) -> dict:
        ctx = _rebuild_ctx(agent, state)
        events: list[dict] = []
        tool_results = ""
        try:
            async for event in agent._tool_phase(ctx):
                events.append(event)
                if event.get("type") == "tool_results":
                    tool_results = event.get("content", "")
        except Exception as exc:
            logger.exception("TOOL node error: %s", exc)
            events.append({"type": "error", "phase": "tool", "content": str(exc)})
        return {"tool_results": tool_results, "events": events}

    # ── Node: THINK ───────────────────────────────────────────────
    async def think_node(state: AgentState) -> dict:
        # Honour skip_think flag (simple greetings)
        from paradise.core.agent import _should_think
        if not _should_think(state.get("user_message", "")):
            return {"thinking_text": ""}
        ctx = _rebuild_ctx(agent, state)
        try:
            thinking = await agent._think_phase(ctx, state.get("tool_results", ""))
        except Exception as exc:
            logger.exception("THINK node error: %s", exc)
            thinking = ""
        new_events: list[dict] = []
        if thinking:
            new_events.append({"type": "thinking", "content": thinking})
        return {"thinking_text": thinking, "events": new_events}

    # ── Node: RESPOND ─────────────────────────────────────────────
    async def respond_node(state: AgentState) -> dict:
        ctx = _rebuild_ctx(agent, state)
        new_events: list[dict] = []
        response_text = ""
        try:
            async for event in agent._respond_phase(
                ctx,
                state.get("tool_results", ""),
                state.get("thinking_text", ""),
            ):
                new_events.append(event)
                if event.get("type") == "done":
                    response_text = event.get("content", "")
        except Exception as exc:
            logger.exception("RESPOND node error: %s", exc)
            new_events.append({"type": "error", "phase": "respond", "content": str(exc)})
        return {"response_text": response_text, "events": new_events}

    # ── Node: REFLECT ─────────────────────────────────────────────
    async def reflect_node(state: AgentState) -> dict:
        ctx = _rebuild_ctx(agent, state)
        try:
            await agent._reflection_phase(ctx)
        except Exception as exc:
            logger.warning("REFLECT node error (non-fatal): %s", exc)
        return {}  # no state update — reflection is fire-and-forget

    graph.add_node("tool", tool_node)
    graph.add_node("think", think_node)
    graph.add_node("respond", respond_node)
    graph.add_node("reflect", reflect_node)

    graph.set_entry_point("tool")
    graph.add_edge("tool", "think")
    graph.add_edge("think", "respond")
    graph.add_edge("respond", "reflect")
    graph.add_edge("reflect", END)

    compiled = graph.compile(checkpointer=checkpointer or MemorySaver())
    logger.info("Compiled agent graph: TOOL→THINK→RESPOND→REFLECT")
    return compiled


async def stream_graph_events(
    compiled_graph,
    initial_state: dict,
    thread_id: str,
) -> AsyncGenerator[dict, None]:
    """Stream events from the compiled graph, yielding each as an SSE-ready dict.

    Uses stream_mode="updates" so we get state diffs per node.
    The `events` list inside each node's update is yielded one-by-one.
    """
    config = {"configurable": {"thread_id": thread_id}}
    async for chunk in compiled_graph.astream(
        initial_state,
        config=config,
        stream_mode="updates",
    ):
        # chunk is {node_name: state_update_dict}
        for _node_name, update in chunk.items():
            for event in (update or {}).get("events", []) or []:
                yield event
