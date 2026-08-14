"""Phase 6 — AgentTeamPattern tests.

Verifies:
  1. Sequential mode: agents run in order, each sees prior outputs
  2. Parallel mode: agents run concurrently (one-shot)
  3. Compose JSON parse failure → single-agent fallback
  4. Sub-agent depth_capped → flat chat fallback
  5. One agent failing doesn't cancel siblings
  6. Synthesis merges agent outputs
  7. register_agent_team_pattern replaces the stub (idempotent)
  8. _parse_team_json edge cases

Run:
    cd server && python -m pytest paradise/tests/test_agent_team.py -v
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from paradise.core.handoff import HANDOFF_OK, HandoffRequest
from paradise.core.patterns import (
    AgentTeamPattern, register_all, register_basic_patterns,
    register_agent_team_pattern,
)
from paradise.core.patterns.agent_team import _parse_team_json
from paradise.core.registry import SubgraphRegistry


# ─────────────────────────────────────────────────────────────────────
# Mocks
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _MockResponse:
    content: str
    tool_calls: list | None = None
    usage: Any = None
    finish_reason: str = "stop"


class _ScriptedTransport:
    """Returns one canned response per chat() call, cycling through a list.
    Records every call for assertion."""

    api_mode = "openai_compat"

    def __init__(self, responses: list[_MockResponse] | None = None):
        self._responses = list(responses or [])
        self._idx = 0
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return self._responses[-1] if self._responses else _MockResponse("x")


class _MockLLMConfig:
    model = "test-model"
    api_url = "http://localhost:11434"
    api_key = ""
    max_tokens = 512


def _make_request(message: str = "research X then summarize") -> HandoffRequest:
    return HandoffRequest(
        session_id="sess-team-1",
        user_id="user-1",
        trace_id="trace-team-1",
        message=message,
        context={"task_goal": message, "soul_md": "helpful assistant"},
        depth=1,
    )


def _team_plan_json(mode: str = "sequential") -> str:
    import json
    return json.dumps({
        "mode": mode,
        "agents": [
            {"role": "researcher", "task": "find facts about X",
             "mode": "chat"},
            {"role": "writer", "task": "write summary of findings",
             "mode": "chat"},
            {"role": "critic", "task": "review for accuracy", "mode": "chat"},
        ],
    })


# ─────────────────────────────────────────────────────────────────────
# 1. Sequential mode
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sequential_three_agents():
    """3 agents run sequentially; agent 2 sees agent 1's output."""
    pattern_transport = _ScriptedTransport([
        _MockResponse(content=_team_plan_json("sequential")),
        _MockResponse(content="final synthesized answer"),
    ])
    # Registry chat transport: one response per sub-agent
    chat_transport = _ScriptedTransport([
        _MockResponse(content="facts about X"),
        _MockResponse(content="summary of facts"),
        _MockResponse(content="review ok"),
    ])

    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=chat_transport,
                            llm_config=_MockLLMConfig())

    pattern = AgentTeamPattern(
        transport=pattern_transport, llm_config=_MockLLMConfig(),
    )
    graph = pattern.build(registry)
    result = await graph.ainvoke({"handoff": _make_request()})

    hr = result["handoff_response"]
    assert hr.status == HANDOFF_OK
    assert hr.output == "final synthesized answer"

    artifacts = hr.artifacts
    assert artifacts["plan_used"] is True
    assert artifacts["team_plan"]["mode"] == "sequential"
    results = artifacts["agent_results"]
    assert len(results) == 3
    assert results[0]["output"] == "facts about X"
    assert results[1]["output"] == "summary of facts"
    assert results[2]["output"] == "review ok"
    assert [r["role"] for r in results] == [
        "researcher", "writer", "critic",
    ]
    # 3 sub-agent chat calls happened (registry transport)
    assert len(chat_transport.calls) == 3

    # Agent 2 (writer) saw agent 1's output via focused prompt
    writer_call = chat_transport.calls[1]
    sys_prompt = writer_call.get("system_prompt", "")
    assert "researcher" in sys_prompt or "facts about X" in sys_prompt
    # focused mode present
    assert "multi-agent team" in sys_prompt or "multi-step plan" in sys_prompt


# ─────────────────────────────────────────────────────────────────────
# 2. Parallel mode
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parallel_three_agents():
    """Parallel mode: all agents run in one execute pass."""
    pattern_transport = _ScriptedTransport([
        _MockResponse(content=_team_plan_json("parallel")),
        _MockResponse(content="merged perspectives"),
    ])
    chat_transport = _ScriptedTransport([
        _MockResponse(content="perspective A"),
        _MockResponse(content="perspective B"),
        _MockResponse(content="perspective C"),
    ])

    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=chat_transport,
                            llm_config=_MockLLMConfig())

    pattern = AgentTeamPattern(
        transport=pattern_transport, llm_config=_MockLLMConfig(),
    )
    graph = pattern.build(registry)
    result = await graph.ainvoke({"handoff": _make_request("compare A B C")})

    hr = result["handoff_response"]
    assert hr.status == HANDOFF_OK
    assert hr.output == "merged perspectives"
    results = hr.artifacts["agent_results"]
    assert len(results) == 3
    assert [r["output"] for r in results] == [
        "perspective A", "perspective B", "perspective C",
    ]
    # All 3 sub-agents called
    assert len(chat_transport.calls) == 3
    # Parallel: no agent sees prior results
    for call in chat_transport.calls:
        assert "perspective" not in call.get("system_prompt", "")


