"""Hindsight memory provider — Paradise adaptation of hermes-agent's hindsight plugin.

Wraps the Hindsight long-term memory backend (knowledge graph, entity resolution,
multi-strategy retrieval) behind the Paradise MemoryProvider ABC and exposes three
tools through the ToolRegistry: hindsight_retain, hindsight_recall, hindsight_reflect.

Simplified from the full hermes version:
  - No embedded daemon management (local_embedded mode not supported)
  - No document_id versioning complexity
  - No bank_id_template resolution
  - No session-switch flush logic

Supports cloud and local_external modes, configured via the same environment
variables as hermes:

  HINDSIGHT_API_KEY                — API key for Hindsight Cloud
  HINDSIGHT_BANK_ID                — memory bank identifier (default: paradise)
  HINDSIGHT_BUDGET                 — recall budget: low/mid/high (default: mid)
  HINDSIGHT_API_URL                — API endpoint
  HINDSIGHT_MODE                   — cloud or local (default: cloud)
  HINDSIGHT_TIMEOUT                — API request timeout in seconds (default: 120)
  HINDSIGHT_RETAIN_TAGS            — comma-separated default tags
  HINDSIGHT_RETAIN_SOURCE          — metadata source value

Graceful degradation: if the hindsight-client package is not importable,
is_available() returns False and all operations are no-ops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paradise.memory.base import MemoryProvider
from paradise.exceptions import MemoryProviderError
from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_API_URL = "https://api.hindsight.vectorize.io"
_DEFAULT_LOCAL_URL = "http://localhost:8888"
_DEFAULT_TIMEOUT = 120
_VALID_BUDGETS = {"low", "mid", "high"}


# ---------------------------------------------------------------------------
# Tool schemas — identical to hermes for cross-project compatibility
# ---------------------------------------------------------------------------

RETAIN_SCHEMA = {
    "name": "hindsight_retain",
    "description": (
        "Store information to long-term memory. Hindsight automatically "
        "extracts structured facts, resolves entities, and indexes for retrieval."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The information to store.",
            },
            "context": {
                "type": "string",
                "description": (
                    "Short label (e.g. 'user preference', 'project decision')."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional per-call tags to merge with configured default retain tags."
                ),
            },
        },
        "required": ["content"],
    },
}

RECALL_SCHEMA = {
    "name": "hindsight_recall",
    "description": (
        "Search long-term memory. Returns memories ranked by relevance using "
        "semantic search, keyword matching, entity graph traversal, and reranking."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for.",
            },
        },
        "required": ["query"],
    },
}

REFLECT_SCHEMA = {
    "name": "hindsight_reflect",
    "description": (
        "Synthesize a reasoned answer from long-term memories. Unlike recall, "
        "this reasons across all stored memories to produce a coherent response."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The question to reflect on.",
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_int_setting(value: Any, default: int) -> int:
    """Parse an integer config/env value, falling back on invalid input."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid integer Hindsight setting %r; using default %s",
            value, default,
        )
        return default


def _normalize_tags(value: Any) -> list[str]:
    """Normalize tag config to a deduplicated list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            raw_items = parsed if isinstance(parsed, list) else text.split(",")
        else:
            raw_items = text.split(",")
    else:
        raw_items = [value]

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        tag = str(item).strip()
        if tag and tag not in seen:
            seen.add(tag)
            normalized.append(tag)
    return normalized


def _utc_timestamp() -> str:
    """Return current UTC timestamp in ISO-8601 with milliseconds and Z suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _load_config() -> dict:
    """Load config from file or environment variables.

    Resolution order:
      1. ~/.hindsight/config.json  (shared config file)
      2. Environment variables
    """
    config_path = Path.home() / ".hindsight" / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "mode": os.environ.get("HINDSIGHT_MODE", "cloud"),
        "apiKey": os.environ.get("HINDSIGHT_API_KEY", ""),
        "timeout": _parse_int_setting(
            os.environ.get("HINDSIGHT_TIMEOUT"), _DEFAULT_TIMEOUT
        ),
        "retain_tags": os.environ.get("HINDSIGHT_RETAIN_TAGS", ""),
        "retain_source": os.environ.get("HINDSIGHT_RETAIN_SOURCE", ""),
        "banks": {
            "paradise": {
                "bankId": os.environ.get("HINDSIGHT_BANK_ID", "paradise"),
                "budget": os.environ.get("HINDSIGHT_BUDGET", "mid"),
                "enabled": True,
            }
        },
    }


