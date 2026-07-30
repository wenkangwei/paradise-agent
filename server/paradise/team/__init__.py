"""Paradise team module -- multi-agent coordination."""

from paradise.team.manager import AgentTeam, TeamManager
from paradise.team.delegation import DelegationTool, DelegationResult, DELEGATE_TOOL_SCHEMA

__all__ = [
    "AgentTeam",
    "TeamManager",
    "DelegationTool",
    "DelegationResult",
    "DELEGATE_TOOL_SCHEMA",
]
