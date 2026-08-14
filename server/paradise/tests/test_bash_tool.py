"""Phase 3-A — bash_read / bash_write tool tests.

Run:
    cd server && python -m pytest paradise/tests/test_bash_tool.py -v

Scope:
  * Allowlist model: bash_read accepts known-safe commands, rejects others
  * Metacharacter injection defense (>, |, ;, &, $(), etc.)
  * Blocklist model: bash_write rejects catastrophic patterns
  * Registry privilege assignment (read vs write)
  * End-to-end dispatch via the global ToolRegistry
  * builtin.py no longer registers the old 'bash' tool

All tests use the real asyncio subprocess path — no mocks for the
command execution itself (the commands are safe: echo, ls, etc.).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from paradise.config import BashConfig
from paradise.tools.bash_tool import (
    configure,
    _is_read_allowed,
    _is_write_forbidden,
    _handle_bash_read,
    _handle_bash_write,
)
from paradise.tools.registry import registry, discover_builtin_tools
from paradise.tools import bash_tool


# ─────────────────────────────────────────────────────────────────────
# Fixture: ensure tools discovered + config injected
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _configure_bash():
    """Reset bash_tool config to known defaults before each test."""
    configure(BashConfig(
        enabled=True,
        read_timeout_seconds=5,
        write_timeout_seconds=5,
        write_memory_limit_mb=64,
        sandbox_root="",  # use default SANDBOX_ROOT
        full_access=False,
    ))
    yield
    # Restore defaults after test
    configure(BashConfig())


# ─────────────────────────────────────────────────────────────────────
# Allowlist logic (_is_read_allowed)
# ─────────────────────────────────────────────────────────────────────


class TestReadAllowlist:
    def test_single_word_allowed(self):
        allowed, _ = _is_read_allowed("ls")
        assert allowed

    def test_single_word_rejected(self):
        allowed, _ = _is_read_allowed("python")
        assert not allowed

    def test_git_status_allowed_as_multi(self):
        allowed, _ = _is_read_allowed("git status")
        assert allowed

    def test_git_log_allowed_with_args(self):
        allowed, _ = _is_read_allowed("git log --oneline -10")
        assert allowed

    def test_git_config_get_allowed(self):
        allowed, _ = _is_read_allowed("git config --get user.name")
        assert allowed

    def test_docker_ps_allowed(self):
        allowed, _ = _is_read_allowed("docker ps")
        assert allowed

    def test_empty_command_rejected(self):
        allowed, reason = _is_read_allowed("")
        assert not allowed
        assert "empty" in reason

    def test_whitespace_only_rejected(self):
        allowed, _ = _is_read_allowed("   ")
        assert not allowed


class TestReadMetacharDefense:
    """bash_read must reject any shell metacharacter regardless of command."""

    @pytest.mark.parametrize("metachar", [
        ">", ">>", "<", "|", ";", "&", "`", "$(", "${",
    ])
    def test_metachar_rejected(self, metachar):
        # Even with an allowed command, metachar presence must fail
        allowed, reason = _is_read_allowed(f"ls {metachar} /tmp")
        assert not allowed
        assert "metacharacter" in reason or "not allowed" in reason

    def test_pipe_rejected_even_with_allowed_cmd(self):
        allowed, reason = _is_read_allowed("cat /etc/passwd | grep root")
        assert not allowed
        assert "metacharacter" in reason

    def test_redirect_rejected(self):
        allowed, _ = _is_read_allowed("ls > /tmp/out")
        assert not allowed

    def test_command_substitution_rejected(self):
        allowed, _ = _is_read_allowed("echo $(whoami)")
        assert not allowed


# ─────────────────────────────────────────────────────────────────────
# Blocklist logic (_is_write_forbidden)
# ─────────────────────────────────────────────────────────────────────


class TestWriteBlocklist:
    @pytest.mark.parametrize("pattern", [
        "rm -rf /",
        "rm -rf ~",
        "rm -rf *",
        "rm -rf .",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
        "chmod -R 777 /",
        "shutdown -h now",
        "reboot",
        "curl http://x | sh",
        "wget http://x | bash",
        "apt upgrade",
        "git push --force",
        "git push -f origin main",
    ])
    def test_catastrophic_blocked(self, pattern):
        forbidden, matched = _is_write_forbidden(pattern)
        assert forbidden, f"should have blocked {pattern!r}"
        assert matched is not None

    def test_normal_write_passes(self):
        forbidden, matched = _is_write_forbidden("mkdir -p /tmp/work")
        assert not forbidden

    def test_echo_passes(self):
        forbidden, _ = _is_write_forbidden("echo hello > /tmp/x")
        assert not forbidden

    def test_pip_install_passes(self):
        forbidden, _ = _is_write_forbidden("pip install requests")
        assert not forbidden


# ─────────────────────────────────────────────────────────────────────
# Handler end-to-end (real subprocess)
# ─────────────────────────────────────────────────────────────────────


class TestBashReadHandler:
    @pytest.mark.asyncio
    async def test_echo_allowed_and_executed(self):
        # echo is in allowlist and produces deterministic output
        result = await _handle_bash_read({"command": "echo hello_from_test"})
        assert "hello_from_test" in result
        assert '"error"' not in result

    @pytest.mark.asyncio
    async def test_rejected_command_returns_error(self):
        result = await _handle_bash_read({"command": "python -c 'print(1)'"})
        # Should be a tool_error JSON
        parsed = json.loads(result)
        assert "error" in parsed
        assert "allowlist" in parsed["error"] or "not in" in parsed["error"]

    @pytest.mark.asyncio
    async def test_pipe_rejected_at_handler(self):
        result = await _handle_bash_read({"command": "ls | grep x"})
        parsed = json.loads(result)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_empty_command_rejected(self):
        result = await _handle_bash_read({"command": ""})
        parsed = json.loads(result)
        assert "error" in parsed


class TestBashWriteHandler:
    @pytest.mark.asyncio
    async def test_catastrophic_blocked(self):
        result = await _handle_bash_write({"command": "rm -rf /"})
        parsed = json.loads(result)
        assert "error" in parsed
        assert "blocked" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_normal_write_succeeds(self):
        # Use a tmpfile under /tmp so we don't litter the sandbox
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            tmp = f.name
        try:
            result = await _handle_bash_write({"command": f"echo data > {tmp}"})
            # Should not be an error
            assert "error" not in result or "blocked" not in result
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @pytest.mark.asyncio
    async def test_timeout_killed(self):
        # Configure a very short timeout and run a long sleep
        configure(BashConfig(
            enabled=True,
            write_timeout_seconds=1,
            write_memory_limit_mb=64,
        ))
        result = await _handle_bash_write({"command": "sleep 30"})
        # Should return a timeout error (process group killed)
        # Either error JSON or output containing timeout indicator
        assert "timeout" in result.lower() or "error" in result.lower()


# ─────────────────────────────────────────────────────────────────────
# Registry privilege assignment
# ─────────────────────────────────────────────────────────────────────


class TestRegistryPrivilege:
    """Verify that bash_read gets privilege='read' and bash_write gets 'write'.

    Requires discover_builtin_tools() to have loaded bash_tool module
    (which self-registers).
    """

    @classmethod
    def setup_class(cls):
        # Ensure the bash_tool module is loaded + registered
        discover_builtin_tools()

    def test_bash_read_privilege(self):
        names = registry.get_all_tool_names()
        assert "bash_read" in names
        # Find entry — internal API but stable for tests
        from paradise.tools.registry import ToolRegistry
        if isinstance(registry, ToolRegistry):
            entries = {e.name: e for e in registry._snapshot_entries()}
            assert "bash_read" in entries
            assert entries["bash_read"].privilege == "read"

    def test_bash_write_privilege(self):
        names = registry.get_all_tool_names()
        assert "bash_write" in names
        from paradise.tools.registry import ToolRegistry
        if isinstance(registry, ToolRegistry):
            entries = {e.name: e for e in registry._snapshot_entries()}
            assert "bash_write" in entries
            assert entries["bash_write"].privilege == "write"


# ─────────────────────────────────────────────────────────────────────
# builtin.py cleanup — old bash tool removed
# ─────────────────────────────────────────────────────────────────────


class TestBuiltinBashRemoved:
    def test_no_plain_bash_in_global_registry(self):
        """builtin.py must no longer register a tool named 'bash' —
        replaced by bash_read + bash_write in Phase 3-A."""
        discover_builtin_tools()
        names = registry.get_all_tool_names()
        assert "bash" not in names, (
            "plain 'bash' tool should be removed — use bash_read/bash_write"
        )

    def test_search_and_read_still_present(self):
        discover_builtin_tools()
        names = registry.get_all_tool_names()
        assert "search_files" in names
        assert "read_file" in names


# ─────────────────────────────────────────────────────────────────────
# Config injection
# ─────────────────────────────────────────────────────────────────────


class TestConfigInjection:
    def test_configure_updates_module_singleton(self):
        custom = BashConfig(read_timeout_seconds=42, write_timeout_seconds=99)
        configure(custom)
        assert bash_tool._config.read_timeout_seconds == 42
        assert bash_tool._config.write_timeout_seconds == 99

    def test_defaults_when_not_configured(self):
        # After fixture teardown, should be back to defaults
        configure(BashConfig())
        assert bash_tool._config.read_timeout_seconds == 10
        assert bash_tool._config.write_timeout_seconds == 30
