"""FrozenMemory — Hermes-style frozen snapshot memory.

Session start: read memory_facts.json → freeze snapshot.
During session: get_* returns frozen data (immutable).
Mutations (add_fact, etc.) write directly to disk.
Changes only visible in next session's snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MemorySnapshot:
    """Immutable snapshot of agent memory, frozen at session start."""
    facts: list[str] = field(default_factory=list)
    user_prefs: dict[str, str] = field(default_factory=dict)
    session_summaries: list[str] = field(default_factory=list)
    frozen_at: str = ""

    def __post_init__(self):
        if not self.frozen_at:
            self.frozen_at = datetime.now().isoformat(timespec="seconds")


class FrozenMemory:
    """Hermes-style frozen snapshot memory for an agent.

    Uses Workspace for direct file I/O (no async file_storage dependency).

    Usage:
        memory = FrozenMemory()
        memory.initialize(agent_id, workspace)
        facts = memory.get_facts()
        memory.add_fact("主人喜欢猫")
    """

    def __init__(self):
        self.agent_id: str = ""
        self._workspace: Any = None
        self._snapshot: MemorySnapshot | None = None

    def initialize(self, agent_id: str, workspace: Any) -> None:
        """Initialize with agent_id and workspace."""
        self.agent_id = agent_id
        self._workspace = workspace
        self.freeze()

    def freeze(self) -> None:
        """Load current state from disk and freeze as snapshot."""
        data = self._read_facts()
        # Workspace.read_memory_facts() returns list or dict
        if isinstance(data, list):
            facts = data
            user_prefs = {}
            session_summaries = []
        elif isinstance(data, dict):
            facts = data.get("facts", [])
            user_prefs = data.get("user_prefs", {})
            session_summaries = data.get("session_summaries", [])
        else:
            facts, user_prefs, session_summaries = [], {}, []
        self._snapshot = MemorySnapshot(
            facts=facts,
            user_prefs=user_prefs,
            session_summaries=session_summaries,
        )
        logger.info("Frozen memory snapshot for %s: %d facts, %d summaries",
                     self.agent_id, len(self._snapshot.facts),
                     len(self._snapshot.session_summaries))

    def _read_facts(self) -> dict:
        """Read memory_facts.json from workspace."""
        if not self._workspace:
            return {}
        return self._workspace.read_memory_facts()

    def _write_facts(self, data: dict) -> None:
        """Write memory_facts.json to workspace."""
        if self._workspace:
            self._workspace.write_memory_facts(data)

    def _ensure_snapshot(self) -> MemorySnapshot:
        if self._snapshot is None:
            self.freeze()
        return self._snapshot  # type: ignore

    # ── Read methods (return frozen snapshot) ─────────────────────

    def get_facts(self) -> list[str]:
        """Return frozen facts (immutable during session)."""
        return list(self._ensure_snapshot().facts)

    def get_user_prefs(self) -> dict[str, str]:
        """Return frozen user preferences (immutable during session)."""
        return dict(self._ensure_snapshot().user_prefs)

    def get_session_summaries(self) -> list[str]:
        """Return frozen session summaries (immutable during session)."""
        return list(self._ensure_snapshot().session_summaries)

    def get_all(self) -> MemorySnapshot:
        """Return the full frozen snapshot."""
        return self._ensure_snapshot()

    # ── Mutation methods (write to disk, visible next session) ─────

    def add_fact(self, fact: str) -> None:
        """Add a fact to memory. Writes to disk immediately."""
        data = self._read_facts()
        facts = data.get("facts", [])
        if fact not in facts:
            facts.append(fact)
        data["facts"] = facts
        self._write_facts(data)

    def remove_fact(self, fact: str) -> None:
        """Remove a fact from memory."""
        data = self._read_facts()
        facts = data.get("facts", [])
        data["facts"] = [f for f in facts if f != fact]
        self._write_facts(data)

    def replace_fact(self, old: str, new: str) -> None:
        """Replace a fact with a new version."""
        data = self._read_facts()
        facts = data.get("facts", [])
        data["facts"] = [new if f == old else f for f in facts]
        self._write_facts(data)

    def update_user_pref(self, key: str, value: str) -> None:
        """Update a user preference."""
        data = self._read_facts()
        prefs = data.get("user_prefs", {})
        prefs[key] = value
        data["user_prefs"] = prefs
        self._write_facts(data)

    def add_session_summary(self, summary: str) -> None:
        """Add a session summary."""
        data = self._read_facts()
        summaries = data.get("session_summaries", [])
        summaries.append(summary)
        # Keep only last 50 summaries
        data["session_summaries"] = summaries[-50:]
        self._write_facts(data)

    def init_memory_facts(self) -> None:
        """Initialize memory_facts.json if it doesn't exist."""
        existing = self._read_facts()
        if not existing:
            self._write_facts({
                "facts": [],
                "user_prefs": {},
                "session_summaries": [],
            })

    def to_prompt_text(self) -> str:
        """Format frozen memory as text for LLM prompt."""
        snapshot = self._ensure_snapshot()
        parts = []
        if snapshot.facts:
            facts_text = "你的长期记忆:\n" + "\n".join(f"- {f}" for f in snapshot.facts)
            parts.append(facts_text)
        if snapshot.user_prefs:
            prefs_text = "关于主人的偏好:\n" + "\n".join(
                f"- {k}: {v}" for k, v in snapshot.user_prefs.items()
            )
            parts.append(prefs_text)
        if snapshot.session_summaries:
            summaries_text = "过去对话摘要:\n" + "\n".join(
                f"- {s}" for s in snapshot.session_summaries[-5:]
            )
            parts.append(summaries_text)
        return "\n\n".join(parts)

    # ── MemoryProvider ABC compatibility ───────────────────────────

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return self.to_prompt_text()

    def prefetch(self, query: str) -> str:
        return self.to_prompt_text()

    def sync_turn(self, user_content: str, assistant_content: str) -> None:
        """Sync turn — no-op for builtin memory."""
        pass

    def get_tool_schemas(self) -> list[dict]:
        return []

    def handle_tool_call(self, tool_name: str, args: dict) -> str:
        return '{"error": "builtin memory has no tools"}'

    def shutdown(self) -> None:
        pass
