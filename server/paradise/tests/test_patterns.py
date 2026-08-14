"""Phase 2.6 — Handoff Protocol / SubgraphRegistry / SubgraphPattern tests.

Run:
    cd server && python -m pytest paradise/tests/test_patterns.py -v

Scope:
  * HandoffRequest/Response dataclass shape & defaults
  * HandoffResponse.ok property
  * SubgraphRegistry register/get/list_modes/build_mode_prompt
  * Duplicate registration rejected
  * Lazy compile memoisation
  * Compile failure → compiled=None (resilient)
  * StubPattern.availability()=False and fallback build()
  * register_all() Phase 2.6 shim is a no-op

Out of scope (later phases):
  * Real ChatPattern / ToolReactPattern (Phase 2.10)
  * Supervisor handoff_node integration (Phase 2.9)
  * mode_judge prompt assembly (Phase 2.8)
"""
from __future__ import annotations

import pytest

from paradise.core.handoff import (
    HANDOFF_COST_EXCEEDED,
    HANDOFF_DEPTH_CAPPED,
    HANDOFF_ERROR,
    HANDOFF_OK,
    HandoffError,
    HandoffRequest,
    HandoffResponse,
)
from paradise.core.patterns.base import SubgraphPattern
from paradise.core.patterns.stub import StubPattern
from paradise.core.patterns import register_all
from paradise.core.registry import SubgraphRegistry, SubgraphSpec


# ─────────────────────────────────────────────────────────────────────
# Test fixtures: minimal pattern implementations
# ─────────────────────────────────────────────────────────────────────


class _CompiledGraph:
    """Cheap stand-in for a real compiled LangGraph (avoids pulling langgraph
    into Phase 2.6 tests). Identity: instances compare by id()."""
    pass


class BasicPattern(SubgraphPattern):
    """Test-only basic pattern. Builds a new _CompiledGraph each call."""
    name = "test_basic"
    description = "test basic pattern"
    category = "basic"
    cost_budget_usd = 0.01
    max_turns = 3

    def build(self, registry):
        return _CompiledGraph()


class FailingPattern(SubgraphPattern):
    """Pattern whose build() raises — exercises registry resilience."""
    name = "failing"
    description = "always fails to compile"
    category = "basic"

    def build(self, registry):
        raise RuntimeError("simulated compile failure")


class CountingPattern(SubgraphPattern):
    """Records build() call count to verify memoisation."""
    name = "counting"
    description = "counts builds"
    category = "basic"

    def __init__(self):
        self.build_count = 0

    def build(self, registry):
        self.build_count += 1
        return _CompiledGraph()


# ─────────────────────────────────────────────────────────────────────
# HandoffRequest / HandoffResponse
# ─────────────────────────────────────────────────────────────────────


class TestHandoffRequest:
    def test_required_fields_present(self):
        req = HandoffRequest(
            session_id="s1", user_id="u1", trace_id="t1", message="hi"
        )
        assert req.session_id == "s1"
        assert req.user_id == "u1"
        assert req.trace_id == "t1"
        assert req.message == "hi"

    def test_defaults(self):
        req = HandoffRequest(session_id="s", user_id="u", trace_id="t")
        assert req.user_tier == "free"
        assert req.message == ""
        assert req.context == {}
        assert req.depth == 1                # supervisor→subgraph is depth 1
        assert req.parent_trace_id is None
        assert req.deadline_ms == 0

    def test_context_dict_isolated_between_instances(self):
        """Mutable default must not leak across instances (dataclass
        field(default_factory=dict) guard)."""
        a = HandoffRequest(session_id="a", user_id="u", trace_id="t")
        b = HandoffRequest(session_id="b", user_id="u", trace_id="t")
        a.context["k"] = "v"
        assert "k" not in b.context


class TestHandoffResponse:
    def test_ok_property_true_for_ok_status(self):
        r = HandoffResponse(session_id="s", trace_id="t")
        assert r.status == HANDOFF_OK
        assert r.ok is True

    @pytest.mark.parametrize("status", [
        HANDOFF_COST_EXCEEDED,
        HANDOFF_DEPTH_CAPPED,
        HANDOFF_ERROR,
    ])
    def test_ok_property_false_for_non_ok(self, status):
        r = HandoffResponse(session_id="s", trace_id="t", status=status)
        assert r.ok is False

    def test_defaults(self):
        r = HandoffResponse(session_id="s", trace_id="t")
        assert r.output == ""
        assert r.artifacts == {}
        assert r.cost_incurred_usd == 0.0
        assert r.turns_used == 0
        assert r.error is None


