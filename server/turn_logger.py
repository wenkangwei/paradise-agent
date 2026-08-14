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

        Returns None if the turn is too short to be useful.

        Supports both plain chat turns and tool-call turns. Tool-call turns
        produce an ATIF-compatible multi-message structure:
            user → assistant(tool_calls) → tool → assistant(final_answer)
        plus a `trajectory` field for downstream RL training.

        Schema: aichat-1.0 (ATIF-compatible; full ATIF migration is W2).
        """
        if len(self._final_content.strip()) < 10:
            return None

        user_text = self._extract_user_text()
        thinking = next(
            (evt.get("content", "") for evt in self._output_events
             if evt.get("type") == "thinking"),
            "",
        )

        messages, trajectory, tools_used = self._build_messages(user_text, thinking)
        if len(messages) < 2:
            return None

        entry: dict[str, Any] = {
            "conversation_id": self._conv_id,
            "turn": self._turn,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": self._input_events[0].get("model", "") if self._input_events else "",
            "schema_version": "aichat-1.0",
            "messages": messages,
            "metadata": {
                "has_attachments": bool(self._input_events[0].get("attachments")) if self._input_events else False,
                "tools_used": tools_used,
                "tool_calls_count": self._tool_calls_count,
                "total_tokens": self._token_estimate,
                "duration_ms": round((time.time() - self._start_ts) * 1000, 1),
                "environment": os.getenv("APP_ENV", "dev"),
            },
        }
        if trajectory:
            entry["trajectory"] = trajectory
        return entry

    def _extract_user_text(self) -> str:
        """Pull the user's text from the first user_message input event."""
        for evt in self._input_events:
            if evt.get("type") != "user_message":
                continue
            cp = evt.get("content_preview", "")
            if isinstance(cp, str):
                return cp
            if isinstance(cp, list):
                parts = [
                    item.get("text", "")
                    for item in cp
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                return " ".join(parts)
            break
        return ""

    def _build_messages(
        self, user_text: str, thinking: str
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Reconstruct OpenAI/ATIF-compatible messages list.

        For tool-call turns, splits content around tool_calls:
          - assistant(tool_calls) carrying pre-tool reasoning (if any)
          - one role=tool message per tool_call
          - final assistant message with post-tool answer

        Returns (messages, trajectory, tools_used).
        """
        messages: list[dict] = [{"role": "user", "content": user_text}]
        trajectory: list[dict] = []
        tools_used: list[str] = []

        if self._tool_calls_count == 0:
            # Plain chat turn — single assistant message (legacy behaviour)
            asst_msg: dict[str, Any] = {"role": "assistant", "content": self._final_content}
            if thinking:
                asst_msg["thinking"] = thinking[:1000]
            messages.append(asst_msg)
            return messages, trajectory, tools_used

        # Tool-call turn — walk output events in order, split content around tool_calls.
        pre_tool_text = ""
        post_tool_text = ""
        seen_tool = False
        for evt in self._output_events:
            t = evt.get("type")
            if t == "tool_call":
                seen_tool = True
            elif t == "content_delta":
                if seen_tool:
                    post_tool_text += evt.get("text", "")
                else:
                    pre_tool_text += evt.get("text", "")

        # Assistant message with tool_calls (carries pre-tool reasoning, if any).
        asst_msg: dict[str, Any] = {"role": "assistant", "content": pre_tool_text}
        if thinking:
            asst_msg["thinking"] = thinking[:1000]

        tool_calls_payload: list[dict] = []
        tool_results: list[str] = []
        tc_counter = 0
        for evt in self._output_events:
            if evt.get("type") != "tool_call":
                continue
            tc_id = f"call_{self._turn}_{tc_counter}"
            tc_counter += 1
            name = evt.get("name", "")
            args = evt.get("arguments", {}) or {}
            result = evt.get("result", "")
            duration = evt.get("duration_ms", 0)
            tools_used.append(name)
            tool_calls_payload.append({
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })
            tool_results.append(result)
            trajectory.append({
                "action": "tool_call",
                "name": name,
                "arguments": args,
                "result": result,
                "duration_ms": duration,
            })

        asst_msg["tool_calls"] = tool_calls_payload
        messages.append(asst_msg)

        # Tool result messages (role=tool, one per call, order matched by id).
        for i, tc in enumerate(tool_calls_payload):
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_results[i],
            })

        # Final assistant message with post-tool answer (if any).
        if post_tool_text.strip():
            messages.append({"role": "assistant", "content": post_tool_text})

        return messages, trajectory, tools_used


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
