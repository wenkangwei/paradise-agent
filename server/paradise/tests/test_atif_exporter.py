"""Phase 2.13 — AtifExporter + cleanup_node integration tests."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paradise.config import AtifConfig, ParadiseConfig
from paradise.core.handoff import (
    HANDOFF_ERROR,
    HANDOFF_OK,
    HandoffRequest,
    HandoffResponse,
)
from paradise.observability.atif_exporter import AtifExporter, _redact_text


# ── Minimal mocks (inline so this file is self-contained) ──────────


@dataclass
class _MockUsage:
    prompt_tokens: int = 5
    completion_tokens: int = 5
    total_tokens: int = 10


@dataclass
class _MockResponse:
    content: str | None
    tool_calls: list | None = None
    finish_reason: str = "stop"
    usage: _MockUsage | None = field(default_factory=_MockUsage)


class _ScriptedTransport:
    """Mock transport that pops responses from a queue."""
    api_mode = "openai_compat"

    def __init__(self, responses: list[_MockResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("ScriptedTransport queue empty")
        return self._responses.pop(0)


# ── Helpers ────────────────────────────────────────────────────────


def _enabled_cfg(
    export_path: str,
    *,
    redact_pii: bool = True,
    include_handoff: bool = True,
    include_cost: bool = True,
) -> AtifConfig:
    return AtifConfig(
        enabled=True,
        export_path=export_path,
        redact_pii=redact_pii,
        include_handoff=include_handoff,
        include_cost=include_cost,
    )


def _sample_trace(*, user_message: str = "hi", output: str = "hello") -> dict[str, Any]:
    """Minimal trace matching ARCHITECTURE_V2.md §7.2 shape."""
    return {
        "session_id": "s1",
        "trace_id": "t1",
        "user_message": user_message,
        "intent": {"label": "chitchat", "confidence": 0.9, "source": "rule"},
        "mode": "chat",
        "handoff_chain": [
            {
                "depth": 1,
                "mode": "chat",
                "request": HandoffRequest(
                    session_id="s1", user_id="u1", trace_id="t1",
                    message=user_message,
                ),
                "response": HandoffResponse(
                    session_id="s1", trace_id="t1", output=output,
                    cost_incurred_usd=0.001, turns_used=1,
                ),
            }
        ],
        "turns": [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": output},
        ],
        "cost_usd": 0.001,
        "latency_ms": None,
        "retry_count": 0,
        "feedback": None,
    }


async def _drain(exporter: AtifExporter, *, timeout_s: float = 1.0) -> None:
    """Wait until queue is empty OR writer task has exited.

    A writer that exits (e.g., because export_path is unreachable) is a
    valid terminal state — records get pulled and dropped before exit,
    so a successful test is one that doesn't hang.
    """
    if exporter._sink is None:
        return
    deadline = asyncio.get_event_loop().time() + timeout_s
    while not exporter._sink.empty():
        # Writer task exited → queue will never drain, but that's OK.
        if exporter._writer_task is not None and exporter._writer_task.done():
            return
        if asyncio.get_event_loop().time() >= deadline:
            return
        await asyncio.sleep(0.01)


def _read_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


# ── TestRedactText ─────────────────────────────────────────────────


class TestRedactText:
    """Unit tests for the PII regex layer."""

    def test_chinese_mobile_masked(self):
        assert _redact_text("我的手机是13800138000") == "我的手机是[REDACTED]"

    def test_ascii_context_mobile_masked(self):
        assert _redact_text("call 13800138000 now") == "call [REDACTED] now"

    def test_id_card_masked(self):
        assert _redact_text("身份证110101199003070123") == "身份证[REDACTED]"

    def test_id_card_with_x_suffix_masked(self):
        # 17 digits + X is a valid ID card format
        assert _redact_text("id=12345678901234567X") == "id=[REDACTED]"

    def test_bank_card_masked(self):
        assert _redact_text("卡号6222020200112345678") == "卡号[REDACTED]"

    def test_partial_bank_card_not_touched_by_mobile(self):
        """Mobile regex must not match first 11 digits of a 19-digit bank card."""
        s = "6222020200112345678"
        result = _redact_text(s)
        # Whole thing is matched as a bank card, no partial mobile redaction
        assert "[REDACTED]" in result
        assert "13800138000" not in result

    def test_empty_string_passthrough(self):
        assert _redact_text("") == ""

    def test_non_string_passthrough(self):
        # _redact_text is only called on strings; verify it rejects gracefully
        assert _redact_text(None) is None  # type: ignore[arg-type]
        assert _redact_text(42) == 42  # type: ignore[arg-type]

    def test_idempotent_on_already_redacted(self):
        once = _redact_text("phone=13800138000")
        twice = _redact_text(once)
        assert once == twice == "phone=[REDACTED]"

    def test_mixed_pii_in_one_string(self):
        s = "手机13800138000身份证110101199003070123卡6222020200112345678"
        result = _redact_text(s)
        assert "13800138000" not in result
        assert "110101199003070123" not in result
        assert "6222020200112345678" not in result
        assert result.count("[REDACTED]") == 3


# ── TestAtifExporterDisabled ───────────────────────────────────────


class TestAtifExporterDisabled:
    """Zero-overhead guarantee when enabled=False."""

    def test_enabled_property_reflects_config(self):
        exp = AtifExporter(AtifConfig(enabled=False))
        assert exp.enabled is False
        exp_on = AtifExporter(AtifConfig(enabled=True, export_path="/tmp/x"))
        assert exp_on.enabled is True

    def test_disabled_export_writes_nothing(self, tmp_path: Path):
        exp = AtifExporter(AtifConfig(enabled=False, export_path=str(tmp_path)))
        asyncio.run(exp.export(_sample_trace()))
        # No writer task should have been spawned
        assert exp._sink is None
        # No files created
        assert list(tmp_path.iterdir()) == []

    def test_disabled_export_does_not_raise_on_bad_input(self, tmp_path: Path):
        exp = AtifExporter(AtifConfig(enabled=False, export_path=str(tmp_path)))
        asyncio.run(exp.export("not a dict"))  # type: ignore[arg-type]
        asyncio.run(exp.export(None))  # type: ignore[arg-type]
        asyncio.run(exp.export({}))


# ── TestAtifExporterEnabled ────────────────────────────────────────


class TestAtifExporterEnabled:
    """End-to-end JSONL file writing."""

    def test_export_writes_one_jsonl_record(self, tmp_path: Path):
        exp = AtifExporter(_enabled_cfg(str(tmp_path)))
        try:
            async def _run():
                await exp.export(_sample_trace(user_message="hello"))
                await _drain(exp)
            asyncio.run(_run())
        finally:
            asyncio.run(exp.aclose())

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        records = _read_jsonl(files[0])
        assert len(records) == 1
        assert records[0]["session_id"] == "s1"
        assert records[0]["mode"] == "chat"

    def test_export_appends_to_same_day_file(self, tmp_path: Path):
        exp = AtifExporter(_enabled_cfg(str(tmp_path)))
        try:
            async def _run():
                await exp.export(_sample_trace(user_message="msg1"))
                await exp.export(_sample_trace(user_message="msg2"))
                await exp.export(_sample_trace(user_message="msg3"))
                await _drain(exp)
            asyncio.run(_run())
        finally:
            asyncio.run(exp.aclose())

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1  # all same UTC day
        records = _read_jsonl(files[0])
        assert len(records) == 3

    def test_export_never_raises_on_internal_error(self, tmp_path: Path):
        """Disk error / bad trace must not propagate."""
        exp = AtifExporter(_enabled_cfg("/nonexistent/path/that/cannot/be/created"))
        # Should not raise even though write will fail
        async def _run():
            await exp.export(_sample_trace())
            await _drain(exp)
        asyncio.run(_run())
        asyncio.run(exp.aclose())

    def test_export_ignores_non_dict_input(self, tmp_path: Path):
        exp = AtifExporter(_enabled_cfg(str(tmp_path)))
        async def _run():
            await exp.export("string")  # type: ignore[arg-type]
            await exp.export(None)  # type: ignore[arg-type]
            await _drain(exp)
        asyncio.run(_run())
        asyncio.run(exp.aclose())
        # No records written
        files = list(tmp_path.glob("*.jsonl"))
        assert files == []

    def test_export_serialises_dataclasses_in_handoff_chain(self, tmp_path: Path):
        """HandoffRequest/Response are dataclasses — must JSON-encode."""
        exp = AtifExporter(_enabled_cfg(str(tmp_path)))
        try:
            async def _run():
                await exp.export(_sample_trace())
                await _drain(exp)
            asyncio.run(_run())
        finally:
            asyncio.run(exp.aclose())

        files = list(tmp_path.glob("*.jsonl"))
        record = _read_jsonl(files[0])[0]
        # Dataclasses should be serialised as dicts, not str()
        assert isinstance(record["handoff_chain"][0]["request"], dict)
        assert record["handoff_chain"][0]["request"]["session_id"] == "s1"
        assert record["handoff_chain"][0]["response"]["output"] == "hello"

    def test_export_sets_timestamp_when_missing(self, tmp_path: Path):
        exp = AtifExporter(_enabled_cfg(str(tmp_path)))
        try:
            async def _run():
                trace = _sample_trace()
                trace.pop("timestamp", None)  # not in sample, but be explicit
                await exp.export(trace)
                await _drain(exp)
            asyncio.run(_run())
        finally:
            asyncio.run(exp.aclose())
        files = list(tmp_path.glob("*.jsonl"))
        record = _read_jsonl(files[0])[0]
        assert "timestamp" in record
        # ISO-8601 starts with a year
        assert record["timestamp"][4] == "-"


# ── TestAtifExporterConfigDrivenFields ─────────────────────────────


class TestAtifExporterConfigDrivenFields:
    """include_handoff / include_cost / redact_pii toggles."""

    def test_include_handoff_false_strips_chain(self, tmp_path: Path):
        exp = AtifExporter(_enabled_cfg(str(tmp_path), include_handoff=False))
        try:
            async def _run():
                await exp.export(_sample_trace())
                await _drain(exp)
            asyncio.run(_run())
        finally:
            asyncio.run(exp.aclose())
        record = _read_jsonl(list(tmp_path.glob("*.jsonl"))[0])[0]
        assert "handoff_chain" not in record

    def test_include_cost_false_strips_cost(self, tmp_path: Path):
        exp = AtifExporter(_enabled_cfg(str(tmp_path), include_cost=False))
        try:
            async def _run():
                await exp.export(_sample_trace())
                await _drain(exp)
            asyncio.run(_run())
        finally:
            asyncio.run(exp.aclose())
        record = _read_jsonl(list(tmp_path.glob("*.jsonl"))[0])[0]
        assert "cost_usd" not in record

    def test_redact_pii_false_keeps_plaintext(self, tmp_path: Path):
        exp = AtifExporter(_enabled_cfg(str(tmp_path), redact_pii=False))
        try:
            async def _run():
                await exp.export(
                    _sample_trace(user_message="call 13800138000")
                )
                await _drain(exp)
            asyncio.run(_run())
        finally:
            asyncio.run(exp.aclose())
        record = _read_jsonl(list(tmp_path.glob("*.jsonl"))[0])[0]
        assert "13800138000" in record["user_message"]
        assert "13800138000" in record["turns"][0]["content"]

    def test_redact_pii_true_masks_all_text_fields(self, tmp_path: Path):
        exp = AtifExporter(_enabled_cfg(str(tmp_path), redact_pii=True))
        try:
            async def _run():
                await exp.export(
                    _sample_trace(user_message="phone=13800138000")
                )
                await _drain(exp)
            asyncio.run(_run())
        finally:
            asyncio.run(exp.aclose())
        record = _read_jsonl(list(tmp_path.glob("*.jsonl"))[0])[0]
        # user_message + turns[].content + handoff_chain[].request.message
        assert "13800138000" not in record["user_message"]
        assert "13800138000" not in record["turns"][0]["content"]
        assert "13800138000" not in record["handoff_chain"][0]["request"]["message"]

    def test_redact_does_not_touch_cost(self, tmp_path: Path):
        exp = AtifExporter(_enabled_cfg(str(tmp_path)))
        try:
            async def _run():
                await exp.export(_sample_trace())
                await _drain(exp)
            asyncio.run(_run())
        finally:
            asyncio.run(exp.aclose())
        record = _read_jsonl(list(tmp_path.glob("*.jsonl"))[0])[0]
        assert record["cost_usd"] == 0.001


# ── TestAtifExporterResilience ─────────────────────────────────────


class TestAtifExporterResilience:
    """Queue overflow, format errors, concurrent calls."""

    def test_queue_overflow_records_drop(self, tmp_path: Path):
        """Fill queue past maxsize=1000 — should not block or raise."""
        cfg = _enabled_cfg(str(tmp_path))
        exp = AtifExporter(cfg)

        async def _run():
            # Block the writer artificially by NOT awaiting any exports.
            # The first export inits the queue; subsequent put_nowait calls
            # fill it. After 1000 records are queued, additional puts
            # should be dropped, not raised.
            # Init queue without writing by overriding _writer_loop:
            exp._sink = asyncio.Queue(maxsize=1000)
            for i in range(1500):
                await exp.export(_sample_trace(user_message=f"msg{i}"))
            # Drain at the end so the writer task can finish
            exp._writer_task = asyncio.create_task(exp._writer_loop())
            await _drain(exp, timeout_s=5.0)

        asyncio.run(_run())
        asyncio.run(exp.aclose())
        assert exp.dropped_count == 500

    def test_multiple_exports_concurrent_safe(self, tmp_path: Path):
        """10 concurrent exports should all land without race."""
        exp = AtifExporter(_enabled_cfg(str(tmp_path)))

        async def _run():
            await asyncio.gather(
                *[exp.export(_sample_trace(user_message=f"m{i}")) for i in range(10)]
            )
            await _drain(exp)

        asyncio.run(_run())
        asyncio.run(exp.aclose())
        files = list(tmp_path.glob("*.jsonl"))
        records = _read_jsonl(files[0])
        assert len(records) == 10


# ── TestAtifConfigParsing ──────────────────────────────────────────


class TestAtifConfigParsing:
    """ParadiseConfig.from_dict round-trips AtifConfig."""

    def test_default_config_has_atif_disabled(self):
        cfg = ParadiseConfig()
        assert cfg.atif.enabled is False
        assert cfg.atif.export_path == "data/atif"
        assert cfg.atif.redact_pii is True
        assert cfg.atif.include_handoff is True
        assert cfg.atif.include_cost is True

    def test_from_dict_parses_atif_section(self):
        cfg = ParadiseConfig.from_dict({
            "atif": {
                "enabled": True,
                "export_path": "/custom/path",
                "redact_pii": False,
            }
        })
        assert cfg.atif.enabled is True
        assert cfg.atif.export_path == "/custom/path"
        assert cfg.atif.redact_pii is False
        # Unspecified fields keep defaults
        assert cfg.atif.include_handoff is True

    def test_from_dict_ignores_unknown_atif_keys(self):
        """Unknown keys must not crash config loading."""
        cfg = ParadiseConfig.from_dict({
            "atif": {"enabled": True, "unknown_field": "ignored"}
        })
        assert cfg.atif.enabled is True

    def test_from_dict_without_atif_section_uses_defaults(self):
        cfg = ParadiseConfig.from_dict({"llm": {"model": "x"}})
        assert cfg.atif.enabled is False


# ── TestSupervisorAtifIntegration ──────────────────────────────────


class TestSupervisorAtifIntegration:
    """End-to-end: cleanup_node calls exporter with correct trace shape."""

    def test_cleanup_node_calls_exporter_when_enabled(self, tmp_path: Path):
        """A full supervisor turn should land a JSONL record."""
        from paradise.core.registry import SubgraphRegistry
        from paradise.core.patterns import register_all
        from paradise.core.patterns.chat import ChatPattern
        from paradise.core.supervisor import build_supervisor_graph

        # Stand up a minimal chat-only registry — bypass ollama entirely
        transport = _ScriptedTransport([_MockResponse(content="hello back")])
        registry = SubgraphRegistry()
        register_all(registry)
        # Replace chat stub with real ChatPattern
        del registry._patterns["chat"]
        registry.register(ChatPattern(transport=transport))

        exporter = AtifExporter(_enabled_cfg(str(tmp_path)))
        try:
            graph = build_supervisor_graph(
                registry,
                intent_classifier=None,
                mode_judge=None,
                atif_exporter=exporter,
            )
            result = asyncio.run(graph.ainvoke(
                {"user_message": "hi", "session_id": "sess1", "trace_id": "tr1"},
                config={"configurable": {"thread_id": "sess1"}},
            ))
            asyncio.run(_drain(exporter))
        finally:
            asyncio.run(exporter.aclose())

        assert result["final_output"] == "hello back"
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        record = _read_jsonl(files[0])[0]
        assert record["session_id"] == "sess1"
        assert record["trace_id"] == "tr1"
        assert record["mode"] == "chat"
        assert record["turns"][0]["content"] == "hi"
        assert record["turns"][1]["content"] == "hello back"

    def test_cleanup_node_skips_export_when_disabled(self, tmp_path: Path):
        """Enabled=False means zero exporter activity."""
        from paradise.core.registry import SubgraphRegistry
        from paradise.core.patterns import register_all
        from paradise.core.patterns.chat import ChatPattern
        from paradise.core.supervisor import build_supervisor_graph

        transport = _ScriptedTransport([_MockResponse(content="ack")])
        registry = SubgraphRegistry()
        register_all(registry)
        del registry._patterns["chat"]
        registry.register(ChatPattern(transport=transport))

        exporter = AtifExporter(AtifConfig(enabled=False, export_path=str(tmp_path)))
        try:
            graph = build_supervisor_graph(
                registry, atif_exporter=exporter,
            )
            asyncio.run(graph.ainvoke(
                {"user_message": "hi", "session_id": "s1"},
                config={"configurable": {"thread_id": "s1"}},
            ))
        finally:
            asyncio.run(exporter.aclose())

        # No files written
        assert list(tmp_path.glob("*.jsonl")) == []
        # Queue never initialised
        assert exporter._sink is None

    def test_cleanup_node_skips_export_when_none(self, tmp_path: Path):
        """atif_exporter=None preserves Phase 2.9 behavior."""
        from paradise.core.registry import SubgraphRegistry
        from paradise.core.patterns import register_all
        from paradise.core.patterns.chat import ChatPattern
        from paradise.core.supervisor import build_supervisor_graph

        transport = _ScriptedTransport([_MockResponse(content="ack2")])
        registry = SubgraphRegistry()
        register_all(registry)
        del registry._patterns["chat"]
        registry.register(ChatPattern(transport=transport))

        graph = build_supervisor_graph(registry, atif_exporter=None)
        result = asyncio.run(graph.ainvoke(
            {"user_message": "hi", "session_id": "s1"},
            config={"configurable": {"thread_id": "s1"}},
        ))
        assert result["final_output"] == "ack2"

    def test_exporter_failure_does_not_break_turn(self, tmp_path: Path):
        """If exporter.export raises despite contract, turn still succeeds."""
        from paradise.core.registry import SubgraphRegistry
        from paradise.core.patterns import register_all
        from paradise.core.patterns.chat import ChatPattern
        from paradise.core.supervisor import build_supervisor_graph

        transport = _ScriptedTransport([_MockResponse(content="ok")])
        registry = SubgraphRegistry()
        register_all(registry)
        del registry._patterns["chat"]
        registry.register(ChatPattern(transport=transport))

        class _RaisingExporter:
            enabled = True

            async def export(self, trace):  # noqa: ANN001
                raise RuntimeError("simulated exporter bug")

            async def aclose(self):
                pass

        graph = build_supervisor_graph(
            registry, atif_exporter=_RaisingExporter(),
        )
        # Must NOT raise — cleanup swallows exporter errors
        result = asyncio.run(graph.ainvoke(
            {"user_message": "hi", "session_id": "s1"},
            config={"configurable": {"thread_id": "s1"}},
        ))
        assert result["final_output"] == "ok"


# ── TestFactoryWiring ──────────────────────────────────────────────


class TestFactoryWiring:
    """factory.build_atif_exporter reads config correctly."""

    def test_build_atif_exporter_default_disabled(self):
        from paradise.factory import build_atif_exporter
        cfg = ParadiseConfig()
        exp = build_atif_exporter(cfg)
        assert exp.enabled is False

    def test_build_atif_exporter_enabled_when_config_set(self):
        from paradise.factory import build_atif_exporter
        cfg = ParadiseConfig()
        cfg.atif = AtifConfig(enabled=True, export_path="/tmp/x")
        exp = build_atif_exporter(cfg)
        assert exp.enabled is True

    def test_build_atif_exporter_never_raises_on_bad_config(self):
        """If AtifConfig is malformed, returns inert exporter."""
        from paradise.factory import build_atif_exporter
        cfg = ParadiseConfig()
        # Corrupt the atif field with an invalid type
        cfg.atif = "not an AtifConfig"  # type: ignore[assignment]
        exp = build_atif_exporter(cfg)
        assert exp.enabled is False
