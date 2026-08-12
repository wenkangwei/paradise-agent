"""Phase 4 tests — privilege rings and sandbox executor."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _make_entry(name="t", privilege="read"):
    """Build a fake ToolEntry with the required attributes."""
    e = MagicMock()
    e.name = name
    e.privilege = privilege
    return e


# ── Privilege check ────────────────────────────────────────────────

def test_check_privilege_read_passes():
    from paradise.tools.executor import check_privilege
    check_privilege(_make_entry("search", "read"), enable_admin=False)


def test_check_privilege_write_passes():
    """write tools don't require enable_admin — they just get subprocess isolation."""
    from paradise.tools.executor import check_privilege
    check_privilege(_make_entry("file_write", "write"), enable_admin=False)


def test_check_privilege_admin_rejected_by_default():
    from paradise.tools.executor import check_privilege, PrivilegeDenied
    with pytest.raises(PrivilegeDenied):
        check_privilege(_make_entry("shell", "admin"), enable_admin=False)


def test_check_privilege_admin_allowed_when_enabled():
    from paradise.tools.executor import check_privilege
    check_privilege(_make_entry("shell", "admin"), enable_admin=True)


# ── In-process dispatch (read tools) ──────────────────────────────

def test_dispatch_read_tool_runs_in_process():
    """Read tools must run in-process — no subprocess overhead."""
    from paradise.tools.executor import dispatch_tool_call

    entry = MagicMock()
    entry.name = "echo"
    entry.privilege = "read"
    entry.handler = lambda args: f"echoed: {args['msg']}"

    result = dispatch_tool_call(entry, {"msg": "hi"})
    assert result == "echoed: hi"


def test_dispatch_admin_tool_rejected_without_env():
    from paradise.tools.executor import dispatch_tool_call, PrivilegeDenied

    entry = MagicMock()
    entry.name = "rm_rf"
    entry.privilege = "admin"
    entry.handler = lambda args: "should not run"

    # Ensure env var unset
    old = os.environ.pop("PARADISE_ENABLE_ADMIN_TOOLS", None)
    try:
        with pytest.raises(PrivilegeDenied):
            dispatch_tool_call(entry, {})
    finally:
        if old is not None:
            os.environ["PARADISE_ENABLE_ADMIN_TOOLS"] = old


def test_dispatch_admin_tool_runs_when_env_set(monkeypatch):
    from paradise.tools.executor import dispatch_tool_call

    entry = MagicMock()
    entry.name = "rm_rf"
    entry.privilege = "admin"
    entry.handler = lambda args: "admin ok"

    monkeypatch.setenv("PARADISE_ENABLE_ADMIN_TOOLS", "1")
    # Note: admin tools would normally go through subprocess; but our test handler
    # is a lambda which can't be subprocessed — dispatch falls back to in-process
    # with a warning (see executor.execute_with_sandbox).
    result = dispatch_tool_call(entry, {})
    assert result == "admin ok"


# ── Subprocess isolation for write tools ──────────────────────────

def test_subprocess_isolation_enforces_timeout():
    """A write-privilege tool that runs forever must hit the timeout."""
    from paradise.tools.executor import dispatch_tool_call, ToolTimeout

    # Define a module-level handler that sleeps
    import paradise.tests.test_executor as self_mod

    entry = MagicMock()
    entry.name = "slow"
    entry.privilege = "write"
    # Use a real importable handler (this test module's _slow_handler)
    entry.handler = self_mod._slow_handler

    with pytest.raises(ToolTimeout):
        dispatch_tool_call(
            entry, {"seconds": 5},
            timeout_seconds=1,  # force timeout
            memory_limit_mb=64,
        )


def test_subprocess_isolation_enforces_memory():
    """A write-privilege tool that allocates a huge list must hit memory limit."""
    from paradise.tools.executor import dispatch_tool_call, ToolMemoryExceeded

    import paradise.tests.test_executor as self_mod

    entry = MagicMock()
    entry.name = "hog"
    entry.privilege = "write"
    entry.handler = self_mod._memory_hog_handler

    with pytest.raises((ToolMemoryExceeded, RuntimeError)):
        dispatch_tool_call(
            entry, {"mb": 200},
            timeout_seconds=10,
            memory_limit_mb=32,  # tight
        )


# ── Handlers used by subprocess tests (must be module-level) ──────

def _slow_handler(args):
    import time
    time.sleep(args.get("seconds", 5))
    return "done"


def _memory_hog_handler(args):
    mb = args.get("mb", 100)
    _ = bytearray(mb * 1024 * 1024)  # allocate big chunk
    return "allocated"
