"""ATIF (Agent Trace & Instruction Format) exporter.

Phase 2.13 — production training-data export. Default OFF; flipped on
via `AtifConfig.enabled=True` (config.prod.yaml::atif.enabled).

Design (see ARCHITECTURE_V2.md §7):
  * Zero overhead when disabled — `export()` returns before touching disk
    or spawning any task.
  * Fire-and-forget — caller awaits `export()` but the actual disk write
    happens in a background writer task consuming an asyncio.Queue. The
    await is just a `put_nowait`; never blocks on disk I/O.
  * Never raises — any failure (queue full, disk error, bad input) is
    caught and logged. Training data is best-effort, never on the
    critical path of serving user requests.
  * Date-based rotation — one JSONL file per UTC day under `export_path`,
    so a long-running process produces `2026-08-13.jsonl`,
    `2026-08-14.jsonl`, etc. Roll-over happens lazily on next write.
  * PII redaction — when `redact_pii=True`, masks Chinese mobile numbers,
    ID card numbers (18-digit), and bank card numbers (16-19 digit) in
    user-visible text fields before serialisation. Numbers in
    non-text fields (cost, latency) are untouched.

The JSONL schema matches ARCHITECTURE_V2.md §7.2. `cleanup_node` in
`paradise.core.supervisor` is the only intended caller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paradise.config import AtifConfig

logger = logging.getLogger("paradise.atif")

# ── PII regex patterns ──────────────────────────────────────────────
# Order matters: longer/more-specific patterns first so a 19-digit bank
# card isn't partially matched as a mobile number prefix.
#
# We use digit-context lookarounds (?<!\d) ... (?!\d) rather than \b
# because Python 3's re module treats Chinese characters as word chars
# (re.UNICODE is the default), so \b would NOT fire between a Chinese
# char and a digit — leaving "我的手机是13800138000" unmasked.
#
# Mobile (China): 11 digits starting with 1, not adjacent to other digits
# so we don't match the first 11 of a 19-digit bank card.
_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# ID card (China): 18 chars — 17 digits + (digit or X).
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")

# Bank card: 16-19 consecutive digits, not adjacent to other digits.
# Runs after ID card so an 18-digit ID gets fully redacted first.
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")

_REDACTED = "[REDACTED]"


def _redact_text(text: str) -> str:
    """Mask PII patterns in a string. Idempotent on already-masked text."""
    if not isinstance(text, str) or not text:
        return text
    s = _ID_CARD_RE.sub(_REDACTED, text)
    s = _BANK_CARD_RE.sub(_REDACTED, s)
    s = _MOBILE_RE.sub(_REDACTED, s)
    return s


class AtifExporter:
    """Fire-and-forget JSONL writer for ATIF training traces.

    Thread-safety: bound to one event loop. The queue + writer task are
    created lazily on first `export()` call inside the running loop.
    Concurrent callers in the same loop share one queue + writer.

    Attributes:
        enabled: convenience mirror of `self._cfg.enabled` for fast-path
            checks without attribute lookup in the caller.
    """

    def __init__(self, config: AtifConfig):
        self._cfg = config
        self._sink: asyncio.Queue[dict[str, Any]] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._dropped_count: int = 0

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    @property
    def dropped_count(self) -> int:
        """Records dropped due to queue overflow — for metrics scraping."""
        return self._dropped_count

    async def export(self, turn_trace: dict[str, Any]) -> None:
        """Queue a turn trace for asynchronous JSONL write.

        Never raises. Returns immediately when disabled, when the input
        is not a dict, or when the queue is full (records a drop).
        """
        if not self._cfg.enabled:
            return
        if not isinstance(turn_trace, dict):
            logger.warning("ATIF export ignored non-dict trace: %r", type(turn_trace))
            return

        try:
            record = self._format(turn_trace)
        except Exception:
            logger.exception("ATIF _format failed — record dropped")
            return

        # Lazy-init queue + writer on first call inside the running loop.
        # create_task/Queue both require a running event loop.
        if self._sink is None:
            try:
                self._sink = asyncio.Queue(maxsize=1000)
                self._writer_task = asyncio.create_task(
                    self._writer_loop(),
                    name="atif-writer",
                )
            except RuntimeError:
                # No running loop — fall back to synchronous write so
                # callers running outside asyncio still get their record.
                self._sink = None
                self._write_record_sync(record)
                return
            except Exception:
                logger.exception("ATIF queue/writer init failed — record dropped")
                self._sink = None
                return

        try:
            self._sink.put_nowait(record)
        except asyncio.QueueFull:
            self._dropped_count += 1
            logger.warning(
                "ATIF queue full (size=1000) — record dropped (total dropped=%d)",
                self._dropped_count,
            )
        except Exception:
            # put_nowait shouldn't raise except QueueFull, but be defensive.
            logger.exception("ATIF enqueue failed — record dropped")

    async def flush(self, timeout_s: float = 2.0) -> bool:
        """Wait for the writer to drain the queue.

        Returns True if queue was drained (or already empty), False on
        timeout. Test helper — production code never calls this; the
        writer loop runs until the event loop shuts down.
        """
        if self._sink is None:
            return True
        deadline = asyncio.get_event_loop().time() + timeout_s
        while not self._sink.empty():
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def aclose(self) -> None:
        """Cancel the writer task. Test helper; not called in production."""
        if self._writer_task is not None and not self._writer_task.done():
            self._writer_task.cancel()
            try:
                await self._writer_task
            except (asyncio.CancelledError, Exception):
                pass
        self._writer_task = None
        self._sink = None

    # ── Internals ──────────────────────────────────────────────────

    async def _writer_loop(self) -> None:
        """Single concurrent writer — pulls from queue, writes to disk."""
        try:
            export_dir = Path(self._cfg.export_path)
            export_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception(
                "ATIF export_path unreachable (%s) — writer exiting",
                self._cfg.export_path,
            )
            return

        while True:
            try:
                record = await self._sink.get()
            except asyncio.CancelledError:
                logger.debug("ATIF writer cancelled — exiting loop")
                break
            try:
                self._write_record_sync(record, export_dir=export_dir)
            except Exception:
                logger.exception("ATIF write failed — record lost")

    def _write_record_sync(
        self,
        record: dict[str, Any],
        *,
        export_dir: Path | None = None,
    ) -> None:
        """Append a record to today's JSONL file."""
        if export_dir is None:
            export_dir = Path(self._cfg.export_path)
            export_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fpath = export_dir / f"{date_str}.jsonl"
        line = json.dumps(record, ensure_ascii=False, default=_json_default)
        with fpath.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _format(self, trace: dict[str, Any]) -> dict[str, Any]:
        """Apply config-driven transforms: PII redaction, field stripping.

        Order matters:
          1. Strip unwanted top-level sections (handoff_chain, cost_usd)
             before doing any redaction work — saves CPU when disabled.
          2. Serialise any nested dataclasses (HandoffRequest/Response,
             intent result objects) to plain dicts via json round-trip.
          3. Walk the resulting plain dict and redact PII in text fields.

        Always sets `timestamp` to current UTC ISO-8601 if the caller
        didn't provide one (deterministic ordering for replay).
        """
        record: dict[str, Any] = dict(trace)
        record.setdefault(
            "timestamp", datetime.now(timezone.utc).isoformat(timespec="seconds")
        )

        if not self._cfg.include_handoff:
            record.pop("handoff_chain", None)

        if not self._cfg.include_cost:
            record.pop("cost_usd", None)

        # Serialise dataclasses → plain dicts so _redact_record can use
        # isinstance(x, dict) checks uniformly.
        try:
            record = json.loads(json.dumps(record, default=_json_default))
        except (TypeError, ValueError):
            logger.exception("ATIF serialisation failed — record dropped")
            raise

        if self._cfg.redact_pii:
            record = self._redact_record(record)

        return record

    def _redact_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Mask PII in known text-bearing fields.

        Walks user_message, output, and turns[*].content. Other fields
        (intent label, mode, etc.) are short controlled strings and left
        alone. Artifacts under handoff_chain could carry arbitrary user
        data — those are redacted via _redact_text recursively on string
        values when include_handoff=True.
        """
        # Top-level free-text fields
        for key in ("user_message", "output"):
            if key in record:
                record[key] = _redact_text(record[key])

        # turns: list of {"role":..., "content":...}
        turns = record.get("turns")
        if isinstance(turns, list):
            new_turns = []
            for t in turns:
                if isinstance(t, dict) and "content" in t:
                    t = {**t, "content": _redact_text(t["content"])}
                new_turns.append(t)
            record["turns"] = new_turns

        # handoff_chain: redact strings inside request.message and
        # response.output, leave structured fields alone.
        chain = record.get("handoff_chain")
        if isinstance(chain, list):
            new_chain = []
            for hop in chain:
                if not isinstance(hop, dict):
                    new_chain.append(hop)
                    continue
                hop = dict(hop)
                req = hop.get("request")
                if isinstance(req, dict) and "message" in req:
                    hop["request"] = {**req, "message": _redact_text(req["message"])}
                resp = hop.get("response")
                if isinstance(resp, dict) and "output" in resp:
                    hop["response"] = {**resp, "output": _redact_text(resp["output"])}
                new_chain.append(hop)
            record["handoff_chain"] = new_chain

        return record


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for dataclasses and datetime."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    # Last resort — stringify so we never fail the write
    return str(obj)


__all__ = ["AtifExporter"]
