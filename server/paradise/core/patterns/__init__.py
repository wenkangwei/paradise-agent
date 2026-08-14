"""Pattern package — agent execution modes.

Public API:
    SubgraphPattern       — ABC for all patterns (base.py)
    StubPattern           — placeholder for unavailable patterns (stub.py)
    ChatPattern           — single-shot LLM reply (chat.py)             [Phase 2.10]
    ToolReactPattern      — ReAct loop with tool calling (tool_react.py) [Phase 2.10]
    RagPattern            — retrieval-augmented generation (rag.py)      [Phase 2.11]
    PlanExecutePattern    — plan-then-execute (plan_execute.py)         [Phase 2.11]
    register_all()        — populate a registry with the canonical 11 patterns
    register_basic_patterns() — wire real basic patterns into a registry
                                (replaces the corresponding stubs)

Phase 2.11 changes:
  * RagPattern + PlanExecutePattern ship. register_basic_patterns now
    accepts an optional retriever= kwarg for RagPattern injection;
    when omitted, RagPattern lazy-resolves via qdrant_provider and
    degrades to chat-fallback if qdrant is unavailable.
  * After register_basic_patterns, list_modes() returns the 4 basic
    patterns: chat / plan_execute / rag / tool_react.

Phase 2.10 changes:
  * ChatPattern + ToolReactPattern shipped — fully functional when
    constructed with explicit transport + tool_registry.
"""
from __future__ import annotations

from paradise.core.patterns.base import SubgraphPattern
from paradise.core.patterns.stub import StubPattern
from paradise.core.patterns.chat import ChatPattern, ChatState
from paradise.core.patterns.tool_react import ToolReactPattern, ToolReactState
from paradise.core.patterns.rag import RagPattern, RagState, Retriever
from paradise.core.patterns.plan_execute import (
    PlanExecutePattern,
    PlanExecuteState,
)
from paradise.core.patterns.judge import JudgePattern, JudgeState
from paradise.core.patterns.agent_team import AgentTeamPattern, AgentTeamState

# Re-export handoff so callers can do `from paradise.core.patterns import ...`
# in one place. The handoff module itself is the source of truth.
from paradise.core.handoff import (
    HandoffError,
    HandoffRequest,
    HandoffResponse,
    HANDOFF_OK,
)


