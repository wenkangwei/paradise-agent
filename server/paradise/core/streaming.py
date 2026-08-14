"""Phase 3-C streaming channel via ContextVar.

Why a ContextVar (not graph state)?
    The supervisor's checkpointer serializes state with msgpack. An
    ``asyncio.Queue`` is not serializable, so putting it in state crashes
    the graph at the checkpoint boundary. A ContextVar sidesteps
    serialization entirely: it lives in the task's context, propagates
    automatically into ``asyncio.create_task`` children (so nodes running
    inside ``compiled.ainvoke`` see it), and is invisible to LangGraph.

Lifecycle:
    * ``run_via_supervisor`` creates the Queue and calls
      ``set_event_sink(queue)`` BEFORE spawning ``invoke_task``.
    * Nodes (``_emit_intent_event``, ``tool_react``) call
      ``get_event_sink()``; returns ``None`` when nothing is wired
      (unit tests, non-streaming callers).
    * The caller drains the queue via the local Queue reference.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Optional

# None when no streaming sink is wired in the current task.
_event_sink: ContextVar[Optional["asyncio.Queue"]] = ContextVar(
    "paradise_event_sink", default=None
)


def set_event_sink(queue: Optional[asyncio.Queue]) -> None:
    """Bind an asyncio.Queue as the current task's streaming sink."""
    _event_sink.set(queue)


def get_event_sink() -> Optional[asyncio.Queue]:
    """Return the bound sink, or None when no streaming caller is active."""
    return _event_sink.get()
