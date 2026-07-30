"""SSE (Server-Sent Events) channel adapter for Paradise.

Wraps the existing FastAPI StreamingResponse SSE pattern used by
chat_paradise.py.  Provides:
  - asyncio.Queue-per-session management (register / unregister)
  - Pending-message buffer for sessions whose queue hasn't been registered yet
  - An async stream generator that yields ``data: {json}\\n\\n`` frames
  - Inbound normalization from ChatRequest-like dicts to InboundMessage

Reference: chat_paradise.py ``_sse()`` and ``generate()`` patterns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from paradise.adapters.base import ChannelAdapter, InboundMessage, SendResult

logger = logging.getLogger(__name__)

# ── Module-level adapter registry ──────────────────────────────────────
# Import global registry from package
from paradise.adapters import register_adapter


# ── SSE helper ─────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    """Format a dict as an SSE ``data:`` line.

    Mirrors chat_paradise._sse(): ``data: {json}\\n\\n``
    """
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── SSEAdapter ─────────────────────────────────────────────────────────

class SSEAdapter(ChannelAdapter):
    """Channel adapter for Server-Sent Events (browser SSE connections).

    Unlike push-based platforms (Feishu, Discord), SSE is a pull model
    driven by the client's EventSource / fetch stream.  The adapter
    therefore manages a per-session asyncio.Queue that a FastAPI
    StreamingResponse generator consumes.
    """

    name = "sse"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        # chat_id -> asyncio.Queue[dict | None]
        self._queues: dict[str, asyncio.Queue[dict | None]] = {}
        # Pending messages for sessions whose queue hasn't been registered yet.
        # chat_id -> list[dict]
        self._pending: dict[str, list[dict]] = {}

    # ── Lifecycle (no-op for SSE) ──────────────────────────────────

    async def connect(self) -> bool:
        """SSE requires no pre-connection; always returns True."""
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Clear all queues and pending buffers."""
        self._queues.clear()
        self._pending.clear()
        self._mark_disconnected()

    # ── Sending ────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Push a message dict into the session's queue.

        If the session has no registered queue yet the message is
        buffered in ``_pending`` so nothing is lost.
        """
        msg: dict = {"content": content, "done": False}
        if reply_to:
            msg["reply_to"] = reply_to
        msg.update(kwargs)

        queue = self._queues.get(chat_id)
        if queue is not None:
            await queue.put(msg)
        else:
            self._pending.setdefault(chat_id, []).append(msg)
            logger.debug("SSE: queued pending message for %s", chat_id)

        return SendResult(success=True, message_id=str(id(msg)))

    # ── Inbound normalization ──────────────────────────────────────

    def normalize_event(self, raw_event: Any) -> InboundMessage | None:
        """Convert a ChatRequest-like dict to InboundMessage.

        Expected keys: session_id, content, agent_id, mode, agent_ids.
        """
        if not isinstance(raw_event, dict):
            return None

        content = raw_event.get("content")
        if not content:
            return None

        return InboundMessage(
            user_id="user",
            content=content,
            platform="sse",
            chat_id=raw_event.get("session_id") or raw_event.get("agent_id", ""),
            chat_type="dm" if raw_event.get("mode") == "private" else "group",
            message_type="text",
            raw_event=raw_event,
        )

    # ── Queue management ───────────────────────────────────────────

    def register_queue(self, chat_id: str, queue: asyncio.Queue[dict | None]) -> None:
        """Register an asyncio.Queue for *chat_id*.

        Flushes any pending messages that accumulated before registration.
        """
        self._queues[chat_id] = queue
        pending = self._pending.pop(chat_id, [])
        for msg in pending:
            queue.put_nowait(msg)
        if pending:
            logger.debug("SSE: flushed %d pending messages for %s", len(pending), chat_id)

    def unregister_queue(self, chat_id: str) -> None:
        """Remove the queue for *chat_id* and discard pending messages."""
        self._queues.pop(chat_id, None)
        self._pending.pop(chat_id, None)

    # ── Stream generator ───────────────────────────────────────────

    def get_stream_generator(self, chat_id: str) -> AsyncIterator[str]:
        """Return an async generator that yields SSE-formatted strings.

        A sentinel ``None`` value in the queue signals end-of-stream.
        Usage::

            adapter.register_queue(chat_id, q)
            return StreamingResponse(
                adapter.get_stream_generator(chat_id),
                media_type="text/event-stream",
            )
        """
        return self._generate(chat_id)

    async def _generate(self, chat_id: str) -> AsyncIterator[str]:
        """Core generator: reads from the session queue, yields SSE frames."""
        queue = self._queues.get(chat_id)
        if queue is None:
            logger.warning("SSE: no queue registered for %s", chat_id)
            return

        try:
            while True:
                event = await queue.get()
                if event is None:
                    # Sentinel: end of stream
                    break
                yield _sse(event)
        except asyncio.CancelledError:
            logger.debug("SSE: stream cancelled for %s", chat_id)
        finally:
            self.unregister_queue(chat_id)


# ── Self-register ──────────────────────────────────────────────────────
register_adapter("sse", SSEAdapter)
