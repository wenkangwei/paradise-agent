"""MemoryProvider — abstract base for pluggable memory backends.

Simplified from hermes-agent MemoryProvider, keeping core lifecycle only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'builtin', 'hindsight')."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if configured and ready."""

    @abstractmethod
    def initialize(self, agent_id: str, workspace: Any, **kwargs) -> None:
        """Initialize for an agent session."""

    def system_prompt_block(self) -> str:
        """Return text to include in system prompt. Empty string to skip."""
        return ""

    def prefetch(self, query: str) -> str:
        """Recall relevant context. Return formatted text or empty string."""
        return ""

    def sync_turn(self, user_content: str, assistant_content: str) -> None:
        """Persist a completed turn. Should be non-blocking."""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas in OpenAI function calling format."""

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        """Handle a tool call. Return JSON string result."""
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        """Clean shutdown."""

    def on_turn_start(self, turn_number: int, message: str) -> None:
        """Called at start of each turn."""

    def on_session_end(self) -> None:
        """Called when session ends."""
