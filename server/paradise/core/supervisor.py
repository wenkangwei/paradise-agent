"""Supervisor graph — top-level orchestration of agent execution patterns.

Topology:
    START
      ↓
    INTENT ──────────────────────────────────┐
      ↓                                       │
    HANDOFF (invoke subgraph via Handoff      │
             Protocol)                        │
      ↓                                       │
    REFLECT ─[retry?]──┐                      │
      ↓ (accept)        ↓ (retry, mode=chat)  │
    CLEANUP          HANDOFF                  │
      ↓                ↑                      │
    END ───────────────────────────────────── ┘

The supervisor owns:
  * Intent classification (delegated to IntentClassifier)
  * Intent → mode mapping (chitchat → chat, tool_* → tool_react, ...)
  * Handoff invocation (construct HandoffRequest, dispatch to subgraph,
    store HandoffResponse)
  * One-shot retry on error (fallback to chat subgraph)
  * Cleanup (placeholder for Phase 2.13 ATIF export + cache writeback)

The supervisor does NOT own:
  * Subgraph internals — communicated only via Handoff Protocol
  * LLM calls — those happen inside subgraphs
  * Tool execution — same

Coexistence with graph.py:
  The legacy build_agent_graph() (4-phase TOOL→THINK→RESPOND→REFLECT)
  is untouched. supervisor.py is a parallel implementation that
  ParadiseAgent can route to via a new flag (Phase 2.9+: supervisor_enabled).
  Both can coexist during the migration window.

Phase 2.9.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from paradise.core.handoff import (
    HANDOFF_DEPTH_CAPPED,
    HANDOFF_ERROR,
    HANDOFF_OK,
    HandoffRequest,
    HandoffResponse,
)
from paradise.core.registry import SubgraphRegistry
from paradise.emotion.engine import EmotionState

logger = logging.getLogger("paradise.supervisor")

# Phase 5: shared emotion placeholder for supervisor-path reflection.
# The supervisor doesn't own an EmotionEngine (that's dev path's job via
# LoopContext), but ReflectionEngine.on_turn_complete requires an
# EmotionState argument. This singleton is reused across turns —
# reflection's emotion delta (clamped to ±0.1) slowly drifts it, which
# is acceptable since supervisor-path emotion isn't surfaced to the user.
# When dev-path reflection runs, it passes its own EmotionState instead.
_SUPERVISOR_DEFAULT_EMOTION = EmotionState()


# ── Intent → mode mapping ────────────────────────────────────────────
#
# When the intent classifier returns a label, map it to an execution
# mode. The mapping is intentionally conservative: when in doubt, fall
# back to "chat" (cheapest, safest). Modes that aren't yet implemented
# (rag / plan_execute in Phase 2.9) also fall back to tool_react or
# chat — better to do *something* useful than crash because the ideal
# pattern is unavailable.
_INTENT_TO_MODE: dict[str, str] = {
    # ── Chat: no external tool needed ──────────────────────────────
    "chitchat":         "chat",
    "knowledge_qa":     "chat",   # LLM answers from parametric knowledge
    "creative":         "chat",   # writing/generation — pure LLM strength
    "unsafe":           "chat",   # constrained prompt (Phase 1 guardrails)

    # ── tool_react: needs one or more tools ────────────────────────
    "tool_search":      "tool_react",
    "tool_weather":     "tool_react",
    "tool_reminder":    "tool_react",
    "tool_translate":   "tool_react",

    # ── plan_execute: multi-step / analysis ────────────────────────
    "task_complex":     "plan_execute",

    # ── Catch-all: give the LLM tools so it CAN use them when needed ──
    # Defaulting to tool_react (not chat) because chat has no tools and
    # forces hallucination on tool-capable queries the classifier missed.
    # tool_react's system prompt says "call tools WHEN NEEDED" — if the
    # query is pure chat, the LLM just answers without calling anything.
    "unknown":          "tool_react",
}

# Default mode when intent lookup misses completely. Keeping this
# explicit so it's visible in code review rather than buried in a
# .get() fallback.
_DEFAULT_MODE = "chat"

# Maximum retry attempts on HANDOFF_ERROR. One retry is enough for the
# common case (transient transport failure); more retries risk burning
# cost on persistent failures.
_MAX_RETRIES = 1

# Hard cap on subgraph nesting depth. Supervisor is depth=0; its direct
# subgraphs are depth=1; subgraphs invoked BY those subgraphs are
# depth=2 (e.g., plan_execute → tool_react). Depth > _MAX_DEPTH is
# rejected with HANDOFF_DEPTH_CAPPED before any subgraph runs.
#
# Why 2:
#   * N=1 = no nesting at all (every subgraph is top-level only).
#   * N=2 covers 90% of real cases (plan_execute decomposes into a few
#     tool_react sub-tasks).
#   * N≥3 lets cost/latency spiral out of control at 1k DAU scale, and
#     makes debugging the handoff chain miserable.
_MAX_DEPTH = 2

# When mode_judge confidence exceeds this threshold, its mode choice
# overrides the intent → mode mapping. Kept here (not imported from
# mode_judge.py) so the fusion policy is visible at the supervisor level
# where it's actually applied. Must stay in sync with
# mode_judge._CONFIDENCE_OVERRIDE.
_MODE_JUDGE_OVERRIDE_THRESHOLD = 0.7


class SupervisorState(TypedDict, total=False):
    """Top-level state passed between supervisor nodes.

    Fields flow:
        Input:   user_message, session_id, trace_id, user_id, user_tier
        Intent:  intent, intent_confidence, intent_source, mode
        Handoff: handoff_request, handoff_response
        Reflect: retry_count, should_retry
        Output:  final_output
    """
    # Input (per-turn)
    user_message: str
    session_id: str
    trace_id: str
    user_id: str
    user_tier: str

    # Context bags (Phase 3-D — propagated from LoopContext via
    # run_via_supervisor so subgraphs have persona/history/memory).
    # See paradise.core.patterns.context_builder for consumption.
    conversation_history: list[dict]
    compact_context: str
    soul_md: str
    memory_md: str
    user_profile: str

    # Intent (set by intent_node)
    intent: str
    intent_confidence: float
    intent_source: str
    mode: str

    # Query preprocessing (set by preprocess_node — Phase 6)
    corrected_message: str        # typo-fixed query (== user_message when skipped)
    rewritten_queries: list[str]  # ≤3 LLM rewrites (empty when skipped/failed)

    # Complexity (set by intent_node via ComplexityAssessor — Phase 6)
    complexity: str               # "simple" | "complex" | "unknown"
    complexity_source: str        # "rule" | "llm" | "default"

    # Handoff (set by handoff_node)
    handoff_request: HandoffRequest
    handoff_response: HandoffResponse

    # Reflection (set by reflect_node)
    retry_count: int
    should_retry: bool

    # Output (set by cleanup_node)
    final_output: str

    # Phase 5: Memory + Reflection (set by cleanup_node)
    # Optional — only populated when memory_provider is wired.
    memory_synced: bool            # cleanup_node memory sync success flag

    # NOTE: the streaming sink (asyncio.Queue) is NOT stored in state —
    # it travels via a ContextVar (paradise.core.streaming) to avoid
    # msgpack-serialization crashes at the checkpointer boundary.


# ── Public entry point ───────────────────────────────────────────────


def build_supervisor_graph(
    registry: SubgraphRegistry,
    intent_classifier: Any | None = None,
    mode_judge: Any | None = None,
    checkpointer: Any | None = None,
    atif_exporter: Any | None = None,
    memory_provider: Any | None = None,
    reflection_engine: Any | None = None,
    preprocessor: Any | None = None,
    complexity_assessor: Any | None = None,
) -> Any:
    """Compile the supervisor StateGraph.

    Args:
        registry: SubgraphRegistry populated with at least one available
            pattern (typically `chat` after register_basic_patterns()).
            The supervisor will route to whatever mode intent_node picks,
            falling back to `chat` if the requested mode is unavailable.
        intent_classifier: optional IntentClassifier instance. When None,
            intent_node runs mode_judge alone (or falls back to mode=chat
            if mode_judge is also None). In production, pass
            paradise.factory.build_intent_classifier(cfg).
        mode_judge: optional ModeJudge instance (Phase 2.8). Runs in
            parallel with intent_classifier via asyncio.gather. When its
            confidence exceeds _MODE_JUDGE_OVERRIDE_THRESHOLD, its mode
            choice overrides the intent → mode mapping. When None,
            intent_node uses intent-only routing (Phase 2.9 behavior).
        checkpointer: LangGraph Checkpointer. Defaults to in-process
            MemorySaver (sessions lost on restart). Pass SqliteSaver /
            PostgresSaver from factory.py for persistence.
        atif_exporter: optional AtifExporter (Phase 2.13). When provided
            AND its config.enabled is True, cleanup_node builds a turn
            trace dict and queues it for asynchronous JSONL export.
            Default-None keeps cleanup_node at Phase 2.9 behavior —
            zero overhead, no trace built, no await. Fail-soft: any
            exporter error is swallowed so cleanup never fails.
        memory_provider: optional MemoryManager (Phase 5). When provided,
            cleanup_node calls sync_turn(user_msg, final_output) to write
            the turn to long-term memory. Fire-and-forget — errors swallowed.
        reflection_engine: optional ReflectionEngine (Phase 5). When
            provided, cleanup_node schedules on_turn_complete as a
            background asyncio task for post-turn fact extraction + memory.md
            writeback. Fire-and-forget, never awaits.
        preprocessor: optional QueryPreprocessor (Phase 6). When provided,
            preprocess_node runs before intent_node to correct typos and
            generate ≤3 query rewrites. Fail-soft — any error passes the
            raw message through.
        complexity_assessor: optional ComplexityAssessor (Phase 6). When
            provided, intent_node classifies the turn simple/complex after
            intent classification. Complex turns route to "agent_team"
            when that pattern is registered, else "plan_execute".

    Returns:
        Compiled LangGraph runnable. Invoke via:
            graph.ainvoke(
                {"user_message": "...", "session_id": "s1", ...},
                config={"configurable": {"thread_id": session_id}},
            )
    """
    graph = StateGraph(SupervisorState)

    # ── Node: PREPROCESS (Phase 6) ──────────────────────────────────
    async def preprocess_node(state: SupervisorState) -> dict:
        """Correct typos + generate query rewrites (reference flow step 1-2).

        Pass-through when preprocessor is None or the heuristic gate
        skips the message (short greetings etc.). Fail-soft: any error
        passes the raw message through unchanged.
        """
        from paradise.core.preprocess import should_preprocess

        message = state.get("user_message", "")
        if preprocessor is None or not should_preprocess(message):
            return {}
        try:
            result = await preprocessor.process(message)
            delta: dict[str, Any] = {}
            # Only store when the correction actually changed something —
            # keeps checkpoints small and intent_node's fallback simple.
            if result.corrected and result.corrected != message:
                delta["corrected_message"] = result.corrected
            if result.rewrites:
                delta["rewritten_queries"] = result.rewrites
            return delta
        except Exception as exc:
            logger.warning(
                "preprocess failed (%s) — using raw message trace=%s",
                exc, state.get("trace_id"),
            )
            return {}

    # ── Node: INTENT ─────────────────────────────────────────────────
    async def intent_node(state: SupervisorState) -> dict:
        """Classify intent + mode in parallel, then fuse + assess complexity.

        Fan-out via asyncio.gather(return_exceptions=True) so one
        failing classifier doesn't poison the other. Fail-soft at every
        layer: any None / exception falls back to chat.

        Fusion policy (see _fuse_intent_and_mode):
          * mode_judge conf > threshold → mode_judge wins
          * else → intent → mode mapping
          * intent unavailable → chat

        Phase 6: after fusion, ComplexityAssessor (when wired) classifies
        the turn simple/complex. Complex turns override the mode:
          * "agent_team" registered → agent_team
          * else mode == "chat"     → plan_execute
        """
        msg = (
            state.get("corrected_message")
            or state.get("user_message", "")
        )
        if not msg:
            fused = _mode_update("unknown", 0.0, "none", _DEFAULT_MODE)
            await _emit_intent_event(fused)
            return fused

        tasks: list = []
        if intent_classifier is not None:
            tasks.append(intent_classifier.classify(msg))
        if mode_judge is not None:
            tasks.append(mode_judge.classify(msg))

        if not tasks:
            logger.debug("intent_node: no classifier/judge → chat")
            fused = _mode_update("unknown", 0.0, "none", _DEFAULT_MODE)
            await _emit_intent_event(fused)
            return fused

        # Parallel fan-out. return_exceptions so a crashing judge doesn't
        # cancel a working classifier (asyncio.gather by default cancels
        # siblings on first exception).
        results = await asyncio.gather(*tasks, return_exceptions=True)

        intent_res: Any | None = None
        mode_res: Any | None = None
        idx = 0
        if intent_classifier is not None:
            r = results[idx]; idx += 1
            if isinstance(r, Exception):
                logger.warning("intent classifier raised: %s", r)
            else:
                intent_res = r
        if mode_judge is not None:
            r = results[idx]
            if isinstance(r, Exception):
                logger.warning("mode_judge raised: %s", r)
            else:
                mode_res = r

        fused = _fuse_intent_and_mode(
            intent_res,
            mode_res,
            intent_classifier_wired=intent_classifier is not None,
        )

        # ── Phase 6: complexity assessment + routing override ──────
        if complexity_assessor is not None:
            try:
                complexity = await complexity_assessor.assess(
                    msg, fused.get("intent", "unknown"),
                )
            except Exception as exc:
                logger.warning(
                    "complexity assessment raised (%s) — treating as "
                    "simple trace=%s", exc, state.get("trace_id"),
                )
                complexity = None

            if complexity is not None:
                fused["complexity"] = (
                    "complex" if complexity.is_complex else "simple"
                )
                fused["complexity_source"] = complexity.source
                if complexity.is_complex:
                    # list_modes() filters out unavailable stubs — the
                    # agent_team slot always exists (register_all), so a
                    # membership check alone would match the stub.
                    if "agent_team" in registry.list_modes():
                        fused["mode"] = "agent_team"
                        fused["intent_source"] += "+complexity"
                    elif fused.get("mode") == "chat":
                        # No agent_team available — at least decompose.
                        fused["mode"] = "plan_execute"
                        fused["intent_source"] += "+complexity"
                logger.info(
                    "complexity: %s (%s, %s) → mode=%s trace=%s",
                    fused["complexity"], complexity.source,
                    complexity.reason, fused.get("mode"),
                    state.get("trace_id"),
                )
        if "complexity" not in fused:
            fused["complexity"] = "unknown"
            fused["complexity_source"] = "none"

        await _emit_intent_event(fused)
        return fused

    # ── Node: HANDOFF ────────────────────────────────────────────────
    async def handoff_node(state: SupervisorState) -> dict:
        """Dispatch to the chosen subgraph via Handoff Protocol.

        Pure execution — no fallback logic here. All retry/fallback
        decisions live in reflect_node, so the routing policy is
        observable in a single place (easier to debug, easier to
        extend with LLM-as-judge later).
        """
        mode = state.get("mode", _DEFAULT_MODE)
        req = _build_handoff_request(state)
        response = await invoke_subgraph(registry, mode, req)
        return {
            "handoff_request": req,
            "handoff_response": response,
        }

    # ── Node: REFLECT ────────────────────────────────────────────────
    async def reflect_node(state: SupervisorState) -> dict:
        """Decide whether to retry or accept the subgraph response.

        Phase 2.9 policy: single retry on HANDOFF_ERROR by re-running
        handoff in chat mode (regardless of current mode). Subsequent
        failures are accepted as-is — the cleanup_node will surface the
        error message to the user.

        Future phases can extend this with:
          * Output quality critique (LLM-as-judge)
          * Tool-result validation
          * Multi-step plan progress checks

        Always sets retry_count so downstream nodes / observability
        have a stable field to read.
        """
        response: HandoffResponse | None = state.get("handoff_response")
        retry_count: int = state.get("retry_count", 0)
        current_mode: str = state.get("mode", _DEFAULT_MODE)

        if (
            response is not None
            and response.status == HANDOFF_ERROR
            and retry_count < _MAX_RETRIES
            and current_mode != _DEFAULT_MODE
        ):
            logger.info(
                "reflect: scheduling retry #%d (mode %s → chat) trace=%s",
                retry_count + 1, current_mode, state.get("trace_id"),
            )
            return {
                "retry_count": retry_count + 1,
                "mode": _DEFAULT_MODE,
                "should_retry": True,
            }

        return {
            "retry_count": retry_count,
            "should_retry": False,
        }

    # ── Node: CLEANUP ────────────────────────────────────────────────
    async def cleanup_node(state: SupervisorState) -> dict:
        """Persist turn artifacts and prepare final output.

        Phase 2.13: when atif_exporter is wired AND its config.enabled
        is True, build a turn trace dict and queue it for asynchronous
        JSONL export. The export is fire-and-forget — exporter.export()
        only awaits a `put_nowait` on an asyncio.Queue and never blocks
        on disk I/O. Fail-soft: any exporter error is swallowed so
        cleanup never fails the user-facing turn.

        Semantic cache writeback and reflection persistence hook in
        here in Phase 5 / later.
        """
        response: HandoffResponse | None = state.get("handoff_response")
        final_output = response.output if response else ""

        # On persistent error, surface a user-friendly message rather
        # than the raw exception string.
        if response and response.status == HANDOFF_ERROR and not final_output:
            final_output = (
                "Sorry, I encountered an issue handling that. "
                "Please try again."
            )

        logger.info(
            "cleanup trace=%s status=%s turns=%d output_len=%d",
            state.get("trace_id"),
            response.status if response else "missing",
            response.turns_used if response else 0,
            len(final_output),
        )

        result: dict[str, Any] = {"final_output": final_output}

        # ── Phase 5: Memory sync (fire-and-forget) ──────────────────
        # Write the turn to long-term memory via MemoryManager.sync_turn.
        # Sync call (MemoryManager is synchronous internally). Errors are
        # swallowed so memory failures never break the user-facing turn.
        if memory_provider is not None:
            try:
                memory_provider.sync_turn(
                    state.get("user_message", ""),
                    final_output,
                )
                result["memory_synced"] = True
            except Exception:
                logger.exception(
                    "memory sync_turn failed — trace=%s",
                    state.get("trace_id"),
                )

        # ── Phase 5: Reflection on_turn_complete (fire-and-forget) ──
        # Schedule post-turn reflection (fact extraction + memory.md
        # writeback) as a background asyncio task. Never awaited — the
        # user has already received the response by this point.
        if reflection_engine is not None:
            try:
                asyncio.create_task(reflection_engine.on_turn_complete(
                    agent_name=str(state.get("mode", "chat")),
                    user_message=state.get("user_message", ""),
                    agent_response=final_output,
                    emotion_state=_SUPERVISOR_DEFAULT_EMOTION,
                ))
            except Exception:
                logger.exception(
                    "reflection on_turn_complete scheduling failed — trace=%s",
                    state.get("trace_id"),
                )

        # ── Phase 2.13: ATIF export. Fast-path the disabled check so the
        # 99% case (enabled=False) does zero dict allocation.
        if atif_exporter is not None and getattr(atif_exporter, "enabled", False):
            trace = _build_atif_trace(state, final_output, response)
            try:
                await atif_exporter.export(trace)
            except Exception:
                # Exporter is contractually non-raising but be defensive
                # — never let telemetry break a successful turn.
                logger.exception(
                    "ATIF export raised — trace=%s", state.get("trace_id")
                )

        return result

    # ── Edges ────────────────────────────────────────────────────────
    graph.add_node("preprocess", preprocess_node)
    graph.add_node("intent", intent_node)
    graph.add_node("handoff", handoff_node)
    graph.add_node("reflect", reflect_node)
    graph.add_node("cleanup", cleanup_node)

    graph.set_entry_point("preprocess")
    graph.add_edge("preprocess", "intent")
    graph.add_edge("intent", "handoff")
    graph.add_edge("handoff", "reflect")

    # Conditional: retry loops back to handoff, otherwise proceed
    def _after_reflect(state: SupervisorState) -> str:
        return "handoff" if state.get("should_retry") else "cleanup"

    graph.add_conditional_edges(
        "reflect",
        _after_reflect,
        {"handoff": "handoff", "cleanup": "cleanup"},
    )
    graph.add_edge("cleanup", END)

    compiled = graph.compile(checkpointer=checkpointer or MemorySaver())
    logger.info(
        "Compiled supervisor graph: INTENT→HANDOFF→REFLECT→CLEANUP "
        "(intent_classifier=%s, mode_judge=%s, registry_size=%d)",
        "yes" if intent_classifier else "no",
        "yes" if mode_judge else "no",
        len(registry),
    )
    return compiled


# ── Internal helpers ─────────────────────────────────────────────────


def _mode_update(
    intent: str,
    confidence: float,
    source: str,
    mode: str,
) -> dict:
    """Single construction point for intent_node's state delta."""
    return {
        "intent": intent,
        "intent_confidence": confidence,
        "intent_source": source,
        "mode": mode,
    }