class TestHandoffError:
    def test_is_exception_subclass(self):
        assert issubclass(HandoffError, Exception)

    def test_raisable(self):
        with pytest.raises(HandoffError, match="contract"):
            raise HandoffError("contract violation")


# ─────────────────────────────────────────────────────────────────────
# SubgraphRegistry — registration & lookup
# ─────────────────────────────────────────────────────────────────────


class TestRegistryRegistration:
    def test_register_single(self):
        r = SubgraphRegistry()
        r.register(BasicPattern())
        assert "test_basic" in r
        assert len(r) == 1

    def test_register_duplicate_raises(self):
        r = SubgraphRegistry()
        r.register(BasicPattern())
        with pytest.raises(ValueError, match="already registered"):
            r.register(BasicPattern())

    def test_register_empty_name_raises(self):
        """A pattern with empty name must surface as a config error."""
        class NoName(SubgraphPattern):
            name = ""
            description = "x"
            def build(self, registry): return None

        r = SubgraphRegistry()
        with pytest.raises(ValueError, match="empty name"):
            r.register(NoName())

    def test_get_unknown_returns_none(self):
        r = SubgraphRegistry()
        assert r.get("does_not_exist") is None


class TestRegistryLazyCompile:
    def test_first_get_triggers_build(self):
        r = SubgraphRegistry()
        p = CountingPattern()
        r.register(p)
        assert p.build_count == 0
        spec = r.get("counting")
        assert spec is not None
        assert p.build_count == 1
        assert isinstance(spec.compiled, _CompiledGraph)

    def test_second_get_uses_cache(self):
        """Memoisation: subsequent get() must NOT rebuild."""
        r = SubgraphRegistry()
        p = CountingPattern()
        r.register(p)
        r.get("counting")
        r.get("counting")
        r.get("counting")
        assert p.build_count == 1

    def test_compile_failure_leaves_compiled_none(self):
        """Resilience: a pattern that fails to build does not poison
        the registry; supervisor can fall back to chat."""
        r = SubgraphRegistry()
        r.register(FailingPattern())
        spec = r.get("failing")
        assert spec is not None
        assert spec.compiled is None

    def test_compile_failure_does_not_propagate(self):
        r = SubgraphRegistry()
        r.register(FailingPattern())
        # get() should NOT raise even though build() does
        spec = r.get("failing")
        assert spec is not None

    def test_unavailable_pattern_never_compiled(self):
        """Regression: in Phase 2.7, StubPattern('chat', fallback='chat')
        is a self-referential stub. get() must NOT trigger its build(),
        otherwise infinite recursion. unavailable ⇒ skip compile."""
        r = SubgraphRegistry()
        r.register(StubPattern("selfref", "x", fallback="selfref"))
        spec = r.get("selfref")
        assert spec is not None
        assert spec.compiled is None  # not compiled because availability=False


# ─────────────────────────────────────────────────────────────────────
# SubgraphRegistry — mode listing & prompt
# ─────────────────────────────────────────────────────────────────────


class TestRegistryModeListing:
    def test_list_modes_excludes_unavailable_by_default(self):
        r = SubgraphRegistry()
        r.register(BasicPattern())
        r.register(StubPattern("stub1", "stub one"))
        modes = r.list_modes()
        assert "test_basic" in modes
        assert "stub1" not in modes

    def test_list_modes_include_unavailable(self):
        r = SubgraphRegistry()
        r.register(BasicPattern())
        r.register(StubPattern("stub1", "stub one"))
        modes = r.list_modes(include_unavailable=True)
        assert set(modes) == {"test_basic", "stub1"}

    def test_build_mode_prompt_lists_available_only(self):
        r = SubgraphRegistry()
        r.register(BasicPattern())
        r.register(StubPattern("hidden", "should not appear"))
        prompt = r.build_mode_prompt()
        assert "test_basic" in prompt
        assert "hidden" not in prompt
        assert prompt.startswith("Available modes:")

    def test_build_mode_prompt_empty_registry(self):
        r = SubgraphRegistry()
        prompt = r.build_mode_prompt()
        assert prompt == "Available modes:"

    def test_build_mode_prompt_format(self):
        r = SubgraphRegistry()
        r.register(BasicPattern())
        prompt = r.build_mode_prompt()
        # Each mode should be on its own line, prefixed with "- "
        assert "- test_basic:" in prompt


