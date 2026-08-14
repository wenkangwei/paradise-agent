"""AgentTeamPattern — LLM-composed multi-agent orchestration.

Implements the "agent编排流程" subflow from the reference architecture
diagram (agent-flow.png):

    1. agent编排模式选取        → compose_node picks sequential/parallel
    2. 上下文handoff            → _run_agent packs focused context per agent
    3. 子图初始参数生产          → compose_node LLM generates the team
                                   (agent count + per-agent role/task/mode)
    4. agent子图初始化          → execute_node builds child HandoffRequests
    5. 子图执行                 → invoke_subgraph at depth+1 (chat /
                                   tool_react / rag available per agent)
    6. 子图释放                 → synthesize_node merges + emits response

Graph topology (3 nodes):

    compose → execute (sequential: self-loop per agent;
                       parallel: one-shot asyncio.gather) → synthesize → END

Design choices:
  * The team composition (mode + roles + per-agent tasks) is generated
    by ONE LLM call in compose_node. JSON parse failure collapses to a
    single generalist agent — the pattern always runs end-to-end.
  * Sub-agents are real registered subgraphs invoked via
    ``invoke_subgraph`` at depth=req.depth+1. agent_team runs at
    depth=1, so its sub-agents run at depth=2 (exactly at
    supervisor._MAX_DEPTH; they cannot recurse further).
  * Sequential mode is a pipeline: agent N sees agents 0..N-1's
    outputs via context["prior_results"] (researcher → writer → critic).
  * Parallel mode is map-reduce: all agents run concurrently with no
    cross-visibility; synthesize merges their outputs.
  * Fail-soft at every layer: a failing sub-agent (depth_capped,
    error, exception) falls back to a direct transport.chat call with
    a role-focused prompt (``_flat_chat``) so the user sees partial
    progress rather than an abort. One agent's failure never cancels
    its siblings.
  * NOT registered by default. Registered via
    ``register_agent_team_pattern()`` when ``config.agent_team.enabled``
    is True — the complexity router then sends complex queries here.

Reference: /mnt/c/Users/wenka/Desktop/agent-flow.png
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict

from langgraph.graph import END, StateGraph

from paradise.core.handoff import (
    HANDOFF_ERROR,
    HANDOFF_OK,
    HandoffRequest,
    HandoffResponse,
)
from paradise.core.patterns.base import SubgraphPattern

if TYPE_CHECKING:
    from paradise.config import LLMConfig
    from paradise.core.registry import SubgraphRegistry

logger = logging.getLogger("paradise.patterns.agent_team")


_COMPOSE_SYSTEM_PROMPT = (
    "You design a team of specialized AI agents to handle a user request.\n"
    "Output ONLY JSON:\n"
    '{{"mode": "sequential" or "parallel", "agents": [\n'
    '  {{"role": "researcher", "task": "...", "mode": "chat" or "tool_react"}},\n'
    "  ...\n"
    "]}}\n"
    "Rules:\n"
    "- sequential: agents run in order, each agent sees prior agents' "
    "output. Use for pipelines like analyze → summarize → review.\n"
    "- parallel: agents run concurrently on the same request from "
    "different angles. Use for multi-perspective comparison.\n"
    "- 1 to {max_agents} agents. Roles must be distinct and complementary.\n"
    '- mode "chat" for reasoning/writing/reviewing; "tool_react" when the '
    "agent needs tools or retrieval.\n"
    "- Each task is one concrete instruction for that agent (1 sentence).\n"
    "- Write tasks in the same language as the user request.\n"
    "- For genuinely simple requests, use a single agent with mode chat.\n"
    "- No commentary, no markdown, just the JSON."
)

_SYNTHESIZE_SYSTEM_PROMPT = (
    "You merge the outputs of a team of specialized agents into a single "
    "coherent answer for the user. Preserve all factual content from the "
    "agents' outputs; resolve contradictions in favor of the majority or "
    "the critic/reviewer; remove redundancy; reflow into natural prose. "
    "Do not invent new information."
)

_ROLE_CHAT_SYSTEM_PROMPT = (
    "You are a focused assistant handling one part of a larger task. "
    "Reply directly and concisely."
)


class AgentTeamState(TypedDict, total=False):
    """Agent team orchestration subgraph state.

    Fields:
        handoff:          incoming request.
        team_plan:        {"mode": "sequential"|"parallel",
                           "agents": [{"role","task","mode"}, ...]}.
                          Set by compose_node. On parse failure a
                          single-agent plan is substituted (plan_used=False).
        agent_results:    list of {"role","task","output","error?",
                          "nested_fallback?"} dicts, appended per agent.
        iterations:       execute_node invocations completed (sequential
                          mode increments by 1; parallel sets len(agents)).
        plan_used:        True iff compose_node produced a real LLM plan.
        handoff_response: emitted by synthesize_node.
    """
    handoff: HandoffRequest
    team_plan: dict
    agent_results: list[dict]
    iterations: int
    plan_used: bool
    handoff_response: HandoffResponse


class AgentTeamPattern(SubgraphPattern):
    """LLM-composed multi-agent orchestration (sequential / parallel)."""

    name: ClassVar[str] = "agent_team"
    description: ClassVar[str] = (
        "多角色协作编排 — LLM 生成团队组成，顺序/并行执行子 agent，"
        "适合需要多步拆解或多视角的任务"
    )
    category: ClassVar[str] = "multi_agent"
    cost_budget_usd: ClassVar[float] = 0.15
    max_turns: ClassVar[int] = 8  # compose + ≤4 agents + synth

    def __init__(
        self,
        transport: Any | None = None,
        llm_config: "LLMConfig | None" = None,
        max_agents: int = 4,
    ) -> None:
        """
        Args:
            transport: LLM transport for compose / synthesize / fallback
                calls. Sub-agent transports are resolved per-agent from
                the registry. None → lazy resolve via factory.
            llm_config: model config. None → lazy resolve.
            max_agents: hard cap on team size (1-4 typical).
        """
        self._transport = transport
        self._llm_config = llm_config
        self._max_agents = max(1, max_agents)
        self._registry: "SubgraphRegistry | None" = None

    def availability(self) -> bool:
        """Real pattern — always available once registered."""
        return True

    def build(self, registry: "SubgraphRegistry") -> Any:
        """Compile the compose → execute → synthesize graph."""
        self._registry = registry
        pattern = self

        # ── Node: COMPOSE (reference steps 1-3) ─────────────────────
        async def compose_node(state: AgentTeamState) -> dict:
            """One LLM call → team composition JSON.

            Fail-soft: parse failure / transport failure / empty agents
            all collapse to a single generalist chat agent so the
            pattern still produces output.
            """
            req: HandoffRequest = state["handoff"]
            try:
                transport = pattern._resolve_transport()
                cfg = pattern._resolve_llm_config()
                kwargs = pattern._build_compose_kwargs(cfg, req)
                result = await transport.chat(**kwargs)
                content = (
                    result.content if hasattr(result, "content")
                    else str(result)
                )
                plan = _parse_team_json(content, req.message,
                                        pattern._max_agents)
                plan_used = True
            except Exception as exc:
                logger.warning(
                    "agent_team compose failed (%s) — single-agent "
                    "fallback trace=%s", exc, req.trace_id,
                )
                plan = None
                plan_used = False

            if not plan or not plan.get("agents"):
                plan = {
                    "mode": "sequential",
                    "agents": [{
                        "role": "generalist",
                        "task": req.message,
                        "mode": "chat",
                    }],
                }
                plan_used = False

            logger.info(
                "agent_team composed trace=%s mode=%s agents=%d "
                "plan_used=%s roles=%s",
                req.trace_id, plan.get("mode"), len(plan["agents"]),
                plan_used,
                [a.get("role") for a in plan["agents"]],
            )
            return {
                "team_plan": plan,
                "plan_used": plan_used,
                "agent_results": [],
                "iterations": 0,
            }

        # ── Node: EXECUTE (reference steps 4-5) ─────────────────────
        async def execute_node(state: AgentTeamState) -> dict:
            """Run team agents.

            Parallel: all agents in one asyncio.gather — no self-loop.
            Sequential: one agent per invocation, self-loops until the
            team is exhausted (bounded by max_turns).
            """
            req: HandoffRequest = state["handoff"]
            plan: dict = state.get("team_plan") or {}
            mode: str = plan.get("mode", "sequential")
            agents: list[dict] = plan.get("agents") or []
            results: list[dict] = list(state.get("agent_results") or [])
            iteration: int = state.get("iterations", 0)

            if not agents:
                return {}

            # ── Parallel branch: one-shot fan-out ────────────────────
            if mode == "parallel":
                prior: list[str] = []  # no cross-visibility in parallel
                tasks = [
                    pattern._run_agent(req, agent, idx, prior)
                    for idx, agent in enumerate(agents)
                ]
                gathered = await asyncio.gather(*tasks, return_exceptions=True)
                results = []
                for agent, outcome in zip(agents, gathered):
                    results.append(
                        _outcome_to_result(agent, outcome, pattern, req)
                    )
                logger.info(
                    "agent_team parallel done trace=%s agents=%d",
                    req.trace_id, len(results),
                )
                return {"agent_results": results, "iterations": len(results)}

            # ── Sequential branch: one agent per self-loop pass ──────
            # Bounded: leave room for the synthesis call (compose and
            # synthesize each consumed one turn of max_turns).
            max_executions = max(1, pattern.max_turns - 2)
            if iteration >= len(agents) or iteration >= max_executions:
                return {}  # conditional edge routes to synthesize

            agent = agents[iteration]
            prior_outputs = [
                r.get("output", "") for r in results if r.get("output")
            ]
            try:
                outcome = await pattern._run_agent(
                    req, agent, iteration, prior_outputs,
                )
                results.append(
                    _outcome_to_result(agent, outcome, pattern, req)
                )
            except Exception as exc:
                # Defensive: _run_agent already handles its own errors,
                # but never let one agent's crash kill the pipeline.
                logger.exception(
                    "agent_team sequential agent %d raised trace=%s",
                    iteration, req.trace_id,
                )
                results.append({
                    "role": agent.get("role", f"agent{iteration}"),
                    "task": agent.get("task", ""),
                    "output": "",
                    "error": f"{type(exc).__name__}: {exc}",
                })

            return {"agent_results": results, "iterations": iteration + 1}

        # ── Node: SYNTHESIZE (reference step 6) ─────────────────────
        async def synthesize_node(state: AgentTeamState) -> dict:
            """Merge agent outputs into the final user-facing answer."""
            req: HandoffRequest = state["handoff"]
            results: list[dict] = list(state.get("agent_results") or [])
            plan: dict = state.get("team_plan") or {}
            plan_used: bool = state.get("plan_used", False)
            iterations: int = state.get("iterations", 0)

            if not results:
                return {"handoff_response": HandoffResponse(
                    session_id=req.session_id,
                    trace_id=req.trace_id,
                    output="",
                    status=HANDOFF_ERROR,
                    error="agent_team produced no results",
                    artifacts={"team_plan": plan},
                )}

            try:
                transport = pattern._resolve_transport()
                cfg = pattern._resolve_llm_config()
                kwargs = pattern._build_synthesis_kwargs(cfg, req, results)
                result = await transport.chat(**kwargs)
                content = (
                    result.content if hasattr(result, "content")
                    else str(result)
                )
            except Exception as exc:
                logger.exception(
                    "agent_team synthesis failed trace=%s", req.trace_id,
                )
                # Degrade to concatenation rather than failing the turn —
                # the agents DID produce output.
                content = "\n\n".join(
                    f"[{r.get('role')}]: {r.get('output')}"
                    for r in results if r.get("output")
                )

            return {"handoff_response": HandoffResponse(
                session_id=req.session_id,
                trace_id=req.trace_id,
                output=content,
                status=HANDOFF_OK,
                turns_used=iterations + (1 if plan_used else 0) + 1,
                artifacts={
                    "team_plan": plan,
                    "agent_results": results,
                    "plan_used": plan_used,
                },
            )}

        # ── Edges ────────────────────────────────────────────────────
        def should_continue_after_execute(state: AgentTeamState) -> str:
            plan: dict = state.get("team_plan") or {}
            mode: str = plan.get("mode", "sequential")
            agents: list = plan.get("agents") or []
            results: list[dict] = state.get("agent_results") or []
            iteration: int = state.get("iterations", 0)
            max_executions = max(1, pattern.max_turns - 2)

            if mode == "parallel":
                return "synthesize"  # one-shot fan-out already done
            if (
                len(results) >= len(agents)
                or iteration >= len(agents)
                or iteration >= max_executions
            ):
                return "synthesize"
            return "execute"

        graph = StateGraph(AgentTeamState)
        graph.add_node("compose", compose_node)
        graph.add_node("execute", execute_node)
        graph.add_node("synthesize", synthesize_node)
        graph.set_entry_point("compose")
        graph.add_edge("compose", "execute")
        graph.add_conditional_edges(
            "execute",
            should_continue_after_execute,
            {"execute": "execute", "synthesize": "synthesize"},
        )
        graph.add_edge("synthesize", END)
        return graph.compile()

    # ── Sub-agent execution (reference steps 2, 4, 5) ─────────────────

    async def _run_agent(
        self,
        req: HandoffRequest,
        agent_spec: dict,
        idx: int,
        prior_outputs: list[str],
    ) -> tuple[str, bool, bool]:
        """Run one team agent via invoke_subgraph.

        Reference step 2 (context handoff): the child request inherits
        the parent's full context (memory / persona / history bags)
        plus focused fields (agent_role, task_goal, prior_results).
        Reference steps 4-5: invoke_subgraph initializes + executes the
        registered subgraph at depth+1.

        Returns:
            (content, nested_fallback, nested_error):
              content:         agent output text ("" on failure)
              nested_fallback: True if a flat-chat fallback was used
              nested_error:    True if the agent errored entirely
        """
        from paradise.core.supervisor import invoke_subgraph

        agent_mode = agent_spec.get("mode", "chat")
        parent_ctx: dict = req.context or {}

        child_req = HandoffRequest(
            session_id=req.session_id,
            user_id=req.user_id,
            trace_id=f"{req.trace_id}:agent{idx}",
            user_tier=req.user_tier,
            message=agent_spec.get("task", req.message),
            context={
                **parent_ctx,  # inherit memory / persona / history bags
                # Focused overrides:
                "focused_mode": True,
                "task_goal": parent_ctx.get("task_goal", req.message),
                "agent_role": agent_spec.get("role", ""),
                "parent_step": idx,
                "prior_results": prior_outputs,
            },
            depth=req.depth + 1,  # agent_team=1 → sub-agents=2
            parent_trace_id=req.trace_id,
        )

        try:
            response = await invoke_subgraph(
                self._registry, agent_mode, child_req,  # type: ignore[arg-type]
            )
        except Exception as exc:
            logger.warning(
                "agent_team agent[%s] invoke raised (%s) — flat fallback "
                "trace=%s", agent_spec.get("role"), exc, req.trace_id,
            )
            return await self._flat_chat(req, agent_spec, prior_outputs), True, True

        if response.status == "depth_capped":
            logger.info(
                "agent_team agent[%s] depth capped — flat fallback trace=%s",
                agent_spec.get("role"), req.trace_id,
            )
            return await self._flat_chat(req, agent_spec, prior_outputs), True, False

        if response.status != HANDOFF_OK:
            logger.warning(
                "agent_team agent[%s] status=%s err=%s — flat fallback trace=%s",
                agent_spec.get("role"), response.status, response.error,
                req.trace_id,
            )
            return await self._flat_chat(req, agent_spec, prior_outputs), True, False

        return response.output, False, False

    async def _flat_chat(
        self,
        req: HandoffRequest,
        agent_spec: dict,
        prior_outputs: list[str],
    ) -> str:
        """Direct transport.chat with a role-focused prompt.

        Degradation path when invoke_subgraph fails (depth cap, mode
        unavailable, subgraph crash). Uses the same focused context
        fields so the agent keeps its role identity.
        """
        try:
            transport = self._resolve_transport()
            cfg = self._resolve_llm_config()
            kwargs = self._build_flat_kwargs(cfg, req, agent_spec,
                                             prior_outputs)
            result = await transport.chat(**kwargs)
            return (
                result.content if hasattr(result, "content") else str(result)
            ) or ""
        except Exception as exc:
            logger.warning("agent_team flat_chat failed: %s", exc)
            return ""

    # ── Lazy resolution helpers ───────────────────────────────────────

    def _resolve_transport(self) -> Any:
        if self._transport is not None:
            return self._transport
        from paradise.factory import build_transport
        from paradise.config import ParadiseConfig
        return build_transport(ParadiseConfig())

    def _resolve_llm_config(self) -> "LLMConfig":
        if self._llm_config is not None:
            return self._llm_config
        from paradise.config import ParadiseConfig
        return ParadiseConfig().llm

    # ── Per-node kwargs builders ──────────────────────────────────────

    def _build_compose_kwargs(
        self, cfg: "LLMConfig", req: HandoffRequest,
    ) -> dict:
        from paradise.core.patterns.context_builder import (
            build_subgraph_system_prompt,
        )
        system_prompt = build_subgraph_system_prompt(
            req, _COMPOSE_SYSTEM_PROMPT.format(
                max_agents=self._max_agents,
            ),
        )
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": system_prompt,
            "messages": [{"role": "user", "content": req.message}],
            "temperature": 0.2,  # near-deterministic team design
        }
        self._add_provider_kwargs(kwargs, cfg)
        return kwargs

    def _build_synthesis_kwargs(
        self, cfg: "LLMConfig", req: HandoffRequest,
        results: list[dict],
    ) -> dict:
        results_str = "\n\n".join(
            f"[agent: {r.get('role', '?')} | task: {r.get('task', '')}]\n"
            f"{r.get('output') or r.get('error', '(no output)')}"
            for r in results
        )
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": _SYNTHESIZE_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Original request: {req.message}\n\n"
                        f"Agent outputs:\n{results_str}"
                    ),
                }
            ],
            "temperature": 0.5,  # mild creativity for reflow
        }
        self._add_provider_kwargs(kwargs, cfg)
        return kwargs

    def _build_flat_kwargs(
        self,
        cfg: "LLMConfig",
        req: HandoffRequest,
        agent_spec: dict,
        prior_outputs: list[str],
    ) -> dict:
        role = agent_spec.get("role", "assistant")
        task = agent_spec.get("task", req.message)
        prior_str = ""
        if prior_outputs:
            prior_str = "\n\nPrior agents' outputs:\n" + "\n".join(
                f"  [{i+1}] {t[:300]}" for i, t in enumerate(prior_outputs)
            )
        system_prompt = (
            f"You are the '{role}' agent in a multi-agent team handling a "
            f"larger task. Overall goal: {req.message}{prior_str}\n\n"
            f"Your assigned task: {task}\n"
            + _ROLE_CHAT_SYSTEM_PROMPT
        )
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "system_prompt": system_prompt,
            "messages": [{"role": "user", "content": task}],
            "temperature": 0.4,
        }
        self._add_provider_kwargs(kwargs, cfg)
        return kwargs

    def _add_provider_kwargs(self, kwargs: dict, cfg: "LLMConfig") -> None:
        """Mutates kwargs in place — centralizes provider-specific logic."""
        transport = self._resolve_transport()
        if not hasattr(transport, "api_mode"):
            return
        mode = transport.api_mode
        if mode == "ollama_native":
            kwargs["api_url"] = cfg.api_url
        else:
            kwargs["api_url"] = cfg.api_url
            kwargs["api_key"] = cfg.api_key
            kwargs["max_tokens"] = cfg.max_tokens or 512


# ── Module-level helpers ──────────────────────────────────────────────


def _outcome_to_result(
    agent: dict,
    outcome: Any,
    pattern: AgentTeamPattern,
    req: HandoffRequest,
) -> dict:
    """Normalize an agent outcome (tuple or exception) into a result dict."""
    if isinstance(outcome, Exception):
        return {
            "role": agent.get("role", "?"),
            "task": agent.get("task", ""),
            "output": "",
            "error": f"{type(outcome).__name__}: {outcome}",
        }
    content, nested_fallback, nested_error = outcome
    entry: dict[str, Any] = {
        "role": agent.get("role", "?"),
        "task": agent.get("task", ""),
        "output": content or "",
    }
    if nested_fallback:
        entry["nested_fallback"] = True
    if nested_error:
        entry["error"] = "nested invocation failed"
    return entry


def _parse_team_json(
    content: str,
    original_message: str,
    max_agents: int,
) -> dict | None:
    """Extract a team plan from the compose LLM's output.

    Lenient JSON parsing — strips markdown fences, finds the first
    {...} block. Returns None on any parse failure (caller falls back
    to a single-agent plan).

    Normalization:
      * mode coerced to "sequential" | "parallel" (default sequential)
      * agents coerced to [{"role","task","mode"}]; invalid entries
        dropped; capped at max_agents
      * agent "mode" coerced to "chat" | "tool_react" (default chat)
    """
    text = (content or "").strip().strip("`")
    if not text.startswith("{"):
        lo = text.find("{")
        hi = text.rfind("}")
        if lo == -1 or hi == -1 or hi <= lo:
            return None
        text = text[lo:hi + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("agent_team compose JSON parse failed: %r", content[:120])
        return None

    mode = obj.get("mode", "sequential")
    if mode not in ("sequential", "parallel"):
        mode = "sequential"

    raw_agents = obj.get("agents")
    if not isinstance(raw_agents, list):
        return None

    agents: list[dict] = []
    for a in raw_agents:
        if not isinstance(a, dict):
            continue
        task = str(a.get("task", "")).strip()
        if not task:
            # Taskless agent is useless — drop rather than guess.
            continue
        agent_mode = a.get("mode", "chat")
        if agent_mode not in ("chat", "tool_react"):
            agent_mode = "chat"
        agents.append({
            "role": str(a.get("role", f"agent{len(agents)}")).strip()
                    or f"agent{len(agents)}",
            "task": task,
            "mode": agent_mode,
        })
        if len(agents) >= max_agents:
            break

    if not agents:
        return None

    return {"mode": mode, "agents": agents}


__all__ = ["AgentTeamPattern", "AgentTeamState"]