def _fuse_intent_and_mode(
    intent_res: Any | None,
    mode_res: Any | None,
    *,
    intent_classifier_wired: bool,
) -> dict:
    """Merge intent classifier + mode_judge results → state delta.

    Policy:
      * mode_judge conf > _MODE_JUDGE_OVERRIDE_THRESHOLD → mode_judge wins.
        intent_source gets "+llmjudge" suffix for observability.
      * else → intent → mode mapping (Phase 2.9 behavior preserved).
      * intent_res is None and classifier was wired → "error" source.
      * intent_res is None and classifier was NOT wired → "none" source.

    The `intent_classifier_wired` flag preserves the Phase 2.9 contract:
    when no classifier is passed, intent_source is "none" (not "error").
    """
    # Extract intent label / confidence / source
    if intent_res is not None:
        intent_label = (
            intent_res.intent.value
            if hasattr(intent_res, "intent") and hasattr(intent_res.intent, "value")
            else str(getattr(intent_res, "intent", "unknown"))
        )
        intent_conf = float(getattr(intent_res, "confidence", 0.0))
        intent_src = str(getattr(intent_res, "source", "unknown"))
    else:
        # No intent result — either classifier raised (error) or was
        # never wired (none). The distinction matters for observability.
        intent_label = "unknown"
        intent_conf = 0.0
        intent_src = "error" if intent_classifier_wired else "none"

    # Decide mode: intent mapping is the sole mode selector.
    # ModeJudge is queried for observability but never overrides —
    # qwen2.5:0.5b is too unreliable (classifies almost everything as
    # plan_execute with conf>0.7). Intent rules + LLM classifier are
    # sufficient and more accurate.
    chosen_mode = _INTENT_TO_MODE.get(intent_label, _DEFAULT_MODE)
    if mode_res is not None:
        logger.debug(
            "mode_judge observed (not applied): intent=%s → mode=%s "
            "(judge had: mode=%s conf=%.2f)",
            intent_label, chosen_mode,
            getattr(mode_res, "mode", "?"),
            float(getattr(mode_res, "confidence", 0.0)),
        )
    else:
        logger.info(
            "intent only: %s conf=%.2f src=%s → mode=%s",
            intent_label, intent_conf, intent_src, chosen_mode,
        )
    fused_source = intent_src

    return _mode_update(intent_label, intent_conf, fused_source, chosen_mode)