# ─────────────────────────────────────────────────────────────────────
# StubPattern
# ─────────────────────────────────────────────────────────────────────


class TestStubPattern:
    def test_availability_false(self):
        s = StubPattern("debate", "N debater + judge")
        assert s.availability() is False

    def test_category_multi_agent(self):
        s = StubPattern("debate", "x")
        assert s.category == "multi_agent"

    def test_describe_marks_unavailable(self):
        s = StubPattern("debate", "N debater + judge")
        d = s.describe()
        assert "debate" in d
        assert "接口预留" in d or "未启用" in d

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            StubPattern("", "x")

    def test_build_falls_back_to_chat(self):
        """Stub build() delegates to the fallback pattern's compiled graph."""
        r = SubgraphRegistry()
        r.register(BasicPattern())  # pretend this is "chat"
        # Patch: redirect fallback to "test_basic" since we didn't register chat
        s = StubPattern("debate", "x", fallback="test_basic")
        compiled = s.build(r)
        assert isinstance(compiled, _CompiledGraph)

    def test_build_missing_fallback_raises(self):
        """Forcing a stub build with no fallback registered is a config error."""
        r = SubgraphRegistry()
        s = StubPattern("debate", "x", fallback="nonexistent")
        with pytest.raises(KeyError, match="fallback"):
            s.build(r)

    def test_stub_hidden_from_mode_judge_via_registry(self):
        """End-to-end: a registered stub is invisible to list_modes()."""
        r = SubgraphRegistry()
        r.register(BasicPattern())
        r.register(StubPattern("map_reduce", "并行扇出"))
        modes = r.list_modes()
        assert modes == ["test_basic"]


# ─────────────────────────────────────────────────────────────────────
# register_all() — canonical 11-pattern registration
# ─────────────────────────────────────────────────────────────────────


class TestRegisterAll:
    """Phase 2.7: register_all() populates the canonical 11 patterns.

    All 11 are stubs at this phase. Phase 2.10-2.11 will replace the 4
    basic stubs with real implementations; the 7 multi-agent stubs remain
    until a concrete need arises.
    """

    def test_registers_exactly_11_patterns(self):
        r = SubgraphRegistry()
        register_all(r)
        assert len(r) == 11

    def test_all_patterns_are_unavailable_at_phase_2_7(self):
        """Until Phase 2.10 ships ChatPattern, mode_judge has zero
        selectable modes. This is the expected state."""
        r = SubgraphRegistry()
        register_all(r)
        assert r.list_modes() == []

    def test_all_11_visible_to_admin_tools(self):
        r = SubgraphRegistry()
        register_all(r)
        modes = r.list_modes(include_unavailable=True)
        assert len(modes) == 11

    @pytest.mark.parametrize("name,category", [
        ("chat",          "basic"),
        ("tool_react",    "basic"),
        ("rag",           "basic"),
        ("plan_execute",  "basic"),
        ("map_reduce",       "multi_agent"),
        ("agent_team",       "multi_agent"),
        ("chain_of_expert",  "multi_agent"),
        ("guardrail",        "multi_agent"),
        ("hitl",             "multi_agent"),
        ("debate",           "multi_agent"),
        ("reflection",       "multi_agent"),
    ])
    def test_pattern_registered_with_correct_category(self, name, category):
        """4 basic + 7 multi_agent — dashboard grouping depends on this."""
        r = SubgraphRegistry()
        register_all(r)
        spec = r.get(name)
        assert spec is not None, f"{name!r} not registered"
        assert spec.category == category, (
            f"{name!r} expected category={category!r}, got {spec.category!r}"
        )

    def test_basic_pattern_names_canonical(self):
        """The 4 basic mode names are part of the public API — they appear
        in mode_judge prompts, dashboards, and operator runbooks. Renaming
        any of them is a breaking change."""
        r = SubgraphRegistry()
        register_all(r)
        for name in ("chat", "tool_react", "rag", "plan_execute"):
            assert name in r, f"{name!r} missing from registry"

    def test_multi_agent_pattern_names_canonical(self):
        """The 7 multi_agent names mirror Kimi's sub-pattern catalogue.
        Renaming breaks documentation cross-references."""
        r = SubgraphRegistry()
        register_all(r)
        for name in ("map_reduce", "agent_team", "chain_of_expert",
                     "guardrail", "hitl", "debate", "reflection"):
            assert name in r, f"{name!r} missing from registry"

    def test_build_mode_prompt_empty_when_all_unavailable(self):
        """mode_judge prompt must not list any unavailable pattern —
        otherwise the LLM might select a stub and the supervisor will
        fail at handoff time."""
        r = SubgraphRegistry()
        register_all(r)
        prompt = r.build_mode_prompt()
        assert prompt == "Available modes:"

    def test_descriptions_non_empty(self):
        """Each pattern MUST have a meaningful description — it's fed
        verbatim into mode_judge's prompt. Empty descriptions would
        give the LLM zero signal for selection."""
        r = SubgraphRegistry()
        register_all(r)
        for spec in r:
            assert spec.pattern.description, (
                f"{spec.name!r} has empty description"
            )
            assert len(spec.pattern.description) >= 5, (
                f"{spec.name!r} description too short: {spec.pattern.description!r}"
            )

    def test_basic_patterns_have_lower_cost_budget_than_multi_agent(self):
        """Sanity check on cost caps — basic patterns are typically
        cheaper than multi-agent coordination. This isn't a hard rule,
        but the defaults in register_all() should reflect it."""
        r = SubgraphRegistry()
        register_all(r)
        chat_budget = r.get("chat").pattern.cost_budget_usd
        debate_budget = r.get("debate").pattern.cost_budget_usd
        assert chat_budget < debate_budget, (
            "basic chat should be cheaper than multi-agent debate by default"
        )

    def test_register_all_is_idempotent_safe_via_fresh_registry(self):
        """Calling register_all on an already-populated registry must
        raise (duplicate registration). This forces callers to create
        a fresh registry per process, avoiding leaks across tests."""
        r = SubgraphRegistry()
        register_all(r)
        with pytest.raises(ValueError, match="already registered"):
            register_all(r)


