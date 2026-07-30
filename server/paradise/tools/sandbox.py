"""Sandbox security for tool execution."""

from __future__ import annotations

import os
from pathlib import Path

# Sandbox root: agents can only access files under this directory
SANDBOX_ROOT = Path(
    os.getenv("PARADISE_DATA_DIR",
              os.getenv("AIPET_DATA_DIR",
                        str(Path(__file__).resolve().parent.parent.parent / "data")))
)

# Files that tools must NEVER read (contain secrets or config)
BLOCKED_FILES = {"config.json", "settings.json", "user.json"}

# Max output length
MAX_OUTPUT = 4000


def safe_path(path_str: str) -> Path:
    """Resolve path and ensure it's under the sandbox root."""
    p = (SANDBOX_ROOT / path_str).resolve()
    if not str(p).startswith(str(SANDBOX_ROOT.resolve())):
        raise ValueError(f"Path traversal blocked: {path_str}")
    return p


def is_blocked(path_str: str) -> bool:
    """Check if a file path is in the blocked list."""
    return Path(path_str).name in BLOCKED_FILES


def truncate(text: str, max_len: int = MAX_OUTPUT) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... (truncated, {len(text)} total chars)"


def is_write_command(command: str) -> bool:
    """Check if a bash command attempts file write operations."""
    blocked = [
        "rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:", "> /dev/sda",
        "> ", ">> ", "tee ", "sed -i", "awk.*-i", "perl -i",
        "cp ", "mv ", "install ",
        "touch ", "mkdir ", "ln ",
        "dd of=", "truncate",
    ]
    cmd_lower = command.lower()
    return any(b in cmd_lower for b in blocked)


def contains_blocked_file(command: str) -> str | None:
    """Check if command references a blocked file. Returns filename or None."""
    for f in BLOCKED_FILES:
        if f in command:
            return f
    return None