async def _emit_intent_event(fused: dict) -> None:
    """Push an intent event into the current task's streaming sink.

    The sink is bound via ``paradise.core.streaming.set_event_sink`` by
    ``run_via_supervisor`` and propagates automatically to nested nodes.
    Silent no-op when no sink is wired (unit tests, non-streaming callers).

    Shape matches the dev-path intent event so agent_handler SSE
    formatting needs no special-casing:
        {"type": "intent", "content": str, "confidence": float,
         "source": str, "mode": str}
    """
    from paradise.core.streaming import get_event_sink

    event_sink = get_event_sink()
    if event_sink is None:
        return
    try:
        await event_sink.put({
            "type": "intent",
            "content": fused.get("intent", "unknown"),
            "confidence": fused.get("intent_confidence", 0.0),
            "source": fused.get("intent_source", "none"),
            "mode": fused.get("mode", "chat"),
        })
    except Exception as exc:  # never let streaming break the graph
        logger.debug("event_sink intent put failed: %s", exc)


def _build_handoff_request(state: SupervisorState) -> HandoffRequest:
    """Translate supervisor state → HandoffRequest for the subgraph.

    Supervisor is always depth=0; the subgraph it invokes is depth=1.

    Phase 3-D: packs conversation_history, compact_context, soul_md,
    memory_md, user_profile into ``context`` so subgraphs (chat,
    tool_react, plan_execute) can build persona-aware prompts via
    paradise.core.patterns.context_builder.

    Note: the streaming sink travels via ContextVar (not context dict),
    so nested patterns (tool_react, plan_execute) pick it up automatically
    without any explicit propagation here.
    """
    return HandoffRequest(
        session_id=state.get("session_id", ""),
        user_id=state.get("user_id", ""),
        trace_id=state.get("trace_id", ""),
        user_tier=state.get("user_tier", "free"),
        message=(
            state.get("corrected_message") or state.get("user_message", "")
        ),
        context={
            "conversation_history": state.get("conversation_history") or [],
            "compact_context": state.get("compact_context") or "",
            "soul_md": state.get("soul_md") or "",
            "memory_md": state.get("memory_md") or "",
            "user_profile": state.get("user_profile") or "",
            "task_goal": state.get("user_message", ""),
            # Phase 6: preprocessed query rewrites — patterns that
            # benefit from multi-query coverage (rag, agent_team)
            # consume them; others ignore.
            "rewritten_queries": state.get("rewritten_queries") or [],
        },
        depth=1,
        parent_trace_id=None,
    )


