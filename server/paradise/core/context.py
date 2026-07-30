"""LoopContext — dataclass for a single loop execution."""

from __future__ import annotations

from dataclasses import dataclass, field

from paradise.core.channel import Channel


@dataclass
class LoopContext:
    """Context for a single loop execution."""
    agent_id: str
    agent_name: str
    session_id: str
    user_message: str
    # Channel context
    channel: Channel | None = None
    # Direct prompt inputs (used when no channel)
    soul_md: str = ""
    memory_md: str = ""
    mood_modifier: str = ""
    # User info
    user_name: str = ""
    user_personality: str = ""
    user_description: str = ""
    # Tools
    enable_tools: bool = True
