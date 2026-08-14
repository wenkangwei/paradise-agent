"""Built-in tools — search_files, read_file.

Each tool self-registers with the ToolRegistry at import time.

Note: The original ``bash`` tool was replaced in Phase 3-A by
``bash_read`` + ``bash_write`` in ``paradise/tools/bash_tool.py``.
This module no longer registers any bash tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from paradise.tools.sandbox import (
    safe_path, is_blocked, truncate,
)
from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ── Tool schemas ──────────────────────────────────────────────────────

_SEARCH_SCHEMA = {
    "name": "search_files",
    "description": (
        "Search for files by name pattern under the agent data directory. "
        "Returns matching file paths. Use glob patterns like *.json, *.md, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match, e.g. '*.jsonl', '**/*.md'",
            },
            "directory": {
                "type": "string",
                "description": "Subdirectory to search in (relative to agent data root). Default: '.'",
            },
        },
        "required": ["pattern"],
    },
}

_READ_SCHEMA = {
    "name": "read_file",
    "description": (
        "Read the content of a file. Returns file text. "
        "Can read markdown, json, jsonl, text files. "
        "Path is relative to agent data root."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to agent data root, e.g. 'agents/xiaoju/state.json'",
            },
            "lines": {
                "type": "integer",
                "description": "Max number of lines to read (default 50, max 200)",
            },
        },
        "required": ["path"],
    },
}


# ── Handlers ──────────────────────────────────────────────────────────

async def _handle_search(args: dict[str, Any]) -> str:
    pattern = args.get("pattern", "*")
    directory = args.get("directory", ".")
    try:
        search_dir = safe_path(directory)
    except ValueError as e:
        return tool_error(str(e))

    if not search_dir.exists():
        return tool_error(f"directory not found: {directory}")

    matches = sorted(
        str(p.relative_to(SANDBOX_ROOT))
        for p in search_dir.glob(pattern) if p.is_file()
    )
    if not matches:
        return tool_result(f"No files matching '{pattern}' in {directory}")

    result = "\n".join(matches[:50])
    if len(matches) > 50:
        result += f"\n... and {len(matches) - 50} more files"
    return tool_result(result)


async def _handle_read(args: dict[str, Any]) -> str:
    path = args.get("path", "")
    lines = args.get("lines", 50)

    if not path:
        return tool_error("no path specified")
    if is_blocked(path):
        return tool_error(f"{path} is a protected config file")

    lines = min(max(1, lines), 200)
    try:
        file_path = safe_path(path)
    except ValueError as e:
        return tool_error(str(e))

    if not file_path.exists():
        return tool_error(f"file not found: {path}")
    if not file_path.is_file():
        return tool_error(f"not a file: {path}")
    if file_path.stat().st_size > 100_000:
        return tool_error(f"file too large ({file_path.stat().st_size} bytes)")

    try:
        text = await asyncio.to_thread(file_path.read_text, "utf-8")
        all_lines = text.splitlines()
        if len(all_lines) > lines:
            shown = "\n".join(all_lines[:lines])
            return truncate(f"{shown}\n... ({len(all_lines)} total lines, showing first {lines})")
        return truncate(text)
    except Exception as e:
        return tool_error(f"error reading file: {e}")


# ── Self-register with registry ──────────────────────────────────────

registry.register(
    name="search_files",
    toolset="builtin",
    schema=_SEARCH_SCHEMA,
    handler=_handle_search,
    is_async=True,
    description="Search files by glob pattern",
)

registry.register(
    name="read_file",
    toolset="builtin",
    schema=_READ_SCHEMA,
    handler=_handle_read,
    is_async=True,
    description="Read file content",
)


# ── Convenience: get tool definitions for LLM ────────────────────────

def get_builtin_tool_definitions() -> list[dict]:
    """Return OpenAI-format tool definitions for ALL registered tools.

    Tools are auto-discovered at startup via registry.discover_builtin_tools().
    To add a new tool, drop a .py file in paradise/tools/ with a
    registry.register() call — no other changes needed.
    """
    all_names = set(registry.get_all_tool_names())
    if not all_names:
        # Fallback: if discovery hasn't run yet, use known builtins
        all_names = {"search_files", "read_file"}
    return registry.get_definitions(all_names)


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool by name via the registry (supports async tools)."""
    return await registry.dispatch_async(name, arguments)