def register_all(registry) -> None:
    """Register the canonical 11 patterns into `registry`.

    Phase 2.7 state: all 11 slots are stubs. The 4 basic patterns
    (chat / tool_react / rag / plan_execute) are stubbed with
    category="basic" so dashboards group them correctly; their real
    implementations land in Phase 2.10 (chat + tool_react) and Phase 2.11
    (rag + plan_execute). The 7 multi-agent patterns remain stubs until
    a concrete use case forces implementation.

    Replacement strategy for Phase 2.10+:
        # In paradise.core.patterns.chat:
        class ChatPattern(SubgraphPattern):
            name = "chat"
            ...
        # In register_all():
        registry.register(ChatPattern())   # replaces the stub by name
        # — but register() rejects duplicates, so the stub line above
        # must be deleted when ChatPattern ships.

    Until then, list_modes() returns [] because no pattern is available,
    which is correct: the supervisor (Phase 2.9) cannot route without a
    real ChatPattern to fall back to.
    """
    # ── 4 basic patterns (stubbed until Phase 2.10-2.11) ────────────
    # fallback targets don't exist yet — that's fine because availability()
    # is False, so mode_judge cannot select these and supervisor.handoff_node
    # will never call .build(). The fallback would only be hit if someone
    # forces a build via registry.get(name).compiled.
    registry.register(StubPattern(
        name="chat",
        description="单次 LLM 回复 — 闲聊 / 简单 Q&A / 通用兜底",
        fallback="chat",          # self-reference: replaced by ChatPattern in 2.10
        category="basic",
        cost_budget_usd=0.01,
        max_turns=1,
    ))
    registry.register(StubPattern(
        name="tool_react",
        description="ReAct 循环 — 工具调用 / 用户请求执行操作",
        fallback="chat",
        category="basic",
        cost_budget_usd=0.05,
        max_turns=3,
    ))
    registry.register(StubPattern(
        name="rag",
        description="检索增强 — 知识查询 / 文档资料询问",
        fallback="chat",
        category="basic",
        cost_budget_usd=0.03,
        max_turns=2,
    ))
    registry.register(StubPattern(
        name="plan_execute",
        description="Plan-then-Execute — 多步任务 / '先 X 再 Y'",
        fallback="chat",
        category="basic",
        cost_budget_usd=0.10,
        max_turns=5,
    ))

    # ── 7 multi-agent patterns (indefinite stubs) ──────────────────
    # Registered so the API surface is stable from day 1; each can be
    # implemented independently when a real use case arises by subclassing
    # SubgraphPattern and replacing the corresponding StubPattern line.
    registry.register(StubPattern(
        name="map_reduce",
        description="并行扇出 N 个 worker + 汇总 — 大批量同构任务",
    ))
    registry.register(StubPattern(
        name="agent_team",
        description="多角色协作 — 研究员/ writer / critic 角色分工",
    ))
    registry.register(StubPattern(
        name="chain_of_expert",
        description="专家链 — 前一专家输出作为后一专家输入",
    ))
    registry.register(StubPattern(
        name="guardrail",
        description="执行者 + 批评者双 agent — 高风险输出校验",
    ))
    registry.register(StubPattern(
        name="hitl",
        description="Human-in-the-loop — 关键决策人工审批门",
    ))
    registry.register(StubPattern(
        name="debate",
        description="N 个辩手 + 1 个评委 — 多视角对抗推理",
    ))
    registry.register(StubPattern(
        name="reflection",
        description="生成者 + 自我批评循环 — 质量打磨",
    ))


def register_basic_patterns(
    registry,
    transport=None,
    llm_config=None,
    tool_registry=None,
    retriever=None,
    plan_nested_mode=None,
) -> None:
    """Replace the 4 basic-pattern stubs with real implementations.

    Phase 2.11 entry point. Called by the supervisor (Phase 2.9) at
    startup AFTER register_all() has populated the 11 stubs. Removes
    the chat / tool_react / rag / plan_execute stubs by name and
    registers the real patterns.

    Args:
        registry: the SubgraphRegistry to mutate. MUST already have the
            11 stubs registered via register_all().
        transport: LLM transport for all 4 patterns (typically shared).
        llm_config: model config. When None, patterns lazy-resolve at
            invoke time via ParadiseConfig.
        tool_registry: ToolRegistry for ToolReactPattern. When None,
            ToolReactPattern lazy-resolves via the global singleton.
        retriever: Retriever instance for RagPattern. When None,
            RagPattern lazy-resolves via qdrant_provider and degrades
            to chat-fallback if qdrant is unavailable.
        plan_nested_mode: Phase 2.12 — when set (typically "tool_react"),
            PlanExecutePattern delegates each step to that subgraph via
            invoke_subgraph (depth+1). None = flat chat per step
            (Phase 2.11 behavior).

    Raises:
        ValueError: if any of the 4 basic names aren't registered
            (caller forgot register_all), or are already real patterns.

    Typical supervisor wiring:
        registry = SubgraphRegistry()
        register_all(registry)
        register_basic_patterns(
            registry,
            transport=build_transport(config),
            tool_registry=global_tool_registry,
            retriever=qdrant_retriever,         # optional
            plan_nested_mode="tool_react",      # Phase 2.12 nesting
        )
    """
    # Deregister stubs first. We reach into _patterns rather than adding
    # a public deregister() method because this is the only legitimate
    # use case for removal — once a pattern is registered, it's meant to
    # stay. Adding a public API would invite abuse.
    for name in ("chat", "tool_react", "rag", "plan_execute"):
        if name not in registry:
            raise ValueError(
                f"register_basic_patterns: {name!r} not registered — "
                f"did you call register_all() first?"
            )
        existing_pattern = registry._patterns[name].pattern
        if not isinstance(existing_pattern, StubPattern):
            raise ValueError(
                f"register_basic_patterns: {name!r} is already a real "
                f"pattern ({type(existing_pattern).__name__}); "
                f"refusing to replace. Reset the registry first."
            )
        del registry._patterns[name]

    registry.register(ChatPattern(
        transport=transport,
        llm_config=llm_config,
    ))
    registry.register(ToolReactPattern(
        transport=transport,
        llm_config=llm_config,
        tool_registry=tool_registry,
    ))
    registry.register(RagPattern(
        transport=transport,
        llm_config=llm_config,
        retriever=retriever,
    ))
    registry.register(PlanExecutePattern(
        transport=transport,
        llm_config=llm_config,
        nested_mode=plan_nested_mode,
    ))


