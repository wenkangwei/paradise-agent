"""
Data Loader — reads session dialogues and behaviour events from disk,
joins them into a unified labelled dataset.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

# Config passed via function parameters

logger = logging.getLogger("training_data.loader")


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, returning a list of dicts. Silent on missing file."""
    if not path.exists():
        return []
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed line in %s", path)
    return results


def _scan_users(log_dir: Path) -> list[Path]:
    """Return list of user directories under log_dir."""
    if not log_dir.exists():
        return []
    return [d for d in sorted(log_dir.iterdir()) if d.is_dir() and d.name.startswith("u_")]


def load_sessions(log_dir: Path, min_messages: int = 2) -> dict[str, list[dict]]:
    """Load all session dialogues, keyed by session_id.

    Returns:
        {session_id: [message_dict, ...]} — messages sorted by timestamp.
    """
    sessions: dict[str, list[dict]] = {}
    for user_dir in _scan_users(log_dir):
        sessions_dir = user_dir / "sessions"
        if not sessions_dir.exists():
            continue
        for session_dir in sorted(sessions_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            msgs: list[dict] = []
            for sub_dir in sorted(session_dir.iterdir()):
                if not sub_dir.is_dir():
                    continue
                for upload_file in sorted(sub_dir.glob("*.jsonl")):
                    msgs.extend(_read_jsonl(upload_file))
            if len(msgs) >= min_messages:
                msgs.sort(key=lambda m: m.get("timestamp", 0))
                sessions[sid] = msgs
    logger.info("Loaded %d sessions from %s", len(sessions), log_dir)
    return sessions


def load_behaviours(log_dir: Path) -> dict[str, dict[str, str]]:
    """Load behaviour events, returning {message_id: behaviour_meta}.

    Tracks BOTH reaction events (like/dislike) and retry events. The retry
    event marks an assistant message as having been "regenerated" by the user
    — treated as a weak rejection signal (score=1) by the DPO ranker.

    Returns:
        {message_id: {
            "reaction": "like"|"dislike"|None,
            "retry_count": int,
            "session_id": str,
            "_ts": float,  # latest event timestamp
        }}
    """
    behaviours: dict[str, dict] = {}
    for user_dir in _scan_users(log_dir):
        feedback_dir = user_dir / "feedback"
        if not feedback_dir.exists():
            continue
        for session_dir in sorted(feedback_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            for date_dir in sorted(session_dir.iterdir()):
                if not date_dir.is_dir():
                    continue
                for bfile in sorted(date_dir.glob("behavior_*.jsonl")):
                    for evt in _read_jsonl(bfile):
                        evt_type = evt.get("event_type")
                        msg_id = evt.get("message_id", "")
                        ts = evt.get("client_timestamp", 0)
                        if not msg_id:
                            continue
                        slot = behaviours.setdefault(msg_id, {
                            "reaction": None,
                            "retry_count": 0,
                            "session_id": evt.get("session_id", ""),
                            "_ts": 0,
                        })
                        if ts > slot["_ts"]:
                            slot["_ts"] = ts
                            slot["session_id"] = evt.get("session_id", slot.get("session_id", ""))
                        if evt_type == "reaction":
                            reaction = evt.get("event_data", {}).get("reaction", "")
                            if reaction in ("like", "dislike"):
                                slot["reaction"] = reaction
                        elif evt_type == "retry":
                            slot["retry_count"] = slot.get("retry_count", 0) + 1
    logger.info(
        "Loaded %d message behaviour records from %s "
        "(reactions: %d, retries: %d)",
        len(behaviours), log_dir,
        sum(1 for v in behaviours.values() if v.get("reaction")),
        sum(1 for v in behaviours.values() if v.get("retry_count", 0) > 0),
    )
    return behaviours


def join(sessions: dict[str, list[dict]], behaviours: dict[str, dict]) -> list[dict]:
    """Join session messages with behaviour labels.

    Returns list of labelled conversation turns:
        [{
            "session_id": str,
            "messages": [{"role": ..., "content": ..., "message_id": ...}, ...],
            "labels": {message_id: "like"|"dislike"|"retry"|"unmarked", ...},
            "meta":   {message_id: {retry_count, reaction, ...}, ...},
        }]

    Label semantics (mutually exclusive — highest-priority signal wins):
        "dislike"  — user clicked 👎
        "like"     — user clicked 👍
        "retry"    — message was regenerated (retry_count>0) AND no reaction
        "unmarked" — neither reaction nor retry
    """
    results = []
    for sid, msgs in sessions.items():
        labels: dict[str, str] = {}
        meta: dict[str, dict] = {}
        for msg in msgs:
            mid = msg.get("message_id", "")
            if not mid:
                continue
            beh = behaviours.get(mid, {})
            reaction = beh.get("reaction")
            retry_count = beh.get("retry_count", 0)
            meta[mid] = {"reaction": reaction, "retry_count": retry_count}
            if reaction == "like":
                labels[mid] = "like"
            elif reaction == "dislike":
                labels[mid] = "dislike"
            elif retry_count and retry_count > 0:
                labels[mid] = "retry"
            else:
                labels[mid] = "unmarked"
        results.append({
            "session_id": sid,
            "messages": [
                {
                    "role": m.get("role", "").lower(),
                    "content": m.get("content", ""),
                    "message_id": m.get("message_id", ""),
                }
                for m in msgs
            ],
            "labels": labels,
            "meta": meta,
        })
    logger.info("Joined: %d sessions, label distribution: like=%d dislike=%d retry=%d unmarked=%d",
                len(results),
                sum(1 for r in results for v in r["labels"].values() if v == "like"),
                sum(1 for r in results for v in r["labels"].values() if v == "dislike"),
                sum(1 for r in results for v in r["labels"].values() if v == "retry"),
                sum(1 for r in results for v in r["labels"].values() if v == "unmarked"))
    return results
