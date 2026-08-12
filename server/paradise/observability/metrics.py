"""Paradise metrics — TTFT, tool latency, cost per request.

Phase 6 — Observability.

Three core metrics exposed via /metrics (prometheus) or OTel meter:
  - ttft_ms:             histogram, time-to-first-token
  - tool_call_duration_ms: histogram, per tool
  - cost_per_request_usd:  histogram, estimated cost per request

When OTel not installed, metrics are tracked in-memory (dict counters) and
exposed via a simple /metrics text endpoint.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

logger = logging.getLogger("paradise.observability.metrics")

# ── In-memory metrics store (works without OTel) ──────────────────
_metrics_lock = threading.Lock()
_counters: dict[str, float] = defaultdict(float)
_histograms: dict[str, list[float]] = defaultdict(list)


def record_ttft(ms: float) -> None:
    """Time-to-first-token in milliseconds."""
    with _metrics_lock:
        _histograms["ttft_ms"].append(float(ms))
        # Cap memory usage
        if len(_histograms["ttft_ms"]) > 10_000:
            _histograms["ttft_ms"] = _histograms["ttft_ms"][-5_000:]


def record_tool_call(tool_name: str, ms: float) -> None:
    """Per-tool call duration in milliseconds."""
    with _metrics_lock:
        _histograms[f"tool_call_duration_ms:{tool_name}"].append(float(ms))
        _histograms["tool_call_duration_ms"].append(float(ms))


def record_cost(usd: float) -> None:
    """Estimated cost per request in USD."""
    with _metrics_lock:
        _histograms["cost_per_request_usd"].append(float(usd))
        _counters["cost_total_usd"] += float(usd)


def get_meter():
    """Return self — this module IS the meter (no-op bridge for OTel compat)."""
    return _ModuleMeter()


class _ModuleMeter:
    """Bridge so callers expecting an OTel-style meter can use this module."""
    def create_histogram(self, name, **kwargs):
        return _HistogramProxy(name)


class _HistogramProxy:
    def __init__(self, name: str):
        self._name = name

    def record(self, value, **kwargs):
        with _metrics_lock:
            _histograms[self._name].append(float(value))


def render_prometheus() -> str:
    """Render metrics in Prometheus text exposition format.

    Used by /metrics endpoint. Calculates p50/p99 for histograms.
    """
    lines = []
    with _metrics_lock:
        snapshot = {k: list(v) for k, v in _histograms.items()}
        for name, values in snapshot.items():
            if not values:
                continue
            sorted_v = sorted(values)
            n = len(sorted_v)
            p50 = sorted_v[n // 2]
            p99 = sorted_v[min(n - 1, int(n * 0.99))]
            total = sum(values)
            avg = total / n
            lines.append(f"# HELP {name} histogram (p50/p99/avg/count)")
            lines.append(f"# TYPE {name} summary")
            lines.append(f'{name}_count {n}')
            lines.append(f'{name}_avg {avg:.4f}')
            lines.append(f'{name}_p50 {p50:.4f}')
            lines.append(f'{name}_p99 {p99:.4f}')
            lines.append(f'{name}_sum {total:.4f}')
        for name, value in _counters.items():
            lines.append(f"# HELP {name} counter")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value:.6f}")
    return "\n".join(lines) + "\n"


def _histograms_safe_copy() -> dict[str, list[float]]:
    """Snapshot histograms under lock — avoids mutation during render."""
    # Caller must hold _metrics_lock; this helper just deep-copies
    return {k: list(v) for k, v in _histograms.items()}


def reset() -> None:
    """Reset all metrics — test helper."""
    with _metrics_lock:
        _counters.clear()
        _histograms.clear()
