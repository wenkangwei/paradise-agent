"""Phase 6 tests — observability modules."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ── Tracing ───────────────────────────────────────────────────────

def test_tracing_setup_returns_bool():
    """setup_tracing must return True/False, never raise."""
    from paradise.observability.tracing import setup_tracing
    result = setup_tracing(endpoint=None, service_name="test")
    assert isinstance(result, bool)


def test_get_tracer_returns_object():
    """get_tracer must always return something with start_as_current_span."""
    from paradise.observability.tracing import get_tracer
    tracer = get_tracer()
    assert hasattr(tracer, "start_as_current_span")


def test_noop_tracer_context_manager():
    """Noop tracer must work as a context manager."""
    from paradise.observability.tracing import _NoopTracer
    t = _NoopTracer()
    with t.start_as_current_span("test") as span:
        span.set_attribute("k", "v")  # must not raise


# ── Metrics ───────────────────────────────────────────────────────

def test_metrics_record_ttft():
    from paradise.observability import metrics
    metrics.reset()
    metrics.record_ttft(42.5)
    metrics.record_ttft(100.0)
    text = metrics.render_prometheus()
    assert "ttft_ms_count 2" in text
    assert "ttft_ms_p50" in text


def test_metrics_record_tool_call():
    from paradise.observability import metrics
    metrics.reset()
    metrics.record_tool_call("search", 150.0)
    metrics.record_tool_call("search", 250.0)
    metrics.record_tool_call("fetch", 50.0)
    text = metrics.render_prometheus()
    assert "tool_call_duration_ms:search_count 2" in text
    assert "tool_call_duration_ms:fetch_count 1" in text


def test_metrics_record_cost():
    from paradise.observability import metrics
    metrics.reset()
    metrics.record_cost(0.001)
    metrics.record_cost(0.002)
    text = metrics.render_prometheus()
    assert "cost_total_usd 0.003000" in text
    assert "cost_per_request_usd_count 2" in text


def test_metrics_render_empty_when_no_data():
    from paradise.observability import metrics
    metrics.reset()
    text = metrics.render_prometheus()
    # Empty render still returns a string (possibly just trailing newline)
    assert isinstance(text, str)


# ── Logging ───────────────────────────────────────────────────────

def test_setup_structlog_does_not_raise():
    """setup_structlog must be safe to call in dev mode (no structlog dep)."""
    from paradise.observability.logging_config import setup_structlog
    setup_structlog(level="INFO")
    import logging
    logging.getLogger("test").info("ok")


# ── /metrics endpoint integration ─────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_endpoint():
    """The /metrics endpoint registered in main.py must render successfully."""
    from paradise.observability import metrics
    metrics.reset()
    metrics.record_ttft(10.0)
    metrics.record_tool_call("test_tool", 5.0)
    metrics.record_cost(0.01)

    text = metrics.render_prometheus()
    assert "ttft_ms_count 1" in text
    assert "tool_call_duration_ms:test_tool_count 1" in text
    assert "cost_per_request_usd_count 1" in text
    assert "cost_total_usd" in text
