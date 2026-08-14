"""Phase 2.8 — mode_judge unit tests.

Run:
    cd server && python -m pytest paradise/tests/test_mode_judge.py -v

Scope:
  * _parse_mode_json lenient JSON extraction
  * ModeJudge.classify happy path / invalid JSON / network error
  * Hallucination guard (unknown mode → None)
  * Single-mode registry short-circuit
  * Mode snapshot at construction (mutation after doesn't change view)
  * System prompt includes available modes, excludes stubs
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from paradise.core.mode_judge import (
    ModeJudge,
    ModeJudgeResult,
    _parse_mode_json,
)
from paradise.core.patterns import register_all, register_basic_patterns
from paradise.core.patterns.base import SubgraphPattern
from paradise.core.registry import SubgraphRegistry


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


def _build_registry() -> SubgraphRegistry:
    """Build a registry with the 4 basic modes available
    (chat / tool_react / rag / plan_execute).

    Uses a dummy `object()` transport — mode_judge never invokes the
    transport, only reads registry.list_modes() / build_mode_prompt().
    Phase 2.11 expanded register_basic_patterns to include rag +
    plan_execute, so the snapshot is 4 modes now.
    """
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=object())
    return registry


def _ollama_response(status: int, content: str) -> httpx.Response:
    """Build a fake ollama /api/chat Response with request context set.

    httpx.Response requires `request=` for raise_for_status() to work,
    which ModeJudge.classify calls internally. Without it, the mock
    blows up on the happy path even when the body is valid.
    """
    req = httpx.Request("POST", "http://x/api/chat")
    return httpx.Response(
        status,
        json={"message": {"content": content}},
        request=req,
    )


def _patch_post(response_or_exc):
    """Patch httpx.AsyncClient.post to return a response or raise.

    `response_or_exc` is either an httpx.Response or an Exception.
    """
    if isinstance(response_or_exc, Exception):
        return patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=response_or_exc),
        )
    return patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=response_or_exc),
    )


# ─────────────────────────────────────────────────────────────────────
# _parse_mode_json
# ─────────────────────────────────────────────────────────────────────


class TestParseModeJson:
    def test_plain_json(self):
        assert _parse_mode_json('{"mode": "chat", "confidence": 0.9}') == ("chat", 0.9)

    def test_fenced_json(self):
        text = '```json\n{"mode": "tool_react", "confidence": 0.8}\n```'
        assert _parse_mode_json(text) == ("tool_react", 0.8)

    def test_embedded_json(self):
        text = 'Sure! {"mode": "chat", "confidence": 0.95} done.'
        assert _parse_mode_json(text) == ("chat", 0.95)

    def test_missing_confidence_defaults_to_half(self):
        assert _parse_mode_json('{"mode": "chat"}') == ("chat", 0.5)

    def test_missing_mode_returns_none(self):
        assert _parse_mode_json('{"confidence": 0.9}') is None

    def test_invalid_json_returns_none(self):
        assert _parse_mode_json("not json") is None
        assert _parse_mode_json("") is None
        assert _parse_mode_json("{") is None

    def test_non_string_mode_returns_none(self):
        assert _parse_mode_json('{"mode": 42}') is None


# ─────────────────────────────────────────────────────────────────────
# ModeJudge.classify
# ─────────────────────────────────────────────────────────────────────


class TestModeJudgeClassify:
    @pytest.mark.asyncio
    async def test_returns_result_on_valid_json(self):
        registry = _build_registry()
        judge = ModeJudge(registry, base_url="http://x")
        fake = _ollama_response(200, '{"mode": "chat", "confidence": 0.9}')
        with _patch_post(fake):
            result = await judge.classify("hello")

        assert result is not None
        assert isinstance(result, ModeJudgeResult)
        assert result.mode == "chat"
        assert result.confidence == 0.9
        assert result.source == "llmjudge"
        assert result.latency_ms > 0

    @pytest.mark.asyncio
    async def test_returns_none_on_invalid_json(self):
        registry = _build_registry()
        judge = ModeJudge(registry, base_url="http://x")
        fake = _ollama_response(200, "totally not json")
        with _patch_post(fake):
            result = await judge.classify("hi")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_network_error(self):
        registry = _build_registry()
        judge = ModeJudge(registry, base_url="http://x", timeout_s=0.1)
        with _patch_post(httpx.ConnectError("nope")):
            result = await judge.classify("hi")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error_status(self):
        registry = _build_registry()
        judge = ModeJudge(registry, base_url="http://x")
        # raise_for_status() inside classify will turn 500 into exception
        fake = _ollama_response(500, "")
        with _patch_post(fake):
            result = await judge.classify("hi")
        assert result is None

    @pytest.mark.asyncio
    async def test_hallucination_guard_rejects_unknown_mode(self):
        """LLM picks a mode not in registry → None (defense in depth)."""
        registry = _build_registry()
        judge = ModeJudge(registry, base_url="http://x")
        fake = _ollama_response(
            200, '{"mode": "totally_fake", "confidence": 0.99}',
        )
        with _patch_post(fake):
            result = await judge.classify("hi")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_message_returns_none_without_call(self):
        """Empty / whitespace message short-circuits before LLM call."""
        registry = _build_registry()
        judge = ModeJudge(registry, base_url="http://x")
        with patch("httpx.AsyncClient.post") as mock_post:
            assert await judge.classify("") is None
            assert await judge.classify("   ") is None
            assert mock_post.call_count == 0

    @pytest.mark.asyncio
    async def test_single_mode_registry_short_circuits(self):
        """Registry with exactly one available mode → no LLM call needed.

        Trusts the registry filter — if only chat survived (e.g., all
        tool patterns failed to compile), every request goes to chat
        with confidence 1.0 and source "llmjudge:single".
        """
        # Build a fresh registry with only 1 available pattern.
        registry = SubgraphRegistry()

        class _Solo(SubgraphPattern):
            name = "chat"
            description = "only"
            category = "basic"

            def build(self, registry):  # noqa: ANN001
                raise NotImplementedError

        registry.register(_Solo())
        judge = ModeJudge(registry, base_url="http://x")

        with patch("httpx.AsyncClient.post") as mock_post:
            result = await judge.classify("anything")

        assert result is not None
        assert result.mode == "chat"
        assert result.confidence == 1.0
        assert result.source == "llmjudge:single"
        assert mock_post.call_count == 0

    @pytest.mark.asyncio
    async def test_zero_modes_returns_none(self):
        """No available patterns at all → None (supervisor falls back)."""
        registry = SubgraphRegistry()  # empty
        judge = ModeJudge(registry, base_url="http://x")
        with patch("httpx.AsyncClient.post") as mock_post:
            result = await judge.classify("anything")
        assert result is None
        assert mock_post.call_count == 0


# ─────────────────────────────────────────────────────────────────────
# Prompt construction / registry view
# ─────────────────────────────────────────────────────────────────────


class TestModeJudgePrompt:
    def test_system_prompt_includes_available_modes(self):
        """Phase 2.11: all 4 basic patterns now appear in the prompt."""
        registry = _build_registry()
        judge = ModeJudge(registry, base_url="http://x")
        assert "chat" in judge._system_prompt
        assert "tool_react" in judge._system_prompt
        assert "rag" in judge._system_prompt
        assert "plan_execute" in judge._system_prompt

    def test_system_prompt_excludes_unavailable_stubs(self):
        """The 7 multi-agent stubs should NOT appear in the prompt.

        Phase 2.11 moved rag / plan_execute out of the stub set, so
        only the 7 multi-agent patterns remain unavailable.
        """
        registry = _build_registry()
        judge = ModeJudge(registry, base_url="http://x")
        assert "debate" not in judge._system_prompt
        assert "map_reduce" not in judge._system_prompt
        assert "agent_team" not in judge._system_prompt

    def test_modes_snapshot_at_construction(self):
        """Mutating registry after ModeJudge creation doesn't change view."""
        registry = _build_registry()
        judge = ModeJudge(registry, base_url="http://x")
        # Snapshot of available modes at construct time — Phase 2.11
        # expanded this to 4 basic patterns.
        assert set(judge._modes) == {
            "chat", "tool_react", "rag", "plan_execute",
        }

        # Mutate registry after construction — judge shouldn't see it.
        class _Extra(SubgraphPattern):
            name = "extra"
            description = "added later"
            category = "basic"

            def build(self, registry):  # noqa: ANN001
                raise NotImplementedError

        registry.register(_Extra())
        # Judge still sees the old snapshot
        assert set(judge._modes) == {
            "chat", "tool_react", "rag", "plan_execute",
        }
        assert "extra" not in judge._system_prompt