def _build_atif_trace(
    state: SupervisorState,
    final_output: str,
    response: HandoffResponse | None,
) -> dict:
    """Build a turn trace dict for ATIF export.

    Schema per ARCHITECTURE_V2.md §7.2. Fields absent in state are
    emitted as None rather than omitted, so downstream SFT pipelines
    can rely on the schema shape being stable.

    latency_ms and cost_usd are best-effort: latency requires a
    start-time field in state (not yet wired — Phase 6 adds it); cost
    comes from HandoffResponse.cost_incurred_usd when the subgraph
    populates it (default 0.0).
    """
    req: HandoffRequest | None = state.get("handoff_request")
    mode = state.get("mode", _DEFAULT_MODE)
    return {
        "session_id": state.get("session_id", ""),
        "trace_id": state.get("trace_id", ""),
        # timestamp filled by exporter._format() if absent
        "user_message": state.get("user_message", ""),
        "intent": {
            "label": state.get("intent", "unknown"),
            "confidence": float(state.get("intent_confidence", 0.0)),
            "source": state.get("intent_source", "none"),
        },
        "mode": mode,
        "handoff_chain": _build_handoff_chain(req, response, mode),
        "turns": [
            {"role": "user", "content": state.get("user_message", "")},
            {"role": "assistant", "content": final_output},
        ],
        "cost_usd": float(response.cost_incurred_usd) if response else 0.0,
        "latency_ms": None,
        "retry_count": int(state.get("retry_count", 0)),
        "feedback": None,
    }