def _can_import_hindsight() -> bool:
    """Return True if the hindsight-client package is importable."""
    try:
        import hindsight_client  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Shared async event loop (one per process, reused)
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return a long-lived event loop running on a background thread."""
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _loop_thread = threading.Thread(
            target=_run, daemon=True, name="hindsight-loop"
        )
        _loop_thread.start()
        return _loop


def _run_sync(coro, timeout: float = _DEFAULT_TIMEOUT):
    """Schedule *coro* on the shared loop and block until done."""
    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class HindsightMemoryProvider(MemoryProvider):
    """Hindsight long-term memory provider for Paradise.

    Wraps the hindsight_client library behind the MemoryProvider ABC and
    registers three tools (retain, recall, reflect) with the ToolRegistry.

    If the hindsight-client package is not installed, the provider degrades
    gracefully: is_available() returns False and all operations are no-ops.
    """

    def __init__(self) -> None:
        self._config: dict | None = None
        self._api_key: str = ""
        self._api_url: str = _DEFAULT_API_URL
        self._bank_id: str = "paradise"
        self._budget: str = "mid"
        self._mode: str = "cloud"
        self._timeout: int = _DEFAULT_TIMEOUT
        self._retain_tags: list[str] = []
        self._retain_source: str = ""
        self._client: Any = None
        self._agent_id: str = ""
        self._workspace: Any = None
        self._turn_index: int = 0
        self._available: bool | None = None  # lazy-checked
        self._registered_tools: bool = False

    # -- MemoryProvider ABC --------------------------------------------------

    @property
    def name(self) -> str:
        return "hindsight"

    def is_available(self) -> bool:
        """Return True if hindsight-client is importable and config is present."""
        if self._available is not None:
            return self._available

        if not _can_import_hindsight():
            self._available = False
            return False

        try:
            cfg = _load_config()
            mode = cfg.get("mode", "cloud")
            if mode in ("local", "local_external"):
                self._available = True
                return True
            # Cloud mode: need API key or URL
            has_key = bool(
                cfg.get("apiKey")
                or cfg.get("api_key")
                or os.environ.get("HINDSIGHT_API_KEY", "")
            )
            has_url = bool(
                cfg.get("api_url")
                or os.environ.get("HINDSIGHT_API_URL", "")
            )
            self._available = has_key or has_url
        except Exception:
            self._available = False

        return self._available

    def initialize(self, agent_id: str, workspace: Any, **kwargs) -> None:
        """Initialize the provider for an agent session."""
        if not self.is_available():
            logger.info(
                "HindsightMemoryProvider: skipping init (not available)"
            )
            return

        self._agent_id = str(agent_id or "").strip()
        self._workspace = workspace
        self._config = _load_config()
        self._turn_index = 0

        # Mode — local_external or cloud (no local_embedded in Paradise)
        mode = self._config.get("mode", "cloud")
        if mode in ("local", "local_external"):
            self._mode = "local_external"
        else:
            self._mode = "cloud"

        # API settings
        self._api_key = (
            self._config.get("apiKey")
            or self._config.get("api_key")
            or os.environ.get("HINDSIGHT_API_KEY", "")
        )
        default_url = (
            _DEFAULT_LOCAL_URL
            if self._mode == "local_external"
            else _DEFAULT_API_URL
        )
        self._api_url = (
            self._config.get("api_url")
            or os.environ.get("HINDSIGHT_API_URL", default_url)
        )
        self._timeout = _parse_int_setting(
            self._config.get("timeout")
            if self._config.get("timeout") is not None
            else os.environ.get("HINDSIGHT_TIMEOUT"),
            _DEFAULT_TIMEOUT,
        )

        # Bank settings
        banks = self._config.get("banks", {})
        bank_cfg = banks.get("paradise", banks.get("hermes", {}))
        self._bank_id = (
            self._config.get("bank_id")
            or bank_cfg.get("bankId", "paradise")
        )
        budget = (
            self._config.get("recall_budget")
            or self._config.get("budget")
            or bank_cfg.get("budget", "mid")
        )
        self._budget = budget if budget in _VALID_BUDGETS else "mid"

        # Retain settings
        self._retain_tags = _normalize_tags(
            self._config.get("retain_tags")
            or os.environ.get("HINDSIGHT_RETAIN_TAGS", "")
        )
        self._retain_source = str(
            self._config.get("retain_source")
            or os.environ.get("HINDSIGHT_RETAIN_SOURCE", "")
        ).strip()

        # Reset client so next call gets a fresh one with current config
        self._client = None

        logger.info(
            "HindsightMemoryProvider initialized: mode=%s, api_url=%s, "
            "bank=%s, budget=%s, agent=%s",
            self._mode, self._api_url, self._bank_id, self._budget,
            self._agent_id,
        )

        # Register tools with the global registry (once)
        self._register_tools()

    def system_prompt_block(self) -> str:
        """Return context about Hindsight for the system prompt."""
        if not self.is_available():
            return ""
        return (
            "# Hindsight Memory\n"
            f"Active. Bank: {self._bank_id}, budget: {self._budget}.\n"
            "Use hindsight_recall to search memories, hindsight_reflect for "
            "synthesis, hindsight_retain to store facts."
        )

    def prefetch(self, query: str) -> str:
        """Recall relevant context before a turn. Returns formatted text."""
        if not self.is_available():
            return ""
        try:
            resp = self._run_hindsight_operation(
                lambda client: client.arecall(
                    bank_id=self._bank_id,
                    query=query[:800],
                    budget=self._budget,
                    max_tokens=4096,
                )
            )
            if not resp.results:
                return ""
            text = "\n".join(
                f"- {r.text}" for r in resp.results if r.text
            )
            if not text:
                return ""
            return (
                "# Hindsight Memory (persistent cross-session context)\n\n"
                + text
            )
        except Exception as e:
            logger.debug("Hindsight prefetch failed: %s", e)
            return ""

    def sync_turn(self, user_content: str, assistant_content: str) -> None:
        """Persist a completed turn to Hindsight. Non-blocking via background thread."""
        if not self.is_available():
            return

        self._turn_index += 1
        now = datetime.now(timezone.utc).isoformat()
        messages = [
            {"role": "user", "content": f"User: {user_content}", "timestamp": now},
            {
                "role": "assistant",
                "content": f"Assistant: {assistant_content}",
                "timestamp": now,
            },
        ]
        content = json.dumps(messages, ensure_ascii=False)
        metadata: dict[str, str] = {
            "retained_at": _utc_timestamp(),
            "message_count": "2",
            "turn_index": str(self._turn_index),
        }
        if self._retain_source:
            metadata["source"] = self._retain_source
        if self._agent_id:
            metadata["agent_id"] = self._agent_id

        merged_tags = list(self._retain_tags)
        if self._agent_id:
            merged_tags.append(f"agent:{self._agent_id}")

        def _do_retain() -> None:
            try:
                self._run_hindsight_operation(
                    lambda client: client.aretain(
                        bank_id=self._bank_id,
                        content=content,
                        context="conversation between Paradise Agent and the User",
                        metadata=metadata,
                        tags=merged_tags or None,
                    )
                )
                logger.debug("Hindsight sync_turn retained successfully")
            except Exception as e:
                logger.warning("Hindsight sync_turn failed: %s", e)

        t = threading.Thread(target=_do_retain, daemon=True, name="hindsight-sync")
        t.start()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas for the three Hindsight tools."""
        if not self.is_available():
            return []
        return [RETAIN_SCHEMA, RECALL_SCHEMA, REFLECT_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> str:
        """Dispatch a tool call to the appropriate Hindsight operation."""
        if not self.is_available():
            return tool_error("Hindsight memory provider is not available")

        if tool_name == "hindsight_retain":
            return self._handle_retain(args)
        elif tool_name == "hindsight_recall":
            return self._handle_recall(args)
        elif tool_name == "hindsight_reflect":
            return self._handle_reflect(args)

        return tool_error(f"Unknown Hindsight tool: {tool_name}")

    def shutdown(self) -> None:
        """Clean up the Hindsight client."""
        if self._client is not None:
            try:
                _run_sync(self._client.aclose(), timeout=10.0)
            except Exception:
                pass
            self._client = None
        # Deregister tools from the global registry
        self._deregister_tools()
        logger.debug("HindsightMemoryProvider shut down")

    def on_turn_start(self, turn_number: int, message: str) -> None:
        """Called at start of each turn. No-op for Hindsight."""

    def on_session_end(self) -> None:
        """Called when session ends. Flush and shut down."""
        self.shutdown()

    # -- Internal helpers ----------------------------------------------------

    def _get_client(self) -> Any:
        """Return the cached Hindsight client (created once, reused)."""
        if self._client is None:
            try:
                from hindsight_client import Hindsight
            except ImportError as exc:
                raise MemoryProviderError(
                    "hindsight-client package is not installed",
                    provider_name="hindsight",
                ) from exc

            timeout = self._timeout or _DEFAULT_TIMEOUT
            kwargs: dict[str, Any] = {
                "base_url": self._api_url,
                "timeout": float(timeout),
            }
            if self._api_key:
                kwargs["api_key"] = self._api_key
            logger.debug(
                "Creating Hindsight client (url=%s, has_key=%s, timeout=%s)",
                self._api_url, bool(self._api_key), kwargs["timeout"],
            )
            self._client = Hindsight(**kwargs)
        return self._client

    def _run_hindsight_operation(self, operation):
        """Run an async Hindsight client operation synchronously."""
        client = self._get_client()
        return _run_sync(operation(client), timeout=self._timeout)

    def _handle_retain(self, args: dict[str, Any]) -> str:
        """Handle hindsight_retain tool call."""
        content = args.get("content", "")
        if not content:
            return tool_error("Missing required parameter: content")
        context = args.get("context")
        try:
            metadata: dict[str, str] = {
                "retained_at": _utc_timestamp(),
                "message_count": "1",
                "turn_index": str(self._turn_index),
            }
            if self._retain_source:
                metadata["source"] = self._retain_source
            if self._agent_id:
                metadata["agent_id"] = self._agent_id

            merged_tags = _normalize_tags(self._retain_tags)
            for tag in _normalize_tags(args.get("tags")):
                if tag not in merged_tags:
                    merged_tags.append(tag)

            retain_kwargs: dict[str, Any] = {
                "bank_id": self._bank_id,
                "content": content,
                "metadata": metadata,
            }
            if context is not None:
                retain_kwargs["context"] = context
            if merged_tags:
                retain_kwargs["tags"] = merged_tags

            self._run_hindsight_operation(
                lambda client: client.aretain(**retain_kwargs)
            )
            return tool_result({"result": "Memory stored successfully."})
        except Exception as e:
            logger.warning("hindsight_retain failed: %s", e, exc_info=True)
            return tool_error(f"Failed to store memory: {e}")

    def _handle_recall(self, args: dict[str, Any]) -> str:
        """Handle hindsight_recall tool call."""
        query = args.get("query", "")
        if not query:
            return tool_error("Missing required parameter: query")
        try:
            recall_kwargs: dict[str, Any] = {
                "bank_id": self._bank_id,
                "query": query,
                "budget": self._budget,
                "max_tokens": 4096,
            }
            resp = self._run_hindsight_operation(
                lambda client: client.arecall(**recall_kwargs)
            )
            if not resp.results:
                return tool_result({"result": "No relevant memories found."})
            lines = [
                f"{i}. {r.text}"
                for i, r in enumerate(resp.results, 1)
            ]
            return tool_result({"result": "\n".join(lines)})
        except Exception as e:
            logger.warning("hindsight_recall failed: %s", e, exc_info=True)
            return tool_error(f"Failed to search memory: {e}")

    def _handle_reflect(self, args: dict[str, Any]) -> str:
        """Handle hindsight_reflect tool call."""
        query = args.get("query", "")
        if not query:
            return tool_error("Missing required parameter: query")
        try:
            resp = self._run_hindsight_operation(
                lambda client: client.areflect(
                    bank_id=self._bank_id, query=query, budget=self._budget
                )
            )
            return tool_result(
                {"result": resp.text or "No relevant memories found."}
            )
        except Exception as e:
            logger.warning("hindsight_reflect failed: %s", e, exc_info=True)
            return tool_error(f"Failed to reflect: {e}")

    # -- ToolRegistry integration -------------------------------------------

    def _register_tools(self) -> None:
        """Register the three Hindsight tools with the global ToolRegistry."""
        if self._registered_tools:
            return

        def _check() -> bool:
            return self.is_available()

        registry.register(
            name="hindsight_retain",
            toolset="hindsight",
            schema=RETAIN_SCHEMA,
            handler=self._tool_dispatch_retain,
            check_fn=_check,
            requires_env=["HINDSIGHT_API_KEY"],
            description="Store information to Hindsight long-term memory",
            emoji="brain",
        )
        registry.register(
            name="hindsight_recall",
            toolset="hindsight",
            schema=RECALL_SCHEMA,
            handler=self._tool_dispatch_recall,
            check_fn=_check,
            requires_env=["HINDSIGHT_API_KEY"],
            description="Search Hindsight long-term memory",
            emoji="brain",
        )
        registry.register(
            name="hindsight_reflect",
            toolset="hindsight",
            schema=REFLECT_SCHEMA,
            handler=self._tool_dispatch_reflect,
            check_fn=_check,
            requires_env=["HINDSIGHT_API_KEY"],
            description="Synthesize an answer from Hindsight long-term memories",
            emoji="brain",
        )
        self._registered_tools = True
        logger.debug("Hindsight tools registered with ToolRegistry")

    def _deregister_tools(self) -> None:
        """Remove the Hindsight tools from the global ToolRegistry."""
        if not self._registered_tools:
            return
        for tool_name in (
            "hindsight_retain",
            "hindsight_recall",
            "hindsight_reflect",
        ):
            registry.deregister(tool_name)
        self._registered_tools = False

    # Tool-registry handlers — thin wrappers that delegate to handle_tool_call

    def _tool_dispatch_retain(self, args: dict, **kwargs) -> str:
        return self.handle_tool_call("hindsight_retain", args)

    def _tool_dispatch_recall(self, args: dict, **kwargs) -> str:
        return self.handle_tool_call("hindsight_recall", args)

    def _tool_dispatch_reflect(self, args: dict, **kwargs) -> str:
        return self.handle_tool_call("hindsight_reflect", args)
