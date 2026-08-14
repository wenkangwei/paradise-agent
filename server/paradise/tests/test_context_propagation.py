"""Phase 3-D — context propagation tests.

Verifies that:
  1. build_subgraph_system_prompt injects persona/memory/profile/compact
  2. extract_history_messages caps + strips trailing user message
  3. focused_mode produces a focused prompt (no history)
  4. _build_handoff_request packs context bags into HandoffRequest.context
  5. chat._build_kwargs includes history in messages
  6. tool_react seeds messages with history on first iteration
  7. plan_execute _run_nested_step passes focused context to nested sub-agent

Run:
    cd server && python -m pytest paradise/tests/test_context_propagation.py -v
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from paradise.core.handoff import HandoffRequest, HandoffResponse, HANDOFF_OK
from paradise.core.patterns.context_builder import (
    build_subgraph_system_prompt,
    extract_history_messages,
)
from paradise.core.patterns.chat import ChatPattern, _CHAT_SYSTEM_PROMPT
from paradise.core.patterns.plan_execute import PlanExecutePattern
from paradise.core.supervisor import SupervisorState, _build_handoff_request


# ─────────────────────────────────────────────────────────────────────
# 1. build_subgraph_system_prompt
# ─────────────────────────────────────────────────────────────────────


class TestBuildSubgraphSystemPrompt:
    def test_empty_context_returns_base_prompt(self):
        """No context → base prompt unchanged (backward compat)."""
        req = HandoffRequest(session_id="s", user_id="u", trace_id="t",
                             message="hi")
        result = build_subgraph_system_prompt(req, "BASE")
        assert result == "BASE"

    def test_persona_injected(self):
        """soul_md appears in the system prompt."""
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="hi",
            context={"soul_md": "You are a cat assistant."},
        )
        result = build_subgraph_system_prompt(req, "BASE")
        assert "cat assistant" in result
        assert "BASE" in result
        assert "cat assistant" in result.split("BASE")[0]  # prefix

    def test_memory_injected(self):
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="hi",
            context={"memory_md": "User likes Python."},
        )
        result = build_subgraph_system_prompt(req, "BASE")
        assert "Python" in result

    def test_user_profile_injected(self):
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="hi",
            context={"user_profile": "Name: Alice"},
        )
        result = build_subgraph_system_prompt(req, "BASE")
        assert "Alice" in result

    def test_compact_context_injected(self):
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="hi",
            context={"compact_context": "Earlier we discussed cats."},
        )
        result = build_subgraph_system_prompt(req, "BASE")
        assert "cats" in result

    def test_all_fields_combined(self):
        """All non-empty fields appear in order before base."""
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="hi",
            context={
                "soul_md": "PERSONA",
                "memory_md": "MEMORY",
                "user_profile": "PROFILE",
                "compact_context": "COMPACT",
            },
        )
        result = build_subgraph_system_prompt(req, "BASE")
        assert "PERSONA" in result
        assert "MEMORY" in result
        assert "PROFILE" in result
        assert "COMPACT" in result
        assert "BASE" in result
        # Persona should come before base
        assert result.index("PERSONA") < result.index("BASE")


# ─────────────────────────────────────────────────────────────────────
# 2. extract_history_messages
# ─────────────────────────────────────────────────────────────────────


class TestExtractHistoryMessages:
    def test_no_history_returns_empty(self):
        req = HandoffRequest(session_id="s", user_id="u", trace_id="t",
                             message="hi")
        assert extract_history_messages(req) == []

    def test_history_returned(self):
        """Multi-turn history is extracted."""
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="next",
            context={"conversation_history": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "next"},
            ]},
        )
        history = extract_history_messages(req)
        # Trailing user message is stripped (subgraph adds its own)
        assert len(history) == 2
        assert history[0]["content"] == "hello"
        assert history[1]["content"] == "hi there"

    def test_history_capped_to_max_turns(self):
        """History longer than max_turns is capped to last N."""
        msgs = []
        for i in range(20):
            msgs.append({"role": "user", "content": f"u{i}"})
            msgs.append({"role": "assistant", "content": f"a{i}"})
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="next",
            context={"conversation_history": msgs},
        )
        history = extract_history_messages(req, max_turns=5)
        assert len(history) <= 5

    def test_filtered_roles(self):
        """Only user/assistant messages pass through (no tool/system)."""
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="next",
            context={"conversation_history": [
                {"role": "system", "content": "sys msg"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "tool", "content": "tool result"},
            ]},
        )
        history = extract_history_messages(req)
        assert len(history) == 2  # only user + assistant

    def test_focused_mode_returns_empty(self):
        """focused_mode suppresses history (nested sub-agent)."""
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="step1",
            context={
                "focused_mode": True,
                "conversation_history": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
            },
        )
        assert extract_history_messages(req) == []


# ─────────────────────────────────────────────────────────────────────
# 3. Focused mode system prompt (plan_execute nested)
# ─────────────────────────────────────────────────────────────────────


class TestFocusedModePrompt:
    def test_focused_prompt_includes_task_goal(self):
        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t",
            message="Find all Python files",
            context={
                "focused_mode": True,
                "task_goal": "Audit the codebase",
                "prior_results": ["Found 10 files in src/"],
                "parent_step": 1,
                "soul_md": "You are a code auditor.",
            },
        )
        result = build_subgraph_system_prompt(req, "BASE")
        assert "step 2" in result.lower()  # parent_step=1 → step 2
        assert "Audit the codebase" in result
        assert "Found 10 files" in result
        assert "code auditor" in result
        assert "Find all Python files" in result
        assert "BASE" in result


# ─────────────────────────────────────────────────────────────────────
# 4. _build_handoff_request packs context
# ─────────────────────────────────────────────────────────────────────


class TestBuildHandoffRequestContext:
    def test_context_bags_packed(self):
        """_build_handoff_request packs all context bags into context dict."""
        state: SupervisorState = {
            "user_message": "hello",
            "session_id": "s1",
            "trace_id": "t1",
            "user_id": "u1",
            "user_tier": "pro",
            "conversation_history": [{"role": "user", "content": "prior"}],
            "compact_context": "compacted summary",
            "soul_md": "PERSONA",
            "memory_md": "MEMORY",
            "user_profile": "Alice",
        }
        req = _build_handoff_request(state)
        assert req.message == "hello"
        assert req.context["conversation_history"] == [{"role": "user", "content": "prior"}]
        assert req.context["compact_context"] == "compacted summary"
        assert req.context["soul_md"] == "PERSONA"
        assert req.context["memory_md"] == "MEMORY"
        assert req.context["user_profile"] == "Alice"
        assert req.context["task_goal"] == "hello"

    def test_empty_state_safe_defaults(self):
        """Empty/missing fields don't crash — defaults to empty."""
        state: SupervisorState = {
            "user_message": "hi",
            "session_id": "s",
            "trace_id": "t",
        }
        req = _build_handoff_request(state)
        assert req.context["conversation_history"] == []
        assert req.context["soul_md"] == ""
        assert req.context["task_goal"] == "hi"


