"""ReflectionStorage — persists reflection results to JSONL."""

from __future__ import annotations

import json
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict


@dataclass
class ReflectionEntry:
    type: str  # "post_turn" | "idle" | "scheduled"
    timestamp: float = field(default_factory=time.time)
    facts: list[str] = field(default_factory=list)
    memory_updates: str = ""
    emotion_delta: dict = field(default_factory=dict)
    insights: str = ""
    next_actions: str = ""
    raw_response: str = ""


class ReflectionStorage:
    """Manages reflection log persistence."""

    def __init__(self, reflections_dir: Path):
        self._log_path = reflections_dir / "log.jsonl"
        reflections_dir.mkdir(parents=True, exist_ok=True)

    def append(self, entry: ReflectionEntry) -> None:
        """Append a reflection entry to the log."""
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def read_recent(self, days: int = 7, entry_type: str | None = None) -> list[ReflectionEntry]:
        """Read recent reflections."""
        if not self._log_path.exists():
            return []

        cutoff = time.time() - days * 86400
        entries = []
        try:
            for line in self._log_path.read_text("utf-8").strip().splitlines():
                try:
                    d = json.loads(line)
                    if d.get("timestamp", 0) >= cutoff:
                        if entry_type and d.get("type") != entry_type:
                            continue
                        entries.append(ReflectionEntry(
                            type=d.get("type", ""),
                            timestamp=d.get("timestamp", 0),
                            facts=d.get("facts", []),
                            memory_updates=d.get("memory_updates", ""),
                            emotion_delta=d.get("emotion_delta", {}),
                            insights=d.get("insights", ""),
                            next_actions=d.get("next_actions", ""),
                            raw_response=d.get("raw_response", ""),
                        ))
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            pass
        return entries

    def read_as_text(self, days: int = 7, max_entries: int = 20) -> str:
        """Read recent reflections as formatted text."""
        entries = self.read_recent(days=days)
        if not entries:
            return ""
        lines = []
        for e in entries[-max_entries:]:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.timestamp))
            lines.append(f"[{ts}] ({e.type}) {e.insights or e.memory_updates or e.raw_response[:200]}")
        return "\n".join(lines)
