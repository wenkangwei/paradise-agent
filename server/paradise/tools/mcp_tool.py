"""MCP (Model Context Protocol) client integration.

Phase 3-B — exposes tools from external MCP servers as paradise tools.
Default OFF; enabled by setting ``ParadiseConfig.mcp.enabled = True``
and listing servers under ``mcp.servers``.

Architecture (borrowed from user's llm-rec-platform/agent_engine/tools/mcp_tool.py):

    ┌──────────────────────────┐       ┌──────────────────────────────┐
    │ paradise supervisor loop │       │ MCP background thread + loop │
    │  (async, main thread)    │       │   (daemon thread)            │
    │                          │       │                              │
    │  tool_react dispatch ────┼───┐   │  Each server: long-lived     │
    │  ('mcp.fs.read_file')    │   │   │  ClientSession on transport  │
    │                          │   │   │  (stdio / sse / http)        │
    │  wrapper handler (sync)  │   │   │                              │
    │         │                │   └──►│  run_coroutine_threadsafe    │
    │         │                │       │  (session.call_tool(...))    │
    │         │                │   ┌───│                              │
    │         ◄────────────────┼───┘   │  Returns JSON result         │
    │  return JSON result      │       │                              │
    └──────────────────────────┘       └──────────────────────────────┘

Why a dedicated background loop (instead of reusing the supervisor loop):
  * MCP servers are long-lived stdio subprocesses. Spawning them inside
    the supervisor loop couples their lifetime to the supervisor graph
    and blocks startup on slow servers.
  * tool_react dispatches tools via ``registry.dispatch_async`` — but
    our wrapper handlers are sync (the registry supports both). A sync
    handler that needs to call an async MCP session uses
    ``run_coroutine_threadsafe`` to hop loops without blocking either.
  * Cleaner shutdown: stop the background loop and all servers die,
    regardless of supervisor state.

Failure modes (all soft — never crash the supervisor):
  * MCP SDK not installed → module import warning, manager inert
  * Server init fails (binary missing, bad URL) → server marked
    unavailable, no tools registered from it
  * call_tool times out → JSON error returned, supervisor turn continues
  * call_tool raises → JSON error, session kept alive for next call
  * Background thread dies → manager marked closed, all tools 503
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any

from paradise.config import McpServerConfig
from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ── Optional MCP SDK import ──────────────────────────────────────────
#
# We import lazily inside __init__ so the module loads cleanly in dev
# environments where ``mcp`` isn't installed. Manager.__init__ will
# raise a friendly error in that case, and factory.build_supervisor
# catches it and continues without MCP tools.

_TRANSPORT_DISPATCH = {
    "stdio": "stdio_client",
    "sse": "sse_client",
    "http": "streamablehttp_client",
}


class McpClientManager:
    """Manage multiple MCP server connections and expose their tools.

    Lifecycle:
        manager = McpClientManager(servers)
        manager.start()                        # block ~connect_timeout per server
        manager.register_tools_into(registry)  # add wrappers as paradise tools
        # ... supervisor runs ...
        await manager.aclose()                 # graceful shutdown

    Thread-safety:
        All mutations happen on the background loop or under _lock.
        ``call_tool`` may be called concurrently from multiple threads
        of the supervisor loop — run_coroutine_threadsafe is thread-safe.
    """

    def __init__(self, servers: list[McpServerConfig]):
        if not servers:
            raise ValueError("McpClientManager requires at least one server")
        self._servers: list[McpServerConfig] = list(servers)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

        # server_name → (transport_ctx, ClientSession)
        # transport_ctx is kept so aclose can __aexit__ it cleanly.
        self._contexts: dict[str, tuple[Any, Any]] = {}

        # server_name → ClientSession (mirror of _contexts values for fast lookup)
        self._sessions: dict[str, Any] = {}

        # server_name → list of (paradise_tool_name, original_tool_name)
        # populated by register_tools_into
        self._registered: dict[str, list[tuple[str, str]]] = {}

        # Init errors keyed by server_name for diagnostics
        self._unavailable: dict[str, str] = {}

        # Lifecycle flag — set once start() succeeds; cleared by aclose()
        self._started = threading.Event()
        self._closed = False

    # ── Public API ──────────────────────────────────────────────────

    def start(self) -> None:
        """Start background thread + initialize all server sessions.

        Blocks the caller until either (a) all servers have initialized,
        or (b) the total connect timeout has elapsed. Servers that fail
        to init are marked unavailable and skipped — start() never raises.

        Idempotent: calling start() twice is a no-op after the first success.
        """
        if self._started.is_set():
            return

        # Spin up background thread with its own event loop.
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        loop_error: list[BaseException | None] = [None]

        def _thread_main() -> None:
            asyncio.set_event_loop(self._loop)  # type: ignore[arg-type]
            try:
                self._loop.run_forever()  # type: ignore[union-attr]
            except BaseException as exc:
                # Should not happen unless the loop is cancelled
                loop_error[0] = exc
            finally:
                try:
                    self._loop.stop()  # type: ignore[union-attr]
                    self._loop.close()  # type: ignore[union-attr]
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=_thread_main,
            name="paradise-mcp-loop",
            daemon=True,
        )
        self._thread.start()

        # Schedule initialization on the background loop and block.
        total_timeout = sum(min(s.connect_timeout_seconds, 60) for s in self._servers) + 5
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_init_all(), self._loop
            )
            future.result(timeout=total_timeout)
        except concurrent.futures.TimeoutExpired:
            logger.warning(
                "MCP start(): init took longer than %ss — proceeding with "
                "whatever servers came up; unreachable ones will be unavailable",
                total_timeout,
            )
        except Exception as exc:
            logger.exception("MCP start(): init task crashed: %s", exc)

        self._started.set()
        up = len(self._sessions)
        down = len(self._unavailable)
        logger.info(
            "MCP manager started: %d/%d servers available (%d unavailable)",
            up, len(self._servers), down,
        )

    def register_tools_into(self, registry_obj: Any) -> int:
        """Register wrappers for each MCP tool into the paradise registry.

        Returns the count of tools successfully registered. Servers that
        failed init are skipped (their tools are not registered).

        Each MCP tool becomes a paradise tool named:
            ``mcp.{server_name}.{original_tool_name}``

        Wrapper handlers are SYNC (is_async=False) — they use
        run_coroutine_threadsafe to hop to the MCP background loop.
        """
        if not self._started.is_set():
            raise RuntimeError("McpClientManager.start() must be called first")

        total = 0
        for srv in self._servers:
            session = self._sessions.get(srv.name)
            if session is None:
                logger.info(
                    "MCP server %r unavailable — skipping tool registration",
                    srv.name,
                )
                continue

            # Pull tool list (runs on background loop, blocks caller)
            try:
                tools_result = self._run_on_loop(
                    session.list_tools(),
                    timeout=srv.connect_timeout_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "MCP server %r list_tools failed: %s — skipping",
                    srv.name, exc,
                )
                self._unavailable[srv.name] = f"list_tools: {exc}"
                continue

            registered_for_srv: list[tuple[str, str]] = []
            for tool in tools_result.tools:
                paradise_name = f"mcp.{srv.name}.{tool.name}"
                handler = self._make_wrapper_handler(srv.name, tool.name)
                schema = {
                    "name": paradise_name,
                    "description": (
                        tool.description
                        or f"MCP tool {tool.name!r} from server {srv.name!r}"
                    ),
                    "parameters": tool.inputSchema
                    or {"type": "object", "properties": {}},
                }
                try:
                    registry_obj.register(
                        name=paradise_name,
                        toolset=f"mcp_{srv.name}",
                        schema=schema,
                        handler=handler,
                        is_async=False,  # sync wrapper; hops loops internally
                        description=schema["description"],
                        privilege="read",  # MCP server enforces its own write policy
                    )
                    registered_for_srv.append((paradise_name, tool.name))
                    total += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to register MCP tool %r: %s", paradise_name, exc
                    )

            with self._lock:
                self._registered[srv.name] = registered_for_srv

            logger.info(
                "MCP server %r: registered %d tools (%s)",
                srv.name, len(registered_for_srv),
                ", ".join(t[0] for t in registered_for_srv) or "none",
            )

        return total

    def call_tool(self, server: str, tool: str, args: dict[str, Any]) -> str:
        """Invoke a tool on an MCP server. Returns JSON string (paradise convention).

        Never raises — failures become JSON error strings. The wrapper
        handlers registered into paradise delegate here.
        """
        if self._closed or self._loop is None:
            return tool_error("MCP manager has been closed")
        with self._lock:
            session = self._sessions.get(server)
        if session is None:
            reason = self._unavailable.get(server, "not initialised")
            return tool_error(f"MCP server {server!r} unavailable ({reason})")

        # Find per-server timeout
        timeout = next(
            (s.timeout_seconds for s in self._servers if s.name == server), 60
        )

        async def _call() -> str:
            try:
                result = await session.call_tool(tool, args)
            except Exception as exc:
                return tool_error(
                    f"MCP call {server}.{tool} failed: {type(exc).__name__}: {exc}"
                )
            if getattr(result, "isError", False):
                return tool_error(
                    f"MCP tool {server}.{tool} returned error: "
                    f"{_extract_text(result)}"
                )
            return tool_result({"output": _extract_text(result)})

        try:
            future = asyncio.run_coroutine_threadsafe(_call(), self._loop)
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutExpired:
            return tool_error(
                f"MCP call {server}.{tool} timed out after {timeout}s"
            )
        except Exception as exc:
            return tool_error(
                f"MCP dispatch error for {server}.{tool}: "
                f"{type(exc).__name__}: {exc}"
            )

    async def aclose(self) -> None:
        """Shut down all sessions and stop the background loop.

        Safe to call multiple times. Best-effort: exceptions during
        shutdown are logged but never re-raised.
        """
        if self._closed:
            return
        self._closed = True

        if self._loop is None or not self._loop.is_running():
            return

        # Close all sessions on the background loop
        async def _shutdown_all() -> None:
            with self._lock:
                items = list(self._contexts.items())
            for name, (ctx, session) in items:
                try:
                    await session.__aexit__(None, None, None)
                except Exception:
                    logger.debug("MCP session %s __aexit__ error", name, exc_info=True)
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    logger.debug("MCP transport %s __aexit__ error", name, exc_info=True)

        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown_all(), self._loop)
            future.result(timeout=10)
        except Exception as exc:
            logger.debug("MCP shutdown task error: %s", exc)

        # Stop the loop & join thread
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            self._contexts.clear()
            self._sessions.clear()
        logger.info("MCP manager closed")

    # ── Read-only views for tests/diagnostics ───────────────────────

    @property
    def available_servers(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions.keys())

    @property
    def unavailable_servers(self) -> dict[str, str]:
        with self._lock:
            return dict(self._unavailable)

    @property
    def registered_tool_names(self) -> list[str]:
        with self._lock:
            return sorted(
                name for entries in self._registered.values() for name, _ in entries
            )

    # ── Internals ───────────────────────────────────────────────────

    def _run_on_loop(self, coro, *, timeout: float):
        """Schedule *coro* on the background loop and block until done."""
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("MCP background loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _make_wrapper_handler(self, server_name: str, tool_name: str):
        """Build a sync handler that delegates to call_tool."""
        def _handler(args: dict[str, Any]) -> str:
            return self.call_tool(server_name, tool_name, args or {})
        _handler.__name__ = f"mcp_wrapper_{server_name}_{tool_name}"
        return _handler

    async def _async_init_all(self) -> None:
        """Parallel-init all servers. Failures per-server, not global."""
        tasks = [self._init_one(s) for s in self._servers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for srv, res in zip(self._servers, results):
            if isinstance(res, Exception):
                logger.warning(
                    "MCP server %r init failed: %s: %s",
                    srv.name, type(res).__name__, res,
                )
                with self._lock:
                    self._unavailable[srv.name] = f"{type(res).__name__}: {res}"
            elif isinstance(res, str):
                # _init_one returned an error string (soft failure)
                with self._lock:
                    self._unavailable[srv.name] = res
            # else: success — _init_one already populated self._sessions

    async def _init_one(self, srv: McpServerConfig):
        """Initialize one server session. Raises or returns error string."""
        # Resolve transport
        try:
            transport_cm = self._build_transport(srv)
        except Exception as exc:
            logger.warning(
                "MCP server %r: transport setup failed: %s", srv.name, exc
            )
            raise

        # Enter the transport context manager (yields (read, write, *extra))
        try:
            transport_tuple = await self._enter_transport(transport_cm, srv)
        except Exception as exc:
            logger.warning(
                "MCP server %r: transport connect failed: %s", srv.name, exc
            )
            raise

        read_stream, write_stream = transport_tuple[0], transport_tuple[1]

        # Create + initialize ClientSession
        try:
            from mcp import ClientSession  # type: ignore
        except ImportError as exc:
            await self._exit_transport(transport_cm)
            raise RuntimeError(f"mcp SDK not installed: {exc}") from exc

        session = ClientSession(read_stream, write_stream)
        try:
            await session.__aenter__()
            await asyncio.wait_for(
                session.initialize(),
                timeout=srv.connect_timeout_seconds,
            )
        except Exception as exc:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass
            await self._exit_transport(transport_cm)
            logger.warning(
                "MCP server %r: initialize failed: %s", srv.name, exc
            )
            raise

        with self._lock:
            self._contexts[srv.name] = (transport_cm, session)
            self._sessions[srv.name] = session
        logger.info(
            "MCP server %r connected (transport=%s)",
            srv.name, srv.transport,
        )
        return session

    def _build_transport(self, srv: McpServerConfig):
        """Construct the transport async context manager for *srv*."""
        if srv.transport == "stdio":
            from mcp import stdio_client, StdioServerParameters  # type: ignore
            if not srv.command:
                raise ValueError(
                    f"MCP server {srv.name!r}: stdio transport requires 'command'"
                )
            params = StdioServerParameters(
                command=srv.command,
                args=srv.args,
                env=srv.env or None,
            )
            return stdio_client(params)
        elif srv.transport == "sse":
            from mcp.client.sse import sse_client  # type: ignore
            if not srv.url:
                raise ValueError(
                    f"MCP server {srv.name!r}: sse transport requires 'url'"
                )
            return sse_client(srv.url)
        elif srv.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client  # type: ignore
            if not srv.url:
                raise ValueError(
                    f"MCP server {srv.name!r}: http transport requires 'url'"
                )
            return streamablehttp_client(srv.url)
        else:
            raise ValueError(
                f"MCP server {srv.name!r}: unknown transport {srv.transport!r} "
                f"(expected: stdio | sse | http)"
            )

    async def _enter_transport(self, transport_cm, srv: McpServerConfig):
        """Enter the transport context with connect_timeout."""
        return await asyncio.wait_for(
            transport_cm.__aenter__(),
            timeout=srv.connect_timeout_seconds,
        )

    async def _exit_transport(self, transport_cm) -> None:
        """Best-effort exit of a partially-entered transport."""
        try:
            await transport_cm.__aexit__(None, None, None)
        except Exception:
            pass


# ── Helpers ──────────────────────────────────────────────────────────


def _extract_text(result: Any) -> str:
    """Pull all TextContent.text blocks from a CallToolResult.

    MCP tools may return multiple content blocks (text/image/audio).
    For paradise we only forward text — image/audio blocks would need
    a different transport path.
    """
    parts: list[str] = []
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts) if parts else "(no textual output)"


__all__ = ["McpClientManager"]