# ─────────────────────────────────────────────────────────────────────
# SubgraphSpec dataclass
# ─────────────────────────────────────────────────────────────────────


class TestSubgraphSpec:
    def test_defaults_at_construction(self):
        """SubgraphSpec is constructed by the registry; verify its shape."""
        p = BasicPattern()
        spec = SubgraphSpec(
            name=p.name,
            pattern=p,
            description=p.describe(),
            category=p.category,
        )
        assert spec.name == "test_basic"
        assert spec.compiled is None
        assert spec.state_schema is None
        assert spec.category == "basic"

    def test_pattern_attribute_preserved(self):
        """Spec must keep a reference to the original pattern object —
        needed by supervisor to read cost_budget / max_turns at runtime."""
        p = BasicPattern()
        spec = SubgraphSpec(name=p.name, pattern=p)
        assert spec.pattern is p
        assert spec.pattern.cost_budget_usd == 0.01
        assert spec.pattern.max_turns == 3


# ─────────────────────────────────────────────────────────────────────
# Integration: full registry lifecycle
# ─────────────────────────────────────────────────────────────────────


class TestRegistryLifecycle:
    def test_iter_yields_all_specs(self):
        r = SubgraphRegistry()
        r.register(BasicPattern())
        r.register(StubPattern("s1", "x"))
        specs = list(r)
        assert len(specs) == 2
        names = {s.name for s in specs}
        assert names == {"test_basic", "s1"}

    def test_seven_stubs_plus_one_basic_e模拟_11_patterns(self):
        """Simulate the Phase 2.7 target state: 4 basic + 7 stubs = 11.
        Here we register 1 real + 7 stubs to verify the registry handles
        the target scale without issue."""
        r = SubgraphRegistry()
        r.register(BasicPattern())
        for name in ("map_reduce", "agent_team", "chain_of_expert",
                     "guardrail", "hitl", "debate", "reflection"):
            r.register(StubPattern(name, f"stub {name}"))
        assert len(r) == 8
        # Only the basic pattern is available
        assert r.list_modes() == ["test_basic"]
        # But all 8 are visible to admin tools
        assert len(r.list_modes(include_unavailable=True)) == 8
