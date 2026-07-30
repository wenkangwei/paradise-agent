"""Workspace — per-agent file system management.

Each agent gets a dedicated directory on disk:
  data/agents/{agent_id}/
    config.json         — agent configuration
    soul.md, memory.md, heartbeat.md, agents.md
    state.json          — emotion state persistence
    memory_facts.json   — frozen snapshot long-term memory
    sessions/           — time-partitioned session JSONL files
      {session_id}/
        topic_index.json
        2025-01-15.jsonl
        2025-01-16.jsonl
    reflections/        — reflection records
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent  # src/backend/
DATA_DIR = os.getenv("PARADISE_DATA_DIR",
                     os.getenv("AIPET_DATA_DIR", str(_BACKEND_DIR / "data")))


class Workspace:
    """Manages an agent's on-disk workspace using direct file I/O."""

    def __init__(self, agent_id: str, data_dir: str | None = None):
        self.agent_id = agent_id
        base = data_dir or os.getenv("PARADISE_DATA_DIR",
                                      os.getenv("AIPET_DATA_DIR", DATA_DIR))
        self.root = Path(base) / "agents" / agent_id

    # ── Initialization ──────────────────────────────────────────────

    def initialize(self, soul_md: str = "", memory_md: str = "",
                   heartbeat_md: str = "", agents_md: str = "") -> None:
        """Create workspace directory and initial files."""
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sessions").mkdir(exist_ok=True)
        (self.root / "reflections").mkdir(exist_ok=True)

        for name, content in [("soul", soul_md), ("memory", memory_md),
                              ("heartbeat", heartbeat_md), ("agents", agents_md)]:
            path = self.root / f"{name}.md"
            if not path.exists():
                path.write_text(content, encoding="utf-8")

        state_path = self.root / "state.json"
        if not state_path.exists():
            self.save_state({
                "mood": "happy", "energy": 1.0, "hunger": 0.0,
                "affection": 0.5, "boredom": 0.0,
                "last_interaction": datetime.now().isoformat(),
                "tick_count": 0,
            })

    # ── Markdown Files ──────────────────────────────────────────────

    def read_md(self, name: str) -> str:
        """Read a markdown file (soul, memory, heartbeat, agents)."""
        path = self.root / f"{name}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def write_md(self, name: str, content: str) -> None:
        """Write a markdown file."""
        path = self.root / f"{name}.md"
        path.write_text(content, encoding="utf-8")

    # ── Memory Facts ────────────────────────────────────────────────

    def read_memory_facts(self) -> list[dict]:
        """Read memory_facts.json."""
        path = self.root / "memory_facts.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return []

    def write_memory_facts(self, facts: list[dict]) -> None:
        """Write memory_facts.json."""
        path = self.root / "memory_facts.json"
        path.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── State ───────────────────────────────────────────────────────

    def load_state(self) -> dict[str, Any]:
        """Load agent state from state.json."""
        path = self.root / "state.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {
            "mood": "happy", "energy": 1.0, "hunger": 0.0,
            "affection": 0.5, "boredom": 0.0,
            "last_interaction": datetime.now().isoformat(),
            "tick_count": 0,
        }

    def save_state(self, state: dict[str, Any]) -> None:
        """Persist agent state to state.json."""
        path = self.root / "state.json"
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── Sessions (time-partitioned) ─────────────────────────────────

    def append_session(self, session_id: str, entry: dict) -> None:
        """Append an entry to a session JSONL file."""
        if "created_at" not in entry:
            entry["created_at"] = datetime.now().isoformat(timespec="seconds")

        ts = entry.get("created_at", "")
        date_str = ts[:10] if ts else date.today().isoformat()
        path = self.root / "sessions" / session_id / f"{date_str}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_session(self, session_id: str, last_n: int = 20) -> list[dict]:
        """Read recent entries from a session across daily JSONL files."""
        session_dir = self.root / "sessions" / session_id
        if not session_dir.exists():
            return []

        entries = []
        for jsonl_file in sorted(session_dir.glob("*.jsonl")):
            try:
                for line in jsonl_file.read_text(encoding="utf-8").strip().splitlines():
                    if line.strip():
                        entries.append(json.loads(line))
            except (json.JSONDecodeError, OSError):
                continue
        return entries[-last_n:]

    def list_sessions(self) -> list[str]:
        """List all session IDs for this agent."""
        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return []
        return sorted(d.name for d in sessions_dir.iterdir() if d.is_dir())

    def read_recent_all_sessions(self, last_n: int = 50) -> str:
        """Read recent messages from all sessions, formatted."""
        parts = []
        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return ""

        for session_dir in sorted(sessions_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            for jsonl_file in sorted(session_dir.glob("*.jsonl")):
                try:
                    lines = jsonl_file.read_text("utf-8").strip().splitlines()
                    for line in lines[-5:]:
                        entry = json.loads(line)
                        role = entry.get("role", "?")
                        content = entry.get("content", "")[:150]
                        parts.append(f"[{session_dir.name}] {role}: {content}")
                except Exception:
                    continue

        if len(parts) > last_n:
            parts = parts[-last_n:]
        return "\n".join(parts)

    # ── Reflections ─────────────────────────────────────────────────

    def append_reflection(self, reflection: dict) -> None:
        """Append a reflection entry."""
        path = self.root / "reflections" / "log.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(reflection, ensure_ascii=False) + "\n")

    # ── Cleanup ─────────────────────────────────────────────────────

    def destroy(self) -> None:
        """Remove entire workspace directory."""
        if self.root.exists():
            shutil.rmtree(self.root)
