"""Tool executor with privilege rings and subprocess isolation.

Phase 4 — Sandbox hardening for production.

Privilege rings:
  - "read":  default, runs in-process (current behaviour)
  - "write": runs in subprocess with timeout + memory limit
  - "admin": rejected unless PARADISE_ENABLE_ADMIN_TOOLS=1; runs in subprocess

Existing tools (web_search, voice_transcribe, etc.) default to "read" and
behave exactly as before. Only tools that explicitly register with
privilege="write" or "admin" trigger the new code paths.

Main-branch code never imports this module — agent.py and tool execution
go through execute_tool() in tools/builtin.py which is unchanged.
"""
from __future__ import annotations

import logging
import os
import resource
import subprocess
import sys
from typing import Any

logger = logging.getLogger("paradise.tools.executor")


class PrivilegeDenied(Exception):
    """Raised when a tool call is rejected by privilege ring."""


class ToolTimeout(Exception):
    """Raised when a subprocess-isolated tool exceeds its time budget."""


class ToolMemoryExceeded(Exception):
    """Raised when a subprocess-isolated tool exceeds its memory budget."""


def check_privilege(tool_entry, enable_admin: bool = False) -> None:
    """Gate a tool call by its privilege ring.

    Args:
        tool_entry: ToolEntry from registry (must have .privilege attribute)
        enable_admin: True if PARADISE_ENABLE_ADMIN_TOOLS=1

    Raises:
        PrivilegeDenied: if admin tool called without enable_admin
    """
    priv = getattr(tool_entry, "privilege", "read")
    if priv == "admin" and not enable_admin:
        logger.warning(
            "Admin tool %r rejected (PARADISE_ENABLE_ADMIN_TOOLS not set)",
            tool_entry.name,
        )
        raise PrivilegeDenied(
            f"Admin tool '{tool_entry.name}' requires PARADISE_ENABLE_ADMIN_TOOLS=1"
        )
    # read and write always allowed at the privilege layer;
    # write gets subprocess isolation in execute_with_sandbox()
    return


def execute_with_sandbox(
    handler,
    args: dict,
    *,
    timeout_seconds: int = 30,
    memory_limit_mb: int = 256,
) -> Any:
    """Run a write-privilege tool in an isolated subprocess.

    For Phase 4 we use a subprocess that re-imports the handler and calls
    it with the provided args. Memory limit applied via RLIMIT_AS (Linux).
    Timeout enforced via subprocess.run().

    Note: this only works for tools whose handler is a picklable callable
    reachable by import path. For lambda handlers or closures, fall back to
    in-process execution (with a logged warning).
    """
    # Heuristic: if handler has __module__ and __qualname__, we can re-import
    module = getattr(handler, "__module__", None)
    qualname = getattr(handler, "__qualname__", None)
    if not module or not qualname or "<lambda>" in qualname:
        logger.warning(
            "Handler %r is not subprocess-safe (closure/lambda); running in-process",
            handler,
        )
        return handler(args)

    # Build a tiny worker script that imports the handler and runs it
    worker_code = f"""
import importlib, json, sys, resource
mod = importlib.import_module({module!r})
obj = mod
for part in {qualname!r}.split("."):
    if part == "<module>":
        continue
    obj = getattr(obj, part)
args = json.loads(sys.stdin.read())
# Apply memory limit (RLIMIT_AS = address space in bytes)
mem_bytes = {memory_limit_mb} * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
try:
    result = obj(args)
    print(json.dumps({{"ok": True, "result": result}}))
except Exception as e:
    print(json.dumps({{"ok": False, "error": repr(e)}}))
"""

    try:
        import json
        proc = subprocess.run(
            [sys.executable, "-c", worker_code],
            input=json.dumps(args),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolTimeout(
            f"Tool exceeded {timeout_seconds}s budget"
        ) from exc

    if proc.returncode != 0:
        # Process killed by OS (likely OOM with RLIMIT_AS) or other failure
        stderr_tail = (proc.stderr or "")[-500:]
        if "MemoryError" in stderr_tail or "Cannot allocate memory" in stderr_tail:
            raise ToolMemoryExceeded(
                f"Tool exceeded {memory_limit_mb}MB memory budget"
            )
        raise RuntimeError(f"Subprocess failed rc={proc.returncode}: {stderr_tail}")

    import json
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Subprocess returned invalid JSON: {proc.stdout[:200]}") from exc

    if not payload.get("ok"):
        raise RuntimeError(payload.get("error", "unknown subprocess error"))
    return payload["result"]


def dispatch_tool_call(
    tool_entry,
    args: dict,
    *,
    enable_admin: bool | None = None,
    timeout_seconds: int = 30,
    memory_limit_mb: int = 256,
) -> Any:
    """Unified entry point — checks privilege, runs in-process or subprocess.

    This is the function Phase 4 wires into the agent's tool execution path
    (replacing direct handler calls). Existing tools (privilege="read")
    behave identically to before — pure in-process call.

    Args:
        tool_entry: ToolEntry from registry
        args: dict of arguments for the tool handler
        enable_admin: True to allow admin tools (defaults to env var)
        timeout_seconds: subprocess timeout for write/admin tools
        memory_limit_mb: subprocess memory cap for write/admin tools

    Raises:
        PrivilegeDenied, ToolTimeout, ToolMemoryExceeded
    """
    if enable_admin is None:
        enable_admin = os.getenv("PARADISE_ENABLE_ADMIN_TOOLS", "").lower() in ("1", "true", "yes")

    check_privilege(tool_entry, enable_admin=enable_admin)

    priv = getattr(tool_entry, "privilege", "read")
    if priv == "read":
        # Default fast path — no isolation
        return tool_entry.handler(args)
    # write or admin → subprocess isolation
    return execute_with_sandbox(
        tool_entry.handler,
        args,
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    )