def _build_handoff_chain(
    req: HandoffRequest | None,
    response: HandoffResponse | None,
    mode: str,
) -> list[dict]:
    """Serialise the handoff request/response pair for ATIF.

    Dataclasses are kept structured here; _json_default in
    atif_exporter handles encoding. PII redaction is applied later
    in AtifExporter._redact_record.
    """
    if req is None and response is None:
        return []
    return [
        {
            "depth": 1,
            "mode": mode,
            "request": req,
            "response": response,
        }
    ]


async def invoke_subgraph(
    registry: SubgraphRegistry,
    mode: str,
    req: HandoffRequest,
) -> HandoffResponse:
    """Resolve mode → subgraph and invoke it with a HandoffRequest.

    PUBLIC — Phase 2.12 promoted this from `_invoke_subgraph` so that
    nested patterns (e.g., PlanExecutePattern delegating a step to
    tool_react) can use the exact same dispatch path as the supervisor,
    including the depth cap. This guarantees the depth budget is
    enforced in exactly one place regardless of who initiates the call.

    Fail-soft at every layer:
      0. req.depth > _MAX_DEPTH → HANDOFF_DEPTH_CAPPED (no subgraph runs)
      1. Mode not registered → synthetic error response
      2. Subgraph compiled=None (stub) → synthetic error response
      3. Subgraph raises → catch and synthesize error response
      4. Subgraph returns no handoff_response → synthesize from empty state

    The synthetic responses (except depth_capped) carry status=
    HANDOFF_ERROR so the supervisor's reflect_node can decide retry vs
    accept uniformly. depth_capped is distinct so callers can fall back
    to a cheaper path rather than retrying into the same wall.
    """
    # Depth cap — checked FIRST, before any subgraph resolution, so a
    # runaway nested call chain fails fast instead of cascading.
    if req.depth > _MAX_DEPTH:
        logger.warning(
            "invoke_subgraph: depth cap hit (depth=%d > max=%d) "
            "mode=%s trace=%s — returning depth_capped",
            req.depth, _MAX_DEPTH, mode, req.trace_id,
        )
        return HandoffResponse(
            session_id=req.session_id,
            trace_id=req.trace_id,
            output="",
            status=HANDOFF_DEPTH_CAPPED,
            error=f"depth {req.depth} exceeds max {_MAX_DEPTH}",
        )

    spec = registry.get(mode)
    if spec is None:
        return _error_response(req, f"mode {mode!r} not registered")
    if spec.compiled is None:
        # Unavailable pattern (stub or compile failure). Don't try to
        # build — registry.get() already attempted and skipped.
        return _error_response(
            req, f"mode {mode!r} has no compiled graph (stub or build failed)"
        )

    try:
        # Subgraph initial state MUST contain a `handoff` field per
        # the Handoff Protocol contract (see handoff.py docstring).
        final_state = await spec.compiled.ainvoke({"handoff": req})
    except Exception as exc:
        logger.exception(
            "subgraph %r raised during invoke trace=%s",
            mode, req.trace_id,
        )
        return _error_response(
            req, f"subgraph {mode!r} crashed: {type(exc).__name__}: {exc}"
        )

    response = final_state.get("handoff_response") if isinstance(final_state, dict) else None
    if response is None:
        # Contract violation — subgraph didn't emit handoff_response.
        # Don't crash; synthesize an error so reflect_node can retry.
        return _error_response(
            req,
            f"subgraph {mode!r} returned no handoff_response (contract violation)",
        )
    return response


def _error_response(req: HandoffRequest, error: str) -> HandoffResponse:
    """Construct a canonical error response tied to the originating request."""
    return HandoffResponse(
        session_id=req.session_id,
        trace_id=req.trace_id,
        output="",
        status=HANDOFF_ERROR,
        error=error,
    )


__all__ = [
    "SupervisorState",
    "build_supervisor_graph",
    "invoke_subgraph",
]
