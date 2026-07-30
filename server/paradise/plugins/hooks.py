"""Hook system — event definitions and executor."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class HookEvent(Enum):
    """All hookable events in the Paradise framework."""
    PRE_MESSAGE = "pre_message"
    POST_MESSAGE = "post_message"
    PRE_TOOL_CALL = "pre_tool_call"
    POST_TOOL_CALL = "post_tool_call"
    PRE_RESPONSE = "pre_response"
    POST_RESPONSE = "post_response"
    ON_HEARTBEAT = "on_heartbeat"
    ON_REFLECTION = "on_reflection"
    ON_REFLECTION_COMPLETE = "on_reflection_complete"


@dataclass
class HookContext:
    """Context passed to hook handlers."""
    event: HookEvent
    agent_id: str = ""
    data: dict = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}


# Type alias for hook handlers
HookHandler = Callable[[HookContext], Any]


class HookExecutor:
    """Executes hooks for a given event."""

    def __init__(self):
        self._hooks: dict[HookEvent, list[HookHandler]] = {}

    def register(self, event: HookEvent, handler: HookHandler) -> None:
        """Register a hook handler for an event."""
        if event not in self._hooks:
            self._hooks[event] = []
        self._hooks[event].append(handler)

    async def fire(self, event: HookEvent, context: HookContext) -> HookContext:
        """Fire all hooks for an event. Returns modified context."""
        handlers = self._hooks.get(event, [])
        for handler in handlers:
            try:
                result = handler(context)
                if result is not None:
                    # Handler can return modified context
                    context = result
            except Exception as e:
                logger.warning("[Hook] Handler error for %s: %s", event.value, e)
        return context

    def clear(self, event: HookEvent | None = None) -> None:
        """Clear hooks for an event, or all hooks if event is None."""
        if event:
            self._hooks.pop(event, None)
        else:
            self._hooks.clear()

    def get_handlers(self, event: HookEvent) -> list[HookHandler]:
        """Return handlers registered for an event."""
        return list(self._hooks.get(event, []))