# ─────────────────────────────────────────────────────────────────────
# 3. Compose JSON parse failure → single-agent fallback
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_json_parse_failure_fallback():
    pattern_transport = _ScriptedTransport([
        _MockResponse(content="garbage not json"),
        _MockResponse(content="single agent answer"),
    ])
    chat_transport = _ScriptedTransport([
        _MockResponse(content="sub-agent output"),
    ])

    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=chat_transport,
                            llm_config=_MockLLMConfig())

    pattern = AgentTeamPattern(
        transport=pattern_transport, llm_config=_MockLLMConfig(),
    )
    graph = pattern.build(registry)
    result = await graph.ainvoke({"handoff": _make_request()})

    hr = result["handoff_response"]
    assert hr.status == HANDOFF_OK
    assert hr.output == "single agent answer"
    assert hr.artifacts["plan_used"] is False
    assert len(hr.artifacts["agent_results"]) == 1
    assert hr.artifacts["agent_results"][0]["role"] == "generalist"


# ─────────────────────────────────────────────────────────────────────
# 4. Depth capping → flat chat fallback
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_depth_capped_falls_back_to_flat_chat():
    """agent_team invoked at depth=2 → sub-agents at depth=3 → capped.

    Sub-agents must fall back to the pattern's own transport (flat
    chat), still producing output.
    """
    # Pattern transport: compose, 2 flat chats (fallbacks), synthesis
    pattern_transport = _ScriptedTransport([
        _MockResponse(content=_team_plan_json("parallel")),  # compose
        _MockResponse(content="flat answer A"),              # agent 0 fallback
        _MockResponse(content="flat answer B"),              # agent 1 fallback
        _MockResponse(content="flat answer C"),              # agent 2 fallback
        _MockResponse(content="synthesized"),                # synthesis
    ])

    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(
        registry,
        transport=_ScriptedTransport([_MockResponse(content="never called")]),
        llm_config=_MockLLMConfig(),
    )

    pattern = AgentTeamPattern(
        transport=pattern_transport, llm_config=_MockLLMConfig(),
    )
    graph = pattern.build(registry)
    # depth=2 → sub-agents would be depth=3 > _MAX_DEPTH=2 → capped
    req = HandoffRequest(
        session_id="s", user_id="u", trace_id="t-depth",
        message="test", context={}, depth=2,
    )
    result = await graph.ainvoke({"handoff": req})

    hr = result["handoff_response"]
    assert hr.status == HANDOFF_OK
    results = hr.artifacts["agent_results"]
    assert len(results) == 3
    # All fell back to flat chat (pattern transport), still got output
    for r in results:
        assert r.get("nested_fallback") is True
        assert r["output"].startswith("flat answer")


# ─────────────────────────────────────────────────────────────────────
# 5. Sub-agent failure doesn't cancel siblings
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subagent_failure_continues():
    """Parallel mode: one agent raises — siblings still complete."""
    import json as _json

    plan = _json.dumps({
        "mode": "parallel",
        "agents": [
            {"role": "a1", "task": "t1", "mode": "chat"},
            {"role": "a2", "task": "t2", "mode": "nonexistent_mode"},
            {"role": "a3", "task": "t3", "mode": "chat"},
        ],
    })
    # Note: nonexistent_mode gets coerced to "chat" by _parse_team_json.
    # So instead test a failure via a chat transport that raises.
    pattern_transport = _ScriptedTransport([
        _MockResponse(content=plan),
        _MockResponse(content="merged"),
    ])

    class _HalfBoomTransport:
        api_mode = "openai_compat"

        def __init__(self):
            self.n = 0

        async def chat(self, **kwargs):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("agent 2 exploded")
            return _MockResponse(content=f"output {self.n}")

    chat_transport = _HalfBoomTransport()

    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=chat_transport,
                            llm_config=_MockLLMConfig())

    pattern = AgentTeamPattern(
        transport=pattern_transport, llm_config=_MockLLMConfig(),
    )
    graph = pattern.build(registry)
    result = await graph.ainvoke({"handoff": _make_request()})

    hr = result["handoff_response"]
    assert hr.status == HANDOFF_OK
    results = hr.artifacts["agent_results"]
    assert len(results) == 3
    # Agent 1 & 3 produced output; agent 2 errored but didn't cancel
    assert results[0]["output"] == "output 1"
    assert results[2]["output"].startswith("output")  # got some output