# ─────────────────────────────────────────────────────────────────────
# 5. chat._build_kwargs uses context
# ─────────────────────────────────────────────────────────────────────


class TestChatBuildKwargsContext:
    def test_messages_include_history(self):
        """chat._build_kwargs prepends history before the user message."""
        from paradise.config import LLMConfig
        cfg = LLMConfig(model="test-model", api_url="http://x", api_key="k")

        req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t", message="next q",
            context={
                "conversation_history": [
                    {"role": "user", "content": "prev q"},
                    {"role": "assistant", "content": "prev a"},
                ],
                "soul_md": "PERSONA",
            },
        )
        pattern = ChatPattern()
        kwargs = pattern._build_kwargs(cfg, req)

        # History should appear before the current user message
        msgs = kwargs["messages"]
        assert len(msgs) == 3  # 2 history + 1 current
        assert msgs[0]["content"] == "prev q"
        assert msgs[1]["content"] == "prev a"
        assert msgs[2]["content"] == "next q"

        # Persona injected into system prompt
        assert "PERSONA" in kwargs["system_prompt"]

    def test_no_context_backward_compat(self):
        """Empty context → single user message, bare system prompt."""
        from paradise.config import LLMConfig
        cfg = LLMConfig(model="m", api_url="http://x", api_key="k")

        req = HandoffRequest(session_id="s", user_id="u", trace_id="t",
                             message="hi")
        pattern = ChatPattern()
        kwargs = pattern._build_kwargs(cfg, req)
        assert len(kwargs["messages"]) == 1
        assert kwargs["messages"][0]["content"] == "hi"
        assert kwargs["system_prompt"] == _CHAT_SYSTEM_PROMPT


# ─────────────────────────────────────────────────────────────────────
# 6. plan_execute _run_nested_step passes focused context
# ─────────────────────────────────────────────────────────────────────


class TestPlanExecuteNestedContext:
    @pytest.mark.asyncio
    async def test_nested_step_carries_focused_context(self):
        """_run_nested_step builds a HandoffRequest with focused_mode=True,
        task_goal, prior_results, and soul_md from the parent context."""
        from paradise.core.registry import SubgraphRegistry
        from paradise.core.handoff import HandoffResponse, HANDOFF_OK

        captured_req: list[HandoffRequest] = []

        async def mock_invoke_subgraph(registry, mode, req):
            captured_req.append(req)
            return HandoffResponse(
                session_id=req.session_id,
                trace_id=req.trace_id,
                output="step result",
                status=HANDOFF_OK,
            )

        parent_req = HandoffRequest(
            session_id="s", user_id="u", trace_id="t1", message="audit code",
            context={
                "task_goal": "Audit the codebase",
                "soul_md": "PERSONA",
                "conversation_history": [
                    {"role": "user", "content": "old chat"},
                ],
            },
            depth=1,
        )
        prior_results = [{"step": "s0", "output": "found 10 files"}]

        pattern = PlanExecutePattern(nested_mode="tool_react")
        pattern._registry = SubgraphRegistry()

        with patch("paradise.core.supervisor.invoke_subgraph",
                   side_effect=mock_invoke_subgraph):
            content, fallback, error = await pattern._run_nested_step(
                parent_req, "Find Python files", 1, prior_results,
            )

        assert error is False
        assert fallback is False
        assert content == "step result"

        # Verify the nested request has focused context
        nested = captured_req[0]
        assert nested.message == "Find Python files"
        assert nested.context["focused_mode"] is True
        assert nested.context["task_goal"] == "Audit the codebase"
        assert nested.context["soul_md"] == "PERSONA"
        assert "found 10 files" in nested.context["prior_results"]
        assert nested.depth == 2  # parent depth=1 → nested depth=2
