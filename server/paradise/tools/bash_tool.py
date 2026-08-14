"""Bash tool — read/write privilege split.

Phase 3-A — replaces the coarse ``bash`` tool in ``builtin.py`` with two
distinct tools so the LLM sees explicit function names and the privilege
is encoded statically at registration time:

    bash_read   (privilege="read")   — allowlist model: only known-safe
                                       commands (ls/cat/grep/find/...).
                                       Rejects shell metacharacters.
    bash_write  (privilege="write")  — blocklist model + ulimit isolation
                                       (memory cap, CPU cap, timeout).
                                       Rejects clearly catastrophic
                                       commands (rm -rf /, mkfs, ...).

Design notes:

  * Allowlist > blocklist for read path. Blocklists have to anticipate
    every novel dangerous command; allowlists just enumerate what's OK.
    Downside: LLM may try a command not on the list — the rejection
    message includes the allowlist so the model can self-correct.
  * The privilege field on ToolEntry (Phase 4) is currently audit-only.
    Handlers implement their own safety inline. When Phase 4 wires
    ``executor.dispatch_tool_call`` into the tool_react path, the
    privilege ring becomes enforceable; until then it's documentation.
  * Config is injected via module-level ``configure()`` so the handler
    signature stays ``(args) -> str`` (compatible with registry).
    Falls back to ``BashConfig()`` defaults when configure() not called.

Self-registers at import via ``registry.register()`` — picked up by
``discover_builtin_tools()`` in tools/registry.py.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import signal
import logging
from typing import Any

from paradise.config import BashConfig
from paradise.tools.registry import registry, tool_error, tool_result
from paradise.tools.sandbox import (
    SANDBOX_ROOT,
    contains_blocked_file,
    is_write_command,
    truncate,
)

logger = logging.getLogger(__name__)


# ── Module-level config (injected by factory.build_supervisor) ───────
#
# Singleton pattern so handler(args) signature stays clean. Defaults to
# BashConfig() if configure() never called — sane behavior in tests and
# dev mode without a full prod config chain.
_config: BashConfig = BashConfig()


def configure(cfg: BashConfig) -> None:
    """Inject runtime config. Called by factory.build_supervisor."""
    global _config
    _config = cfg


def _resolve_cwd() -> str:
    """Working directory for subprocess: explicit sandbox_root or default."""
    if _config.sandbox_root:
        return _config.sandbox_root
    return str(SANDBOX_ROOT)


# ─────────────────────────────────────────────────────────────────────
# bash_read — allowlist of safe commands
# ─────────────────────────────────────────────────────────────────────

# Two-tier allowlist:
#   1. Multi-word commands like "git status" matched as a whole
#   2. Single-word commands matched on first token only
_BASH_READ_ALLOWED_MULTI = frozenset({
    "git status", "git log", "git diff", "git branch",
    "git show", "git remote", "git config --get",
    "docker ps", "docker logs", "docker images", "docker inspect",
})

_BASH_READ_ALLOWED_SINGLE = frozenset({
    # Listing & reading
    "ls", "ll", "la", "cat", "head", "tail", "less", "more",
    # Searching
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    # Locating
    "find", "locate", "which", "whereis", "type", "file",
    # Metadata
    "stat", "wc", "du", "df", "lsblk", "mount",
    # Process / system
    "ps", "top", "htop", "uptime", "who", "whoami", "id",
    "uname", "hostname", "pwd", "date", "cal", "nproc",
    # Env (read-only)
    "env", "printenv",
    # Text processing (read-only usage)
    "tree", "diff", "sort", "uniq", "cut", "tr", "tee",
    # Path helpers
    "basename", "dirname", "realpath",
    # Echo/printf allowed (harmless, useful for debugging)
    "echo", "printf",
    # Network read-only (no packet send)
    "ip", "ifconfig", "netstat", "ss",
})

# Shell metacharacters that bash_read must NEVER accept (injection defense)
_BASH_READ_FORBIDDEN_METACHARS = (
    ">", ">>", "<", "|", ";", "&", "`", "$(", "${",
)


def _is_read_allowed(command: str) -> tuple[bool, str]:
    """Check whether *command* is in the read allowlist.

    Returns (allowed, reason_if_not).
    """
    cmd = command.strip()
    if not cmd:
        return False, "empty command"

    # Layer 1: reject any metacharacter regardless of command
    cmd_lower = cmd.lower()
    for mc in _BASH_READ_FORBIDDEN_METACHARS:
        if mc in cmd_lower:
            return False, (
                f"shell metacharacter {mc!r} not allowed in bash_read "
                f"(use bash_write for commands needing pipes/redirects)"
            )

    # Layer 2: try multi-word match first (e.g. "git status")
    # Check progressively shorter prefixes — handles "git config --get user.name"
    tokens = shlex.split(cmd)
    if not tokens:
        return False, "could not parse command"

    # Try 3-token, then 2-token, then 1-token prefixes
    for n in (min(3, len(tokens)), min(2, len(tokens)), 1):
        prefix = " ".join(tokens[:n]).lower()
        if prefix in _BASH_READ_ALLOWED_MULTI:
            return True, ""

    # Single-token check
    if tokens[0].lower() in _BASH_READ_ALLOWED_SINGLE:
        return True, ""

    return False, (
        f"command {tokens[0]!r} not in bash_read allowlist. "
        f"Allowed: {sorted(_BASH_READ_ALLOWED_SINGLE | _BASH_READ_ALLOWED_MULTI)[:20]}... "
        f"(use bash_write for write operations)"
    )


# ─────────────────────────────────────────────────────────────────────
# bash_write — blocklist + ulimit isolation
# ─────────────────────────────────────────────────────────────────────

# Catastrophic patterns that we reject outright even in write mode.
# These are kill-the-system commands; finer-grained privilege decisions
# are the privilege ring's job (Phase 4).
_BASH_WRITE_FORBIDDEN = (
    "rm -rf /", "rm -rf ~", "rm -rf *", "rm -rf .",
    "rm -rf $home",
    "mkfs", "dd if=/dev/zero of=/dev/",
    "dd of=/dev/sd", "dd of=/dev/nvme",
    ":(){ :|:& };:",                  # classic fork bomb
    "> /dev/sd", "> /dev/nvme",
    "chmod -r 777 /",
    "chown -r ",
    "shutdown", "reboot", "init 0", "halt",
    "systemctl stop",
    # Remote-script execution: block any pipe into a shell interpreter,
    # regardless of what's before the pipe (curl URL | sh, wget | bash, …)
    "| sh", "| bash", "| /bin/sh", "| /bin/bash",
    "curl | sh", "curl | bash", "wget | sh", "wget | bash",
    "apt upgrade", "apt dist-upgrade", "apt-get upgrade",
    "yum update", "dnf update",
    "npm publish",                     # irreversible registry publish
    "git push --force", "git push -f",
)


def _is_write_forbidden(command: str) -> tuple[bool, str | None]:
    """Return (forbidden, matched_pattern_or_None)."""
    cmd_lower = command.lower()
    for pat in _BASH_WRITE_FORBIDDEN:
        if pat in cmd_lower:
            return True, pat
    # Also defer to sandbox.is_write_command for legacy checks
    return False, None


# ─────────────────────────────────────────────────────────────────────
# Handlers (async — both use asyncio.create_subprocess_shell)
# ─────────────────────────────────────────────────────────────────────

async def _handle_bash_read(args: dict[str, Any]) -> str:
    """bash_read handler — allowlist + read-only sandbox."""
    command = args.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return tool_error("empty or invalid command")

    # Layer 1: allowlist
    allowed, reason = _is_read_allowed(command)
    if not allowed:
        logger.info("bash_read rejected: %s — %s", command[:80], reason)
        return tool_error(reason)

    # Layer 2: blocked-file reference (e.g. config.json)
    blocked = contains_blocked_file(command)
    if blocked:
        return tool_error(f"{blocked} is a protected config file")

    cwd = _resolve_cwd()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,  # own process group for clean kill
        )
    except Exception as exc:
        return tool_error(f"failed to start subprocess: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_config.read_timeout_seconds,
        )
    except asyncio.TimeoutError:
        _kill_process_group(proc)
        return tool_error(
            f"command exceeded {_config.read_timeout_seconds}s timeout"
        )

    output = _format_output(command, stdout, stderr)
    return truncate(output)


async def _handle_bash_write(args: dict[str, Any]) -> str:
    """bash_write handler — blocklist + ulimit (mem + cpu + timeout)."""
    command = args.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return tool_error("empty or invalid command")

    # Layer 1: catastrophic blocklist
    forbidden, pattern = _is_write_forbidden(command)
    if forbidden:
        logger.warning("bash_write BLOCKED catastrophic pattern %r: %s",
                       pattern, command[:120])
        return tool_error(
            f"command blocked by catastrophic-pattern filter (matched: {pattern!r})"
        )

    # Layer 2: blocked-file reference
    blocked = contains_blocked_file(command)
    if blocked:
        return tool_error(f"{blocked} is a protected config file")

    # Wrap with ulimit — applied inside the subprocess shell.
    # RLIMIT_AS = virtual memory cap; RLIMIT_CPU = CPU seconds; RLIMIT_FSIZE = file write size.
    mem_kb = _config.write_memory_limit_mb * 1024
    cpu_s = max(1, _config.write_timeout_seconds)
    # fsize cap: 100MB per file write — prevents dd-of-zero style disk fill
    fsize_kb = 100 * 1024
    wrapped = (
        f"ulimit -v {mem_kb} 2>/dev/null; "
        f"ulimit -t {cpu_s} 2>/dev/null; "
        f"ulimit -f {fsize_kb} 2>/dev/null; "
        f"{command}"
    )

    cwd = _resolve_cwd()
    try:
        proc = await asyncio.create_subprocess_shell(
            wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
    except Exception as exc:
        return tool_error(f"failed to start subprocess: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_config.write_timeout_seconds,
        )
    except asyncio.TimeoutError:
        _kill_process_group(proc)
        return tool_error(
            f"command exceeded {_config.write_timeout_seconds}s timeout "
            f"(process group killed)"
        )

    output = _format_output(command, stdout, stderr, proc.returncode)
    return truncate(output)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the entire process group of *proc*. Best-effort."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("killpg failed for pid=%s: %s", proc.pid, exc)
    try:
        proc.kill()
    except Exception:
        pass


def _format_output(
    command: str,
    stdout: bytes | None,
    stderr: bytes | None,
    returncode: int | None = None,
) -> str:
    """Combine stdout/stderr/returncode into a single truncated-pending string."""
    parts = [f"$ {command}"]
    if stdout:
        parts.append(stdout.decode("utf-8", errors="replace"))
    if stderr:
        decoded = stderr.decode("utf-8", errors="replace")
        marker = "STDERR:" if (stdout or returncode is None) else ""
        parts.append(f"{marker}\n{decoded}" if marker else decoded)
    if returncode is not None and returncode != 0:
        parts.append(f"[exit code: {returncode}]")
    if not stdout and not stderr:
        parts.append("(no output)")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────

_BASH_READ_SCHEMA = {
    "name": "bash_read",
    "description": (
        "Execute a read-only bash command (ls, cat, grep, find, ps, git log, etc.). "
        "Allowlist-enforced: only known-safe commands accepted. "
        "Pipes, redirects, and shell metacharacters are NOT allowed "
        "(use bash_write for those). Output truncated to 4000 chars."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Read-only bash command, e.g. 'ls -la /tmp' or 'git log --oneline -10'.",
            }
        },
        "required": ["command"],
    },
}

_BASH_WRITE_SCHEMA = {
    "name": "bash_write",
    "description": (
        "Execute a bash command that may write/modify (mkdir, cp, mv, rm, "
        "pip install, etc.). Runs in an isolated subprocess with memory cap "
        "(256MB default), CPU timeout, and 30s wall-clock timeout. "
        "Catastrophic patterns (rm -rf /, mkfs, dd of=/dev/, ...) are blocked."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Bash command to execute, e.g. 'mkdir -p /tmp/work && echo done'.",
            }
        },
        "required": ["command"],
    },
}


# ─────────────────────────────────────────────────────────────────────
# Self-register at import
# ─────────────────────────────────────────────────────────────────────

registry.register(
    name="bash_read",
    toolset="bash",
    schema=_BASH_READ_SCHEMA,
    handler=_handle_bash_read,
    is_async=True,
    description="Execute read-only bash commands (allowlist-enforced)",
    privilege="read",
)

registry.register(
    name="bash_write",
    toolset="bash",
    schema=_BASH_WRITE_SCHEMA,
    handler=_handle_bash_write,
    is_async=True,
    description="Execute write-capable bash commands (blocklist + ulimit isolation)",
    privilege="write",
)


__all__ = ["configure", "_handle_bash_read", "_handle_bash_write"]
