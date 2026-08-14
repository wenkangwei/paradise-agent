"""Phase 3-B — factory.build_supervisor MCP wiring tests.

Run:
    cd server && python -m pytest paradise/tests/test_factory_mcp_wiring.py -v

Scope:
  * build_supervisor returns 3-tuple (compiled, registry, mcp_manager)
  * mcp_manager is None when mcp.enabled=False (default)
  * mcp_manager is None when mcp.enabled=True but servers=[]
  * mcp_manager is constructed when enabled + servers non-empty (mocked)
  * bash config is injected into bash_tool module singleton
  * build_supervisor fail-soft still returns 3-tuple
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from paradise.config import (
    ParadiseConfig, LLMConfig, BashConfig, McpConfig, McpServerConfig,
)
from paradise.tools import bash_tool


# ─────────────────────────────────────────────────────────────────────
# 3-tuple return shape
# ─────────────────────────────────────────────────────────────────────


class TestBuildSupervisorReturnShape:
    def test_returns_three_tuple_when_mcp_disabled(self):
        """Default config has mcp.enabled=False — third element is None."""
        from paradise.factory import build_supervisor
        cfg = ParadiseConfig(agent_id="shape-test")
        cfg.llm = LLMConfig(provider="ollama", model="qwen2.5:0.5b",
                            api_url="http://localhost:11434")
        result = build_supervisor(cfg)
        assert isinstance(result, tuple)
        assert len(result) == 3
        compiled, registry, mcp_manager = result
        # Either all populated (success) or all None (fail-soft)
        if compiled is None:
            assert registry is None
        assert mcp_manager is None  # always None when mcp disabled

    def test_returns_three_tuple_with_empty_mcp_servers(self):
        """mcp.enabled=True but servers=[] → no manager constructed."""
        from paradise.factory import build_supervisor
        cfg = ParadiseConfig(agent_id="empty-mcp")
        cfg.llm = LLMConfig(provider="ollama", model="qwen2.5:0.5b",
                            api_url="http://localhost:11434")
        cfg.mcp = McpConfig(enabled=True, servers=[])
        result = build_supervisor(cfg)
        assert len(result) == 3
        _, _, mcp_manager = result
        assert mcp_manager is None


# ─────────────────────────────────────────────────────────────────────
# MCP wiring with mocked McpClientManager
# ─────────────────────────────────────────────────────────────────────


class TestMcpWiring:
    def test_mcp_manager_constructed_when_enabled(self):
        """When mcp.enabled=True and servers non-empty, _build_mcp_manager
        constructs McpClientManager, calls start + register_tools_into."""
        from paradise.factory import build_supervisor

        cfg = ParadiseConfig(agent_id="mcp-wire")
        cfg.llm = LLMConfig(provider="ollama", model="qwen2.5:0.5b",
                            api_url="http://localhost:11434")
        cfg.mcp = McpConfig(
            enabled=True,
            servers=[McpServerConfig(name="fs", command="npx")],
        )

        # Mock McpClientManager so no real subprocess is spawned
        fake_manager = MagicMock()
        fake_manager.unavailable_servers = {}
        fake_manager.register_tools_into = MagicMock(return_value=3)

        with patch("paradise.tools.mcp_tool.McpClientManager",
                   return_value=fake_manager) as mock_ctor:
            result = build_supervisor(cfg)

        # Manager should be the fake, returned as third element
        _, _, mcp_manager = result
        if mcp_manager is not None:
            assert mcp_manager is fake_manager
            mock_ctor.assert_called_once()
            fake_manager.start.assert_called_once()
            fake_manager.register_tools_into.assert_called_once()

    def test_mcp_failure_returns_none_manager(self):
        """If McpClientManager.start() raises, _build_mcp_manager
        catches + logs + returns None (fail-soft)."""
        from paradise.factory import build_supervisor

        cfg = ParadiseConfig(agent_id="mcp-fail")
        cfg.llm = LLMConfig(provider="ollama", model="qwen2.5:0.5b",
                            api_url="http://localhost:11434")
        cfg.mcp = McpConfig(
            enabled=True,
            servers=[McpServerConfig(name="fs", command="npx")],
        )

        fake_manager = MagicMock()
        fake_manager.start.side_effect = RuntimeError("subprocess died")

        with patch("paradise.tools.mcp_tool.McpClientManager",
                   return_value=fake_manager):
            result = build_supervisor(cfg)

        _, _, mcp_manager = result
        # Manager should be None because start() raised
        assert mcp_manager is None


# ─────────────────────────────────────────────────────────────────────
# Bash config injection
# ─────────────────────────────────────────────────────────────────────


class TestBashConfigInjection:
    def test_build_supervisor_injects_bash_config(self):
        """build_supervisor should call bash_tool.configure(config.bash)
        so handlers see the configured timeout/memory values."""
        from paradise.factory import build_supervisor

        cfg = ParadiseConfig(agent_id="bash-inject")
        cfg.llm = LLMConfig(provider="ollama", model="qwen2.5:0.5b",
                            api_url="http://localhost:11434")
        cfg.bash = BashConfig(
            enabled=True,
            read_timeout_seconds=42,
            write_timeout_seconds=77,
            write_memory_limit_mb=512,
        )

        # Snapshot before
        before_read = bash_tool._config.read_timeout_seconds

        build_supervisor(cfg)

        # After build, bash_tool should reflect the injected config
        assert bash_tool._config.read_timeout_seconds == 42
        assert bash_tool._config.write_timeout_seconds == 77
        assert bash_tool._config.write_memory_limit_mb == 512

    def test_build_supervisor_discovers_builtin_tools(self):
        """After build, global registry should contain bash_read/bash_write."""
        from paradise.factory import build_supervisor
        from paradise.tools.registry import registry as global_reg

        cfg = ParadiseConfig(agent_id="discover-test")
        cfg.llm = LLMConfig(provider="ollama", model="qwen2.5:0.5b",
                            api_url="http://localhost:11434")
        build_supervisor(cfg)

        names = set(global_reg.get_all_tool_names())
        assert "bash_read" in names
        assert "bash_write" in names
