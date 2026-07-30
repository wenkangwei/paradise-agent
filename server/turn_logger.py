"""Turn Logger — saves agent conversation rounds to disk as JSONL.

Generates two outputs per turn:
  1. logs/<date>/conv_<id>/turn_NNN_input.jsonl  — raw user input + intent
  2. logs/<date>/conv_<id>/turn_NNN_output.jsonl — agent output stream

And one cleaned training dataset:
  3. logs/training/dataset_YYYY-MM.jsonl — SFT/ORPO-ready format
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TurnEvent:
    """One event in a conversation turn."""
    type: str
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)


class TurnRecorder:
    """Records events for a single conversation turn in memory, then flushes to disk."""

    def __init__(self, log_dir: Path, conversation_id: str, turn: int):
        self._log_dir = log_dir
        self._conv_id = conversation_id
        self._turn = turn
        self._input_events: list[dict] = []
        self._output_events: list[dict] = []
        self._start_ts = time.time()
        self._lock = threading.Lock()
        self._final_content = ""
        self._tool_calls_count = 0
        self._token_estimate = 0

    # ── input events ────────────────────────────────────────────

    def turn_start(self, model: str) -> None:
        self._append_input("turn_start", {
            "turn": self._turn,
            "conversation_id": self._conv_id,
            "model": model,
        })

    def user_message(self, content: str | list, attachments_meta: list[dict] | None = None) -> None:
        self._append_input("user_message", {
            "content_preview": _truncate_content(content, 500),
            "attachments": attachments_meta or [],
            "raw_content": content if isinstance(content, list) else None,
        })

    def intent(self, needs_tools: bool, should_think: bool) -> None:
        self._append_input("intent", {
            "needs_tools": needs_tools,
            "should_think": should_think,
        })

    # ── output events ───────────────────────────────────────────

    def phase_start(self, name: str) -> None:
        self._append_output("phase_start", {"phase": name})

    def phase_end(self, name: str) -> None:
        self._append_output("phase_end", {"phase": name})

    def thinking(self, text: str) -> None:
        self._append_output("thinking", {"content": text[:2000]})

    def tool_call(self, name: str, arguments: dict, result: str, duration_ms: float = 0) -> None:
        self._tool_calls_count += 1
        self._append_output("tool_call", {
            "name": name,
            "arguments": arguments,
            "result": result[:3000],
            "duration_ms": round(duration_ms, 1),
        })

    def content_delta(self, text: str) -> None:
        self._token_estimate += 1
        self._final_content += text
        self._append_output("content_delta", {"text": text})

    def error(self, message: str) -> None:
        self._append_output("error", {"message": message})

    def turn_end(self, finish_reason: str = "stop") -> None:
        duration_ms = (time.time() - self._start_ts) * 1000
        self._append_output("turn_end", {
            "final_content": self._final_content[:5000],
            "token_estimate": self._token_estimate,
            "duration_ms": round(duration_ms, 1),
            "tool_calls_count": self._tool_calls_count,
            "finish_reason": finish_reason,
        })

    # ── flush ───────────────────────────────────────────────────

    def flush(self) -> None:
        """Write input and output JSONL files to disk."""
        with self._lock:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            conv_dir = self._log_dir / date_str / f"conv_{_safe_dir_name(self._conv_id)}"
            conv_dir.mkdir(parents=True, exist_ok=True)

            input_path = conv_dir / f"turn_{self._turn:03d}_input.jsonl"
            output_path = conv_dir / f"turn_{self._turn:03d}_output.jsonl"

            _write_jsonl(input_path, self._input_events)
            _write_jsonl(output_path, self._output_events)

            logger.info(
                "Turn %d saved: input=%d events output=%d events (%s)",
                self._turn,
                len(self._input_events),
                len(self._output_events),
                input_path.parent,
            )

    # ── helpers ──────────────────────────────────────────────────

    def _append_input(self, typ: str, data: dict) -> None:
        self._input_events.append({
            "type": typ,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        })

    def _append_output(self, typ: str, data: dict) -> None:
        self._output_events.append({
            "type": typ,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        })

    # ── training data export ────────────────────────────────────

    def to_training_entry(self) -> dict | None:
        """Convert this turn into a cleaned SFT/ORPO training entry.

        Returns None if the turn is not suitable for training
        (e.g. error, too short, tool-call-only).
        """
        if self._tool_calls_count > 0:
            return None  # Skip tool-call turns for now (future: tool-use SFT)
        if len(self._final_content.strip()) < 10:
            return None  # Too short to be useful

        # Extract user message text
        user_text = ""
        for evt in self._input_events:
            if evt.get("type") == "user_message":
                cp = evt.get("content_preview", "")
                if isinstance(cp, str):
                    user_text = cp
                elif isinstance(cp, list):
                    # Extract text parts only
                    parts = []
                    for item in cp:
                        if isinstance(item, dict) and item.get("type") == "text":
                            parts.append(item.get("text", ""))
                    user_text = " ".join(parts)
                break

        # Extract thinking
        thinking = ""
        for evt in self._output_events:
            if evt.get("type") == "thinking":
                thinking = evt.get("content", "")

        messages = [
            {"role": "user", "content": user_text},
        ]
        if thinking:
            messages.append({"role": "assistant", "content": self._final_content, "thinking": thinking[:1000]})
        else:
            messages.append({"role": "assistant", "content": self._final_content})

        return {
            "conversation_id": self._conv_id,
            "turn": self._turn,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": self._input_events[0].get("model", "") if self._input_events else "",
            "messages": messages,
            "metadata": {
                "has_attachments": bool(self._input_events[0].get("attachments")) if self._input_events else False,
                "tools_used": [],
                "total_tokens": self._token_estimate,
                "duration_ms": round((time.time() - self._start_ts) * 1000, 1),
            },
        }


class TrainingExporter:
    """Appends cleaned training entries to a monthly dataset file."""

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir

    def append(self, entry: dict) -> None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        training_dir = self._log_dir / "training"
        training_dir.mkdir(parents=True, exist_ok=True)
        path = training_dir / f"dataset_{month}.jsonl"
        _append_jsonl(path, entry)
        logger.debug("Training entry appended: %s", path)


# ── utilities ────────────────────────────────────────────────────

def _truncate_content(content: str | list, max_len: int) -> str | list:
    if isinstance(content, str):
        return content[:max_len]
    if isinstance(content, list):
        truncated = []
        for item in content:
            if isinstance(item, dict):
                item_copy = dict(item)
                if item_copy.get("type") == "image_url":
                    # Keep image_url metadata, truncate base64 data
                    img = item_copy.get("image_url", {})
                    if isinstance(img, dict):
                        url = img.get("url", "")
                        if isinstance(url, str) and url.startswith("data:"):
                            img["url"] = url[:100] + "...(truncated)"
                    item_copy["image_url"] = img
                truncated.append(item_copy)
            else:
                truncated.append(item)
        return truncated
    return str(content)


def _safe_dir_name(s: str) -> str:
    """Sanitize a string for use as a directory name."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)[:64]


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