def register_judge_pattern(
    registry,
    transport=None,
    llm_config=None,
    judge_transport=None,
    judge_llm_config=None,
    threshold: float = 0.6,
    max_retries: int = 1,
) -> None:
    """Register the JudgePattern into ``registry``.

    Called by the factory (or tests) when ``config.reflection.judge_enabled``
    is True. Adds the pattern by name "judge" — if a stub or prior
    registration exists, it is removed first (idempotent).

    Unlike register_basic_patterns, this does NOT require register_all()
    to have been called first — it works standalone. But in practice the
    factory always calls register_all → register_basic_patterns →
    register_judge_pattern (when enabled).

    Args:
        registry: the SubgraphRegistry to mutate.
        transport: generator LLM transport. None → lazy resolve.
        llm_config: generator model config. None → lazy resolve.
        judge_transport: judge LLM transport. None → reuse ``transport``.
        judge_llm_config: judge model config. None → config.reflection.llm.
        threshold: score below this triggers regeneration.
        max_retries: max regeneration attempts on low score.
    """
    # Remove existing registration if present (stub or prior call)
    if "judge" in registry:
        del registry._patterns["judge"]

    registry.register(JudgePattern(
        transport=transport,
        llm_config=llm_config,
        judge_transport=judge_transport,
        judge_llm_config=judge_llm_config,
        threshold=threshold,
        max_retries=max_retries,
    ))


def register_agent_team_pattern(
    registry,
    transport=None,
    llm_config=None,
    max_agents: int = 4,
) -> None:
    """Register the AgentTeamPattern into ``registry``.

    Phase 6: called by the factory (or tests) when
    ``config.agent_team.enabled`` is True. Replaces the "agent_team"
    stub registered by register_all — idempotent, existing
    registration (stub or prior call) is removed first.

    Once registered, the pattern becomes available to the complexity
    router: complex turns are routed here instead of plan_execute.

    Args:
        registry: the SubgraphRegistry to mutate.
        transport: LLM transport for compose / synthesize / fallback
            calls. Sub-agent transports resolve per-agent from the
            registry. None → lazy resolve.
        llm_config: model config. None → lazy resolve.
        max_agents: hard cap on team size per turn (default 4).
    """
    if "agent_team" in registry:
        del registry._patterns["agent_team"]

    registry.register(AgentTeamPattern(
        transport=transport,
        llm_config=llm_config,
        max_agents=max_agents,
    ))


__all__ = [
    "SubgraphPattern",
    "StubPattern",
    "ChatPattern",
    "ChatState",
    "ToolReactPattern",
    "ToolReactState",
    "RagPattern",
    "RagState",
    "Retriever",
    "PlanExecutePattern",
    "PlanExecuteState",
    "JudgePattern",
    "JudgeState",
    "AgentTeamPattern",
    "AgentTeamState",
    "HandoffRequest",
    "HandoffResponse",
    "HandoffError",
    "HANDOFF_OK",
    "register_all",
    "register_basic_patterns",
    "register_judge_pattern",
    "register_agent_team_pattern",
]
