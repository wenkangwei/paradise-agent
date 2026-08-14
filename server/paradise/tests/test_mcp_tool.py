"""Phase 3-B — McpClientManager tests.

Run:
    cd server && python -m pytest paradise/tests/test_mcp_tool.py -v

Scope:
  * Construction + start lifecycle (with mocked ClientSession)
  * register_tools_into paradise ToolRegistry (naming, schema, privilege)
  * call_tool wrapper (success / server error / timeout / unavailable)
  * aclose idempotency + background thread shutdown
  * Config parsing (McpServerConfig / McpConfig)
  * Transport dispatch (stdio / sse / http) — mocked at SDK boundary

Mock strategy:
  All MCP SDK calls (ClientSession, stdio_client, sse_client,
  streamablehttp_client) are replaced with AsyncMock / MagicMock at
  the module level. No real subprocess or network connection is made.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paradise.config import McpServerConfig, McpConfig, ParadiseConfig
from paradise.tools.registry import registry as global_registry, ToolRegistry


# ─────────────────────────────────────────────────────────────────────
# Test fixtures: fake MCP SDK objects
# ─────────────────────────────────────────────────────────────────────


class _FakeTextContent:
    """Stand-in for mcp.types.TextContent."""
    def __init__(self, text: str):
        self.text = text


class _FakeCallToolResult:
    """Stand-in for mcp.types.CallToolResult."""
    def __init__(self, content: list[Any] | None = None, is_error: bool = False):
        self.content = content or []
        self.isError = is_error


class _FakeTool:
    """Stand-in for a tool descriptor returned by list_tools()."""
    def __init__(self, name: str, description: str = "", input_schema: dict | None = None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema


class _FakeToolsResult:
    """Stand-in for the result of session.list_tools()."""
    def __init__(self, tools: list[_FakeTool]):
        self.tools = tools


class _FakeClientSession:
    """Minimal ClientSession mock.

    Implements just the API surface that McpClientManager uses:
      - async __aenter__ / __aexit__ (context manager)
      - async initialize()
      - async list_tools() → _FakeToolsResult
      - async call_tool(name, args) → _FakeCallToolResult
    """
    def __init__(
        self,
        tools: list[_FakeTool] | None = None,
        call_results: dict[str, _FakeCallToolResult] | None = None,
        init_raises: Exception | None = None,
    ):
        self._tools = tools or []
        self._call_results = call_results or {}
        self._init_raises = init_raises
        self.initialized = False
        self.entered = False
        self.exited = False
        self.call_log: list[tuple[str, dict]] = []

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *args):
        self.exited = True

    async def initialize(self):
        if self._init_raises:
            raise self._init_raises
        self.initialized = True

    async def list_tools(self):
        return _FakeToolsResult(self._tools)

    async def call_tool(self, name: str, args: dict):
        self.call_log.append((name, args))
        if name not in self._call_results:
            return _FakeCallToolResult(
                content=[_FakeTextContent(f"(no result for {name})")]
            )
        return self._call_results[name]


def _build_fake_transport_cm():
    """Build a fake async context manager that yields (read, write, *meta).

    Real stdio_client / sse_client / streamablehttp_client all yield at
    least (read_stream, write_stream). Some yield a third meta element.
    """
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


# ─────────────────────────────────────────────────────────────────────
# Patch helpers
# ─────────────────────────────────────────────────────────────────────


def _patch_mcp_sdk(monkeypatch, sessions: dict[str, _FakeClientSession]):
    """Patch the MCP SDK imports used by mcp_tool.py.

    `sessions` maps server_name → _FakeClientSession. The transport
    factories return dummy context managers; ClientSession is replaced
    with a factory that returns the pre-built fake session for each server.
    """
    # Patch ClientSession to return the pre-built fake per call.
    # We can't easily route by server_name here, so we return a session
    # from a queue or a single shared one.
    if len(sessions) == 1:
        single_session = next(iter(sessions.values()))
        def _client_session_factory(read, write):
            return single_session
    else:
        # Multiple servers: pop from a list per construction order
        session_queue = list(sessions.values())
        def _client_session_factory(read, write):
            return session_queue.pop(0) if session_queue else _FakeClientSession()

    monkeypatch.setattr("mcp.ClientSession", _client_session_factory, raising=False)

    # Patch transport factories
    monkeypatch.setattr(
        "mcp.stdio_client",
        MagicMock(return_value=_build_fake_transport_cm()),
        raising=False,
    )
    monkeypatch.setattr(
        "mcp.StdioServerParameters",
        MagicMock(),
        raising=False,
    )
    # sse / http live in submodules
    import sys
    sse_mod = MagicMock()
    sse_mod.sse_client = MagicMock(return_value=_build_fake_transport_cm())
    monkeypatch.setitem(sys.modules, "mcp.client.sse", sse_mod)

    http_mod = MagicMock()
    http_mod.streamablehttp_client = MagicMock(return_value=_build_fake_transport_cm())
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", http_mod)


# ─────────────────────────────────────────────────────────────────────
# Config parsing
# ─────────────────────────────────────────────────────────────────────


class TestConfigParsing:
    def test_mcp_default_disabled(self):
        cfg = ParadiseConfig()
        assert cfg.mcp.enabled is False
        assert cfg.mcp.servers == []

    def test_mcp_from_dict_enabled(self):
        cfg = ParadiseConfig.from_dict({
            "mcp": {
                "enabled": True,
                "servers": [
                    {"name": "fs", "command": "npx", "args": ["-y", "fs-server"]},
                    {"name": "remote", "url": "https://x.com/mcp", "transport": "http"},
                ],
            }
        })
        assert cfg.mcp.enabled is True
        assert len(cfg.mcp.servers) == 2
        assert cfg.mcp.servers[0].name == "fs"
        assert cfg.mcp.servers[0].command == "npx"
        assert cfg.mcp.servers[0].args == ["-y", "fs-server"]
        assert cfg.mcp.servers[0].transport == "stdio"
        assert cfg.mcp.servers[1].name == "remote"
        assert cfg.mcp.servers[1].url == "https://x.com/mcp"
        assert cfg.mcp.servers[1].transport == "http"

    def test_mcp_server_defaults(self):
        s = McpServerConfig(name="x")
        assert s.command == ""
        assert s.transport == "stdio"
        assert s.timeout_seconds == 60
        assert s.connect_timeout_seconds == 30


# ─────────────────────────────────────────────────────────────────────
# Manager construction
# ─────────────────────────────────────────────────────────────────────


class TestManagerConstruction:
    def test_empty_servers_raises(self):
        from paradise.tools.mcp_tool import McpClientManager
        with pytest.raises(ValueError, match="at least one server"):
            McpClientManager([])

    def test_construction_stores_servers(self):
        from paradise.tools.mcp_tool import McpClientManager
        srv = McpServerConfig(name="x", command="echo")
        m = McpClientManager([srv])
        assert m.available_servers == []
        assert m.unavailable_servers == {}
        assert m.registered_tool_names == []

    def test_call_tool_before_start_returns_error(self):
        from paradise.tools.mcp_tool import McpClientManager
        m = McpClientManager([McpServerConfig(name="x", command="echo")])
        result = json.loads(m.call_tool("x", "foo", {}))
        assert "error" in result

    def test_register_before_start_raises(self):
        from paradise.tools.mcp_tool import McpClientManager
        m = McpClientManager([McpServerConfig(name="x", command="echo")])
        with pytest.raises(RuntimeError, match="start.*must be called"):
            m.register_tools_into(global_registry)


# ─────────────────────────────────────────────────────────────────────
# Start + register lifecycle (with mocked SDK)
# ─────────────────────────────────────────────────────────────────────


class TestStartAndRegister:
    def test_start_with_single_server(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        session = _FakeClientSession(tools=[
            _FakeTool(name="read_file", description="Read a file",
                      input_schema={"type": "object", "properties": {"path": {"type": "string"}}}),
        ])
        _patch_mcp_sdk(monkeypatch, {"fs": session})

        m = McpClientManager([McpServerConfig(name="fs", command="npx",
                                              connect_timeout_seconds=5)])
        m.start()
        try:
            assert "fs" in m.available_servers
            assert session.entered
            assert session.initialized
        finally:
            asyncio.run(m.aclose())

    def test_start_marks_failed_server_unavailable(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        # Session that raises during initialize()
        bad_session = _FakeClientSession(init_raises=RuntimeError("connect refused"))
        _patch_mcp_sdk(monkeypatch, {"bad": bad_session})

        m = McpClientManager([McpServerConfig(name="bad", command="npx",
                                              connect_timeout_seconds=3)])
        m.start()
        try:
            assert "bad" not in m.available_servers
            assert "bad" in m.unavailable_servers
        finally:
            asyncio.run(m.aclose())

    def test_register_tools_creates_paradise_tools(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        session = _FakeClientSession(tools=[
            _FakeTool(name="read_file", description="Read a file",
                      input_schema={"type": "object", "properties": {"path": {"type": "string"}}}),
            _FakeTool(name="write_file", description="Write a file"),
        ])
        _patch_mcp_sdk(monkeypatch, {"fs": session})

        m = McpClientManager([McpServerConfig(name="fs", command="npx",
                                              connect_timeout_seconds=5)])
        m.start()
        try:
            test_registry = ToolRegistry()
            count = m.register_tools_into(test_registry)
            assert count == 2
            names = test_registry.get_all_tool_names()
            assert "mcp.fs.read_file" in names
            assert "mcp.fs.write_file" in names
        finally:
            asyncio.run(m.aclose())

    def test_register_tools_handles_empty_input_schema(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        session = _FakeClientSession(tools=[
            _FakeTool(name="ping", description="Ping", input_schema=None),
        ])
        _patch_mcp_sdk(monkeypatch, {"srv": session})

        m = McpClientManager([McpServerConfig(name="srv", command="x",
                                              connect_timeout_seconds=5)])
        m.start()
        try:
            test_registry = ToolRegistry()
            count = m.register_tools_into(test_registry)
            assert count == 1
            defs = test_registry.get_definitions(["mcp.srv.ping"])
            assert len(defs) == 1
            # Should fall back to default schema when inputSchema is None
            params = defs[0]["function"]["parameters"]
            assert params["type"] == "object"
        finally:
            asyncio.run(m.aclose())

    def test_registered_tool_privilege_is_read(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        session = _FakeClientSession(tools=[_FakeTool(name="x")])
        _patch_mcp_sdk(monkeypatch, {"srv": session})

        m = McpClientManager([McpServerConfig(name="srv", command="x",
                                              connect_timeout_seconds=5)])
        m.start()
        try:
            test_registry = ToolRegistry()
            m.register_tools_into(test_registry)
            entries = {e.name: e for e in test_registry._snapshot_entries()}
            assert entries["mcp.srv.x"].privilege == "read"
        finally:
            asyncio.run(m.aclose())


# ─────────────────────────────────────────────────────────────────────
# call_tool
# ─────────────────────────────────────────────────────────────────────


class TestCallTool:
    def test_successful_call_returns_text(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        session = _FakeClientSession(
            tools=[_FakeTool(name="ping")],
            call_results={"ping": _FakeCallToolResult(
                content=[_FakeTextContent("pong")],
            )},
        )
        _patch_mcp_sdk(monkeypatch, {"srv": session})

        m = McpClientManager([McpServerConfig(name="srv", command="x",
                                              connect_timeout_seconds=5,
                                              timeout_seconds=5)])
        m.start()
        try:
            result = json.loads(m.call_tool("srv", "ping", {}))
            assert "output" in result
            assert "pong" in result["output"]
            # Verify the session saw the call
            assert session.call_log == [("ping", {})]
        finally:
            asyncio.run(m.aclose())

    def test_call_returns_error_when_server_unavailable(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        bad_session = _FakeClientSession(init_raises=RuntimeError("nope"))
        _patch_mcp_sdk(monkeypatch, {"bad": bad_session})

        m = McpClientManager([McpServerConfig(name="bad", command="x",
                                              connect_timeout_seconds=3)])
        m.start()
        try:
            result = json.loads(m.call_tool("bad", "foo", {}))
            assert "error" in result
            assert "unavailable" in result["error"]
        finally:
            asyncio.run(m.aclose())

    def test_call_returns_error_when_mcp_tool_reports_error(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        session = _FakeClientSession(
            tools=[_FakeTool(name="boom")],
            call_results={"boom": _FakeCallToolResult(
                content=[_FakeTextContent("permission denied")],
                is_error=True,
            )},
        )
        _patch_mcp_sdk(monkeypatch, {"srv": session})

        m = McpClientManager([McpServerConfig(name="srv", command="x",
                                              connect_timeout_seconds=5,
                                              timeout_seconds=5)])
        m.start()
        try:
            result = json.loads(m.call_tool("srv", "boom", {}))
            assert "error" in result
            assert "permission denied" in result["error"]
        finally:
            asyncio.run(m.aclose())


# ─────────────────────────────────────────────────────────────────────
# aclose lifecycle
# ─────────────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_aclose_is_idempotent(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        session = _FakeClientSession(tools=[_FakeTool(name="x")])
        _patch_mcp_sdk(monkeypatch, {"srv": session})

        m = McpClientManager([McpServerConfig(name="srv", command="x",
                                              connect_timeout_seconds=5)])
        m.start()
        asyncio.run(m.aclose())
        # Second close must not raise
        asyncio.run(m.aclose())
        assert session.exited

    def test_aclose_closes_all_sessions(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        s1 = _FakeClientSession(tools=[_FakeTool(name="a")])
        s2 = _FakeClientSession(tools=[_FakeTool(name="b")])
        _patch_mcp_sdk(monkeypatch, {"s1": s1, "s2": s2})

        m = McpClientManager([
            McpServerConfig(name="s1", command="x", connect_timeout_seconds=5),
            McpServerConfig(name="s2", command="y", connect_timeout_seconds=5),
        ])
        m.start()
        asyncio.run(m.aclose())
        assert s1.exited
        assert s2.exited

    def test_call_after_close_returns_error(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        session = _FakeClientSession(tools=[_FakeTool(name="x")])
        _patch_mcp_sdk(monkeypatch, {"srv": session})

        m = McpClientManager([McpServerConfig(name="srv", command="x",
                                              connect_timeout_seconds=5)])
        m.start()
        asyncio.run(m.aclose())
        result = json.loads(m.call_tool("srv", "x", {}))
        assert "error" in result
        assert "closed" in result["error"]


# ─────────────────────────────────────────────────────────────────────
# Transport dispatch
# ─────────────────────────────────────────────────────────────────────


class TestTransportDispatch:
    def test_stdio_transport_uses_command(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        session = _FakeClientSession(tools=[])
        _patch_mcp_sdk(monkeypatch, {"fs": session})

        # Capture StdioServerParameters construction
        import mcp
        captured_params = {}
        def _fake_params(command="", args=None, env=None):
            captured_params["command"] = command
            captured_params["args"] = args
            captured_params["env"] = env
            return MagicMock()
        monkeypatch.setattr("mcp.StdioServerParameters", _fake_params, raising=False)

        m = McpClientManager([McpServerConfig(
            name="fs", command="npx", args=["-y", "fs-server"],
            env={"FOO": "bar"}, connect_timeout_seconds=5,
        )])
        m.start()
        asyncio.run(m.aclose())
        assert captured_params["command"] == "npx"
        assert captured_params["args"] == ["-y", "fs-server"]

    def test_sse_transport_requires_url(self):
        from paradise.tools.mcp_tool import McpClientManager
        m = McpClientManager([McpServerConfig(name="x", transport="sse")])
        # _build_transport should raise because url is empty
        # This surfaces during start() as an unavailable server
        m.start()
        try:
            assert "x" in m.unavailable_servers
        finally:
            asyncio.run(m.aclose())

    def test_http_transport_requires_url(self):
        from paradise.tools.mcp_tool import McpClientManager
        m = McpClientManager([McpServerConfig(name="x", transport="http")])
        m.start()
        try:
            assert "x" in m.unavailable_servers
        finally:
            asyncio.run(m.aclose())

    def test_unknown_transport_marks_unavailable(self):
        from paradise.tools.mcp_tool import McpClientManager
        m = McpClientManager([McpServerConfig(name="x", transport="bogus")])
        m.start()
        try:
            assert "x" in m.unavailable_servers
        finally:
            asyncio.run(m.aclose())


# ─────────────────────────────────────────────────────────────────────
# Parallel init — multiple servers, mixed success/failure
# ─────────────────────────────────────────────────────────────────────


class TestParallelInit:
    def test_mixed_success_and_failure(self, monkeypatch):
        from paradise.tools.mcp_tool import McpClientManager
        good = _FakeClientSession(tools=[_FakeTool(name="ok")])
        bad = _FakeClientSession(init_raises=RuntimeError("dead"))
        _patch_mcp_sdk(monkeypatch, {"good": good, "bad": bad})

        m = McpClientManager([
            McpServerConfig(name="good", command="x", connect_timeout_seconds=5),
            McpServerConfig(name="bad", command="y", connect_timeout_seconds=3),
        ])
        m.start()
        try:
            assert "good" in m.available_servers
            assert "bad" not in m.available_servers
            assert "bad" in m.unavailable_servers
            # Good server's tools should still register
            test_reg = ToolRegistry()
            count = m.register_tools_into(test_reg)
            assert count == 1
            assert "mcp.good.ok" in test_reg.get_all_tool_names()
        finally:
            asyncio.run(m.aclose())
