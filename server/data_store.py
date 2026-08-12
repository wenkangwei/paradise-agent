"""Data Store — unified collection pipeline for session dialogues, behavior events, and user profiles.

Directory structure:
  logs/{user_id}/sessions/{session_id}/{sub_session_id}.jsonl
  logs/{user_id}/feedback/{session_id}/{YYYYMMDD}/behavior_{YYYYMMDDHHMM}.jsonl
  logs/{user_id}/profiles/{user_id}.json

All three accept data via the unified /api/data/upload endpoint (see api/routes/data_upload.py).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("data_store")


def _safe_name(s: str) -> str:
    """Sanitize a string for use as a file/directory name."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)[:128]


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class SessionStore:
    """Stores incremental session dialogues keyed by session_id + sub_session_id.

    A sub_session_id = f"{session_id}_{enter_timestamp_ms}" — each app foreground/visit
    creates a new sub-session file. The same conversation visited multiple times
    accumulates multiple sub-session files under the same session_id directory.
    """

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        self._lock = threading.Lock()

    def append(
        self,
        user_id: str,
        session_id: str,
        sub_session_id: str,
        upload_id: str,
        enter_timestamp: int,
        exit_timestamp: int,
        exit_reason: str,
        incremental_messages: list[dict],
        context: dict | None = None,
        client_info: dict | None = None,
    ) -> dict:
        """Append messages to disk.

        Path: {user_id}/sessions/{session_id}/{sub_session_id}/{upload_id}.jsonl
        Each message = one JSONL line.
        """
        safe_user = _safe_name(user_id)
        safe_session = _safe_name(session_id)
        safe_sub = _safe_name(sub_session_id)
        safe_upload = _safe_name(upload_id)
        path = self._log_dir / safe_user / "sessions" / safe_session / safe_sub / f"{safe_upload}.jsonl"
        server_ts = datetime.now(timezone.utc).isoformat()

        with self._lock:
            for msg in incremental_messages:
                line = {
                    "session_id": session_id,
                    "sub_session_id": sub_session_id,
                    "message_id": msg.get("message_id", ""),
                    "role": msg.get("role", ""),
                    "content": msg.get("content", ""),
                    "model": msg.get("model", ""),
                    "status": msg.get("status", ""),
                    "timestamp": msg.get("timestamp", 0),
                    "session_span": {
                        "enter_timestamp": enter_timestamp,
                        "exit_timestamp": exit_timestamp,
                        "exit_reason": exit_reason,
                    },
                    "context": context or {},
                    "client_info": client_info or {},
                    "server_timestamp": server_ts,
                }
                _append_jsonl(path, line)

        logger.info(
            "Session stored: session=%s sub=%s messages=%d → %s",
            session_id, sub_session_id, len(incremental_messages), path,
        )
        return {"stored_messages": len(incremental_messages), "path": str(path)}

    def read_session(self, user_id: str, session_id: str) -> list[dict]:
        """Read all messages for a given user+session across all sub-sessions."""
        safe_user = _safe_name(user_id)
        safe_session = _safe_name(session_id)
        session_dir = self._log_dir / safe_user / "sessions" / safe_session
        if not session_dir.exists() or not session_dir.is_dir():
            return []
        results = []
        for f in sorted(session_dir.glob("*.jsonl")):
            for line in f.read_text("utf-8").strip().splitlines():
                try:
                    results.append(json.loads(line))
                except Exception:
                    continue
        return results


class BehaviorStore:
    """Stores behavior events (like/dislike/share/retry/TTS).

    Path: logs/{user_id}/feedback/{session_id}/{YYYYMMDD}/behavior_{YYYYMMDDHHMM}.jsonl
    """

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        self._lock = threading.Lock()

    def append_batch(
        self, user_id: str, session_id: str,
        events: list[dict], context: dict | None = None, client_info: dict | None = None
    ) -> dict:
        """Append a batch of behavior events."""
        now = datetime.now(timezone.utc)
        safe_user = _safe_name(user_id)
        safe_session = _safe_name(session_id)
        path = (
            self._log_dir / safe_user / "feedback" / safe_session
            / now.strftime("%Y%m%d") / f"behavior_{now.strftime('%Y%m%d%H%M')}.jsonl"
        )
        stored = 0
        with self._lock:
            for evt in events:
                entry = {
                    **evt,
                    "context": context or {},
                    "client_info": client_info or {},
                    "server_timestamp": datetime.now(timezone.utc).isoformat(),
                }
                _append_jsonl(path, entry)
                stored += 1

        logger.info("Behavior events stored: %d events → %s", stored, path)
        return {"stored_events": stored}

    def read_month(self, year_month: str | None = None) -> list[dict]:
        """Read all behavior events for a given month (YYYY-MM), scanning all users/sessions."""
        month = year_month or datetime.now(timezone.utc).strftime("%Y-%m")
        month_prefix = month.replace("-", "")  # "2026-08" → "202608"
        base = self._log_dir
        if not base.exists():
            return []
        results = []
        # Scan: {user}/feedback/{session}/{YYYYMMDD}/behavior_*.jsonl
        for date_dir in base.rglob(f"feedback/*/{month_prefix}*"):
            if date_dir.is_dir():
                for f in sorted(date_dir.glob("behavior_*.jsonl")):
                    for line in f.read_text("utf-8").strip().splitlines():
                        try:
                            results.append(json.loads(line))
                        except Exception:
                            continue
        return results

    def latest_reactions_by_message(self, year_month: str | None = None) -> dict[str, dict]:
        """Return {message_id: latest_reaction_event} for the given month.

        Used by training joiner to label training entries.
        """
        events = self.read_month(year_month)
        latest: dict[str, dict] = {}
        for evt in events:
            if evt.get("event_type") != "reaction":
                continue
            msg_id = evt.get("message_id", "")
            ts = evt.get("client_timestamp", 0)
            if msg_id not in latest or ts > latest[msg_id].get("client_timestamp", 0):
                latest[msg_id] = evt
        return latest