# ─────────────────────────────────────────────────────────────────────
# 6. Synthesis failure → concatenation fallback
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesis_failure_concatenates():
    """Synthesis LLM failure degrades to concatenated agent outputs."""
    import json as _json
    plan = _json.dumps({
        "mode": "parallel",
        "agents": [
            {"role": "r1", "task": "t1", "mode": "chat"},
        ],
    })
    class _BoomOnSecondCall:
        api_mode = "openai_compat"

        def __init__(self):
            self.n = 0

        async def chat(self, **kwargs):
            self.n += 1
            if self.n >= 2:  # synthesis is the pattern transport's 2nd call
                raise RuntimeError("synthesis exploded")
            return _MockResponse(content=plan)

    pattern_transport = _BoomOnSecondCall()
    chat_transport = _ScriptedTransport([
        _MockResponse(content="agent produced this"),
    ])

    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(registry, transport=chat_transport,
                            llm_config=_MockLLMConfig())

    pattern = AgentTeamPattern(
        transport=pattern_transport, llm_config=_MockLLMConfig(),
    )
    graph = pattern.build(registry)
    result = await graph.ainvoke({"handoff": _make_request()})

    hr = result["handoff_response"]
    # Degraded but successful — agent output preserved
    assert hr.status == HANDOFF_OK
    assert "agent produced this" in hr.output


# ─────────────────────────────────────────────────────────────────────
# 7. Registration
# ─────────────────────────────────────────────────────────────────────


def test_register_agent_team_pattern_replaces_stub():
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(
        registry,
        transport=_ScriptedTransport([_MockResponse(content="x")]),
        llm_config=_MockLLMConfig(),
    )

    # Stub not in available modes
    assert "agent_team" not in registry.list_modes()

    register_agent_team_pattern(
        registry,
        transport=_ScriptedTransport([_MockResponse(content="x")]),
        llm_config=_MockLLMConfig(),
    )

    assert "agent_team" in registry.list_modes()
    spec = registry.get("agent_team")
    assert spec is not None
    assert isinstance(spec.pattern, AgentTeamPattern)


def test_register_agent_team_pattern_idempotent():
    registry = SubgraphRegistry()
    register_all(registry)
    register_basic_patterns(
        registry,
        transport=_ScriptedTransport([_MockResponse(content="x")]),
        llm_config=_MockLLMConfig(),
    )

    register_agent_team_pattern(
        registry, transport=_ScriptedTransport([_MockResponse(content="x")]),
        llm_config=_MockLLMConfig(),
    )
    # Second call replaces, doesn't crash
    register_agent_team_pattern(
        registry, transport=_ScriptedTransport([_MockResponse(content="x")]),
        llm_config=_MockLLMConfig(), max_agents=2,
    )
    spec = registry.get("agent_team")
    assert spec.pattern._max_agents == 2


# ─────────────────────────────────────────────────────────────────────
# 8. _parse_team_json edge cases
# ─────────────────────────────────────────────────────────────────────


def test_parse_team_json_valid():
    plan = _parse_team_json(_team_plan_json("sequential"), "orig", 4)
    assert plan is not None
    assert plan["mode"] == "sequential"
    assert len(plan["agents"]) == 3
    assert plan["agents"][0]["role"] == "researcher"


def test_parse_team_json_caps_agents():
    import json as _json
    raw = _json.dumps({
        "mode": "parallel",
        "agents": [
            {"role": f"a{i}", "task": f"t{i}", "mode": "chat"}
            for i in range(10)
        ],
    })
    plan = _parse_team_json(raw, "orig", 4)
    assert plan is not None
    assert len(plan["agents"]) == 4  # capped


def test_parse_team_json_coerces_invalid_mode():
    import json as _json
    raw = _json.dumps({
        "mode": "wild_new_mode",
        "agents": [{"role": "a", "task": "t", "mode": "weird"}],
    })
    plan = _parse_team_json(raw, "orig", 4)
    assert plan["mode"] == "sequential"  # coerced
    assert plan["agents"][0]["mode"] == "chat"  # coerced


def test_parse_team_json_empty_agents_returns_none():
    assert _parse_team_json('{"mode": "sequential", "agents": []}', "o", 4) is None
    assert _parse_team_json("not json", "o", 4) is None
    assert _parse_team_json("", "o", 4) is None


def test_parse_team_json_drops_taskless_agents():
    import json as _json
    raw = _json.dumps({
        "mode": "sequential",
        "agents": [
            {"role": "ok", "task": "real task", "mode": "chat"},
            {"role": "no_task", "mode": "chat"},
        ],
    })
    plan = _parse_team_json(raw, "orig", 4)
    assert len(plan["agents"]) == 1
    assert plan["agents"][0]["role"] == "ok"
