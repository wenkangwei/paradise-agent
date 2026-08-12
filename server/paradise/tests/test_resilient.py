"""Phase 3 tests — ResilientTransport wrapper."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_circuit_breaker_starts_closed():
    from paradise.transports.resilient import CircuitBreaker
    cb = CircuitBreaker(failure_threshold=3, recovery_seconds=30)
    assert cb.state == "closed"
    assert cb.allow() is True


def test_circuit_breaker_trips_after_threshold():
    from paradise.transports.resilient import CircuitBreaker
    cb = CircuitBreaker(failure_threshold=3, recovery_seconds=30)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed"  # not yet
    cb.record_failure()
    assert cb.state == "open"
    assert cb.allow() is False


def test_circuit_breaker_reopens_after_recovery():
    """Verify the full closed → open → half_open → closed cycle.

    Uses a 1-second recovery window to avoid race with monotonic clock.
    """
    import time
    from paradise.transports.resilient import CircuitBreaker
    cb = CircuitBreaker(failure_threshold=2, recovery_seconds=1)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    assert cb.allow() is False
    # Wait for recovery window
    time.sleep(1.05)
    # After recovery window, the state property should transition to half_open
    assert cb.state == "half_open"
    assert cb.allow() is True  # half_open allows one trial call
    # Trial succeeds → circuit closes
    cb.record_success()
    assert cb.state == "closed"


@pytest.mark.asyncio
async def test_resilient_transport_passthrough_on_success():
    from paradise.transports.resilient import ResilientTransport

    fake_inner = MagicMock()
    fake_inner.chat = AsyncMock(return_value="ok")
    fake_inner.api_mode = "openai_compat"

    rt = ResilientTransport(fake_inner, cost_budget_per_request_usd=1.0)
    result = await rt.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=100)
    assert result == "ok"
    fake_inner.chat.assert_awaited_once()
    # Circuit still closed
    assert rt.circuit_state() == "closed"


@pytest.mark.asyncio
async def test_resilient_transport_records_failure_on_exception():
    from paradise.transports.resilient import ResilientTransport

    fake_inner = MagicMock()
    # Raise non-retryable error (ValueError, not ConnectionError)
    fake_inner.chat = AsyncMock(side_effect=ValueError("bad request"))
    fake_inner.api_mode = "openai_compat"

    rt = ResilientTransport(
        fake_inner,
        max_retries=2,
        circuit_failure_threshold=2,
        cost_budget_per_request_usd=1.0,
    )
    with pytest.raises(ValueError):
        await rt.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=100)

    assert rt.circuit_state() == "closed"  # 1 failure < threshold 2

    with pytest.raises(ValueError):
        await rt.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=100)

    # Now 2 failures ≥ threshold → circuit trips
    assert rt.circuit_state() == "open"


@pytest.mark.asyncio
async def test_resilient_transport_circuit_open_raises():
    from paradise.transports.resilient import ResilientTransport, CircuitOpenError

    fake_inner = MagicMock()
    fake_inner.chat = AsyncMock(return_value="never")
    fake_inner.api_mode = "openai_compat"

    rt = ResilientTransport(
        fake_inner,
        circuit_failure_threshold=1,
        cost_budget_per_request_usd=1.0,
    )
    # Force the breaker open
    rt._breaker.record_failure()
    rt._breaker.record_failure()  # trip

    with pytest.raises(CircuitOpenError):
        await rt.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=100)


@pytest.mark.asyncio
async def test_resilient_transport_retries_connection_error():
    from paradise.transports.resilient import ResilientTransport

    # First two attempts: ConnectionError. Third: success.
    call_count = {"n": 0}

    async def flaky_chat(**kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise ConnectionError("network down")
        return "recovered"

    fake_inner = MagicMock()
    fake_inner.chat = flaky_chat
    fake_inner.api_mode = "openai_compat"

    rt = ResilientTransport(
        fake_inner,
        max_retries=5,
        retry_backoff_max_seconds=1,
        circuit_failure_threshold=10,  # high so it doesn't trip
        cost_budget_per_request_usd=1.0,
    )
    result = await rt.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=100)
    assert result == "recovered"
    assert call_count["n"] == 3
    assert rt.circuit_state() == "closed"


@pytest.mark.asyncio
async def test_resilient_transport_cost_budget_rejects():
    from paradise.transports.resilient import ResilientTransport, CostBudgetExceeded

    fake_inner = MagicMock()
    fake_inner.chat = AsyncMock(return_value="should not reach")
    fake_inner.api_mode = "openai_compat"

    rt = ResilientTransport(
        fake_inner,
        cost_budget_per_request_usd=0.0001,  # tiny budget
    )
    # Large messages → cost estimate blows past budget
    big_messages = [{"role": "user", "content": "x" * 100_000}]
    with pytest.raises(CostBudgetExceeded):
        await rt.chat(messages=big_messages, max_tokens=8192)
    fake_inner.chat.assert_not_called()


@pytest.mark.asyncio
async def test_resilient_transport_stream_chat_success():
    from paradise.transports.resilient import ResilientTransport

    async def fake_stream(**kwargs):
        for chunk in ["Hello ", "world"]:
            yield chunk

    fake_inner = MagicMock()
    fake_inner.stream_chat = fake_stream
    fake_inner.api_mode = "openai_compat"

    rt = ResilientTransport(fake_inner, cost_budget_per_request_usd=1.0)
    chunks = []
    async for c in rt.stream_chat(messages=[{"role": "user", "content": "hi"}], max_tokens=100):
        chunks.append(c)
    assert chunks == ["Hello ", "world"]
    assert rt.circuit_state() == "closed"


def test_attribute_passthrough():
    """ResilientTransport must proxy unknown attrs to inner transport."""
    from paradise.transports.resilient import ResilientTransport

    fake_inner = MagicMock()
    fake_inner.api_mode = "anthropic_messages"
    fake_inner.convert_messages = MagicMock(return_value="converted")

    rt = ResilientTransport(fake_inner, cost_budget_per_request_usd=1.0)
    assert rt.api_mode == "anthropic_messages"
    assert rt.convert_messages(messages=[]) == "converted"