class ProfileStore:
    """Stores user profiles keyed by user_id as JSON files.

    Supports incremental updates via `updated_fields` — only specified fields
    are overwritten, others are preserved.
    """

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        self._lock = threading.Lock()

    def update(
        self,
        user_id: str,
        profile: dict,
        updated_fields: list[str] | None = None,
        context: dict | None = None,
        client_info: dict | None = None,
    ) -> dict:
        """Create or incrementally update a user profile.

        Args:
            user_id: Unique user identifier
            profile: Full or partial profile dict
            updated_fields: If specified, only these top-level keys are overwritten.
                            If None, the entire profile is replaced.
        """
        safe_user = _safe_name(user_id)
        path = self._log_dir / safe_user / "profiles" / f"{safe_user}.json"

        with self._lock:
            existing = {}
            if path.exists():
                try:
                    existing = json.loads(path.read_text("utf-8"))
                except Exception:
                    logger.warning("Corrupt profile file, overwriting: %s", path)

            if updated_fields:
                # Incremental update — only touch specified top-level keys
                for field in updated_fields:
                    if field in profile:
                        existing[field] = profile[field]
                existing["last_updated_fields"] = updated_fields
            else:
                # Full replace
                existing = {**profile, "last_updated_fields": None}

            existing["user_id"] = user_id
            existing["server_updated_at"] = datetime.now(timezone.utc).isoformat()
            if context:
                existing["_last_context"] = context
            if client_info:
                existing["_last_client_info"] = client_info

            _write_json(path, existing)

        logger.info(
            "Profile stored: user=%s fields=%s",
            user_id, updated_fields or "(full)",
        )
        return {"stored": True, "fields_updated": updated_fields}


class TrainingJoiner:
    """Joins session dialogues, behavior events, and profiles into labeled training data.

    Called offline via scripts/enrich_training.py — not used at upload time.
    """

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        self._sessions = SessionStore(log_dir)
        self._behaviors = BehaviorStore(log_dir)
        self._profiles = ProfileStore(log_dir)

    def build_training_dataset(self, year_month: str | None = None, output_path: Path | None = None) -> dict:
        """Join all data sources and produce a labeled training dataset.

        Output format per entry:
          {
            "session_id": "...",
            "sub_session_id": "...",
            "user_id": "...",
            "user_profile": {...},
            "messages": [...],
            "labels": {"msg_id": "chosen"|"rejected"|null},
            "context": {...}
          }
        """
        month = year_month or datetime.now(timezone.utc).strftime("%Y-%m")
        output_path = output_path or (self._log_dir / "training" / f"enriched_{month}.jsonl")

        reactions = self._behaviors.latest_reactions_by_message(month)

        # Walk: logs/{user_id}/sessions/{session_id}/{sub_session_id}.jsonl
        results: list[dict] = []
        sessions_base = self._log_dir
        if sessions_base.exists():
            for user_dir in sorted(sessions_base.iterdir()):
                if not user_dir.is_dir():
                    continue
                user_id = user_dir.name
                user_sessions = user_dir / "sessions"
                if not user_sessions.exists() or not user_sessions.is_dir():
                    continue
                for session_dir in sorted(user_sessions.iterdir()):
                    if not session_dir.is_dir():
                        continue
                    session_id = session_dir.name
                    messages = self._sessions.read_session(user_id, session_id)
                    if not messages:
                        continue

                # Label messages by reaction
                labels: dict[str, str | None] = {}
                for msg in messages:
                    msg_id = msg.get("message_id", "")
                    if msg_id in reactions:
                        reaction_data = reactions[msg_id].get("event_data", {})
                        reaction = reaction_data.get("reaction")
                        if reaction == "like":
                            labels[msg_id] = "chosen"
                        elif reaction == "dislike":
                            labels[msg_id] = "rejected"

                # Load user profile
                user_profile = {}
                if user_id:
                    profile_path = self._log_dir / _safe_name(user_id) / "profiles" / f"{_safe_name(user_id)}.json"
                    if profile_path.exists():
                        try:
                            user_profile = json.loads(profile_path.read_text("utf-8"))
                        except Exception:
                            pass

                last_ctx = messages[-1].get("context", {}) if messages else {}

                results.append({
                    "session_id": session_id,
                    "user_id": user_id,
                    "user_profile": user_profile,
                    "messages": messages,
                    "labels": labels,
                    "context": last_ctx,
                    "month": month,
                })

        # Write enriched dataset
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for entry in results:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        total_labeled = sum(1 for r in results for _ in r["labels"])
        logger.info(
            "Training dataset built: %d sessions, %d labeled messages → %s",
            len(results), total_labeled, output_path,
        )
        return {
            "sessions": len(results),
            "labeled_messages": total_labeled,
            "output_path": str(output_path),
        }


# ── Singleton instances ────────────────────────────────────────────────

_log_dir = Path(os.getenv("LOG_DIR", "logs")).resolve()

session_store = SessionStore(_log_dir)
behavior_store = BehaviorStore(_log_dir)
profile_store = ProfileStore(_log_dir)
training_joiner = TrainingJoiner(_log_dir)
