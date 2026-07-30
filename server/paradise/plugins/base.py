"""ParadisePlugin — base class and PluginContext for the plugin system."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from paradise.memory.workspace import Workspace
    from paradise.plugins.hooks import HookEvent, HookHandler
    from paradise.transports.base import ProviderTransport


class PluginContext:
    """Context provided to plugins during registration.

    Plugins use this to register tools, hooks, and access agent resources.
    """

    def __init__(
        self,
        agent_id: str,
        workspace: "Workspace",
        config: dict,
        tool_registry=None,
        hook_executor=None,
        transport: "ProviderTransport | None" = None,
    ):
        self.agent_id = agent_id
        self.workspace = workspace
        self.config = config
        self._tool_registry = tool_registry
        self._hook_executor = hook_executor
        self._transport = transport

    def register_tool(
        self,
        name: str,
        schema: dict,
        handler: Callable,
        toolset: str = "plugin",
        is_async: bool = False,
        description: str = "",
    ) -> None:
        """Register a tool with the tool registry."""
        if self._tool_registry:
            self._tool_registry.register(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                is_async=is_async,
                description=description,
            )

    def register_hook(self, event: "HookEvent", handler: "HookHandler") -> None:
        """Register a hook for a specific event."""
        if self._hook_executor:
            self._hook_executor.register(event, handler)

    def inject_message(self, content: str, role: str = "system") -> dict:
        """Create a message dict for injection."""
        return {"role": role, "content": content}

    def get_llm(self) -> "ProviderTransport | None":
        """Get the agent's LLM transport for plugin use."""
        return self._transport


class ParadisePlugin(ABC):
    """Abstract base class for Paradise plugins.

    Plugins must implement on_register() and optionally override other hooks.
    """

    # Plugin metadata — override in subclass
    name: str = "unnamed"
    version: str = "1.0.0"
    description: str = ""

    def on_register(self, ctx: PluginContext) -> None:
        """Called when plugin is loaded. Use ctx to register tools and hooks."""
        pass

    def on_unregister(self) -> None:
        """Called when plugin is unloaded."""
        pass

    def on_message(self, message: dict) -> dict | None:
        """Called for each message. Return modified message or None to skip."""
        return message

    def on_tool_call(self, name: str, args: dict) -> dict | None:
        """Called before each tool call. Return modified args or None to block."""
        return args

    def on_response(self, response: str) -> str | None:
        """Called after response is generated. Return modified response."""
        return response

    def on_heartbeat(self, decision: dict) -> dict | None:
        """Called after heartbeat decision. Return modified decision."""
        return decision

    def on_reflection(self, reflection: dict) -> None:
        """Called after a reflection is completed."""
        pass
