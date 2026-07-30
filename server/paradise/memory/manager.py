"""MemoryManager — orchestrates memory providers.

Enforces one-external-provider limit. Built-in file memory always active.
"""

from __future__ import annotations

import logging
from typing import Any

from paradise.memory.base import MemoryProvider
from paradise.memory.builtin import FrozenMemory

logger = logging.getLogger(__name__)


class MemoryManager:
    """Orchestrates built-in + optional external memory provider."""

    def __init__(self):
        self._builtin: FrozenMemory | None = None
        self._external: MemoryProvider | None = None
        self._agent_id: str = ""

    def initialize(self, agent_id: str, workspace: Any, external_provider: MemoryProvider | None = None) -> None:
        """Initialize memory system for an agent."""
        self._agent_id = agent_id
        self._builtin = FrozenMemory()
        self._builtin.initialize(agent_id, workspace)

        if external_provider and external_provider.is_available():
            try:
                external_provider.initialize(agent_id, workspace)
                self._external = external_provider
                logger.info("[Memory] External provider '%s' activated for agent %s",
                            external_provider.name, agent_id)
            except Exception as e:
                logger.error("[Memory] Failed to initialize external provider '%s': %s",
                             external_provider.name, e)
                self._external = None

    @property
    def active_provider(self) -> MemoryProvider | None:
        """Return the active external provider, if any."""
        return self._external

    @property
    def builtin(self) -> FrozenMemory | None:
        """Return the built-in frozen memory."""
        return self._builtin

    def system_prompt_block(self) -> str:
        """Collect system prompt blocks from all providers."""
        parts = []
        if self._builtin:
            block = self._builtin.system_prompt_block()
            if block:
                parts.append(block)
        if self._external:
            block = self._external.system_prompt_block()
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    def prefetch(self, query: str) -> str:
        """Prefetch from active external provider."""
        if self._external:
            return self._external.prefetch(query)
        return ""

    def sync_turn(self, user_content: str, assistant_content: str) -> None:
        """Sync turn to all providers."""
        if self._builtin:
            self._builtin.sync_turn(user_content, assistant_content)
        if self._external:
            self._external.sync_turn(user_content, assistant_content)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Collect tool schemas from external provider."""
        if self._external:
            return self._external.get_tool_schemas()
        return []

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        """Route tool call to the appropriate provider."""
        if self._external:
            return self._external.handle_tool_call(tool_name, args)
        return '{"error": "No external memory provider active"}'

    def shutdown(self) -> None:
        """Shutdown all providers."""
        if self._builtin:
            self._builtin.shutdown()
        if self._external:
            self._external.shutdown()
