"""Paradise factory — prod-only component wiring.

Created in Phase 0 as a placeholder. Populated in later phases:
  - Phase 2: build_graph() returns a LangGraph compiled StateGraph
  - Phase 3: build_transport() wraps inner Transport in ResilientTransport
  - Phase 5: build_memory_provider() returns Redis/Qdrant provider

Main-branch code NEVER imports this module. It's only imported when
`ParadiseConfig.mode == "prod"`, gated by the flag in:
  - server/main.py (middleware registration)
  - server/agent_handler.py (transport + memory selection)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from paradise.config import ParadiseConfig

_PROD_CONFIG_PATH = Path(__file__).parent / "config.prod.yaml"


def load_prod_config() -> ParadiseConfig:
    """Load config.prod.yaml with env var substitution.

    Reads `mode: prod` and other prod-only fields. Returns a ParadiseConfig
    dataclass with `mode="prod"` set. Falls back to dev defaults if file
    is missing (e.g., running tests on a fresh clone).
    """
    if not _PROD_CONFIG_PATH.exists():
        # Dev-mode fallback — should not happen in docker compose
        cfg = ParadiseConfig()
        cfg.mode = "dev"
        return cfg

    raw = _PROD_CONFIG_PATH.read_text(encoding="utf-8")
    # Naive ${VAR} substitution (avoids extra dep on python-dotenv)
    for env_key, env_val in os.environ.items():
        raw = raw.replace(f"${{{env_key}}}", env_val)
    data: dict[str, Any] = yaml.safe_load(raw)

    cfg = ParadiseConfig.from_dict(data)
    cfg.mode = data.get("mode", "prod")
    # Phase 0: only `mode` is consumed; remaining prod knobs (ingress/resilience/
    # memory/observability) are read inline in later phases via cfg.prod_raw.
    cfg.prod_raw = data  # type: ignore[attr-defined]
    return cfg


# ── Phase 2/3/5 hooks (stubs, populated in later phases) ──────────

def build_intent_classifier(config: ParadiseConfig):  # noqa: ANN201
    """Phase 2.5: build the intent cascade.

    Strategies are added in order. Each strategy is responsible for its own
    availability check — unavailable backends (Qdrant down, ollama missing)
    silently return None at classify-time, so wiring them here is safe.

    Construction never raises. If the intent module itself fails to import
    (e.g., in dev without prod deps), returns None and the graph runs in
    legacy 4-phase shape.
    """
    try:
        from paradise.core.intent import (
            IntentClassifier, ClassifierConfig,
            RuleStrategy, EmbeddingStrategy, LLMStrategy, CppStrategy,
        )
    except ImportError as exc:
        # Dev mode without intent module deps — graceful degradation.
        return None

    strategies = [RuleStrategy()]

    # L2 — embedding cache. Only wire if qdrant_provider is constructed
    # (Phase 5). For now we attempt a lazy import; if no provider is
    # available, skip the tier.
    try:
        from paradise.memory.qdrant_provider import QdrantSemanticProvider
        qdrant_url = (getattr(config, "prod_raw", None) or {}).get(
            "memory", {}
        ).get("qdrant_url", os.environ.get("QDRANT_URL", "http://localhost:6333"))
        embedder = EmbeddingStrategy(
            qdrant_provider=QdrantSemanticProvider(url=qdrant_url),
        )
        strategies.append(embedder)
    except Exception:
        # Embedding tier optional — silent skip.
        pass

    # L3 — small LLM. Reads ollama URL from env (same var used by transports).
    ollama_url = os.environ.get(
        "OLLAMA_BASE_URL",
        os.environ.get("OLLAMA_API_URL", "http://localhost:11434"),
    )
    strategies.append(LLMStrategy(base_url=ollama_url))

    # L4 — C++ kernel stub. Wired unconditionally; inert until .so lands.
    strategies.append(CppStrategy())

    return IntentClassifier(
        strategies=strategies,
        config=ClassifierConfig(),
    )


def build_graph(config: ParadiseConfig, agent=None, checkpointer=None):  # noqa: ANN201
    """Phase 2: return compiled LangGraph StateGraph.

    The graph is intent-aware when build_intent_classifier() returns a
    non-None classifier; otherwise it falls back to the linear 4-phase
    shape (TOOL→THINK→RESPOND→REFLECT).
    """
    from paradise.core.graph import build_agent_graph
    if agent is None:
        raise ValueError("build_graph requires an agent instance to wrap")
    classifier = build_intent_classifier(config)
    return build_agent_graph(
        agent,
        checkpointer=checkpointer,
        intent_classifier=classifier,
    )


def build_transport(config: ParadiseConfig):  # noqa: ANN201
    """Phase 3: return Transport wrapped in ResilientTransport.

    Resolves the inner transport via the existing registry (resolve_transport),
    then wraps it with retry + circuit breaker + cost budget using settings
    from config.prod.yaml::resilience.
    """
    from paradise.transports import resolve_transport
    from paradise.transports.resilient import wrap_transport

    inner = resolve_transport(config.llm)
    resilience_cfg = (getattr(config, "prod_raw", None) or {}).get("resilience", {})
    return wrap_transport(inner, resilience_cfg)


def build_supervisor(config: ParadiseConfig, checkpointer=None, agent_id="", workspace=None):  # noqa: ANN201
    """Phase 2.9: build a compiled supervisor graph + populated registry.

    Returns:
        (compiled_supervisor_graph, subgraph_registry, mcp_manager)

    The third element is the McpClientManager (or None when MCP is
    disabled / failed to init). Callers MUST close it on shutdown via
    ``await mcp_manager.aclose()`` so background subprocesses don't leak.

    The subgraph registry has 11 patterns registered: real ones wired
    with a shared transport resolved from config.llm, plus stubs.

    Fail-soft: if any component fails to construct (e.g., transport
    resolve raises in dev without ollama), the function returns
    (None, None, None) and callers (agent.run_via_supervisor) MUST
    fall back to run_via_graph or handle_message.

    Args:
        config: ParadiseConfig. Reads config.llm for transport construction.
        checkpointer: optional LangGraph Checkpointer for the supervisor
            graph (session persistence across restarts). When None,
            factory constructs a SqliteSaver (Phase 5) with WAL mode;
            falls back to in-process MemorySaver if sqlite dep missing.
        agent_id: agent identifier for MemoryManager initialization.
            Empty string → memory sync disabled.
        workspace: Workspace instance for MemoryManager + ReflectionEngine
            initialization. None → both disabled.
    """
    mcp_manager = None
    try:
        from paradise.core.supervisor import build_supervisor_graph
        from paradise.core.patterns import (
            register_all, register_basic_patterns, register_judge_pattern,
            register_agent_team_pattern,
        )
        from paradise.core.registry import SubgraphRegistry
        from paradise.transports import resolve_transport

        # Resolve transport — same one used by agent.handle_message,
        # so cost/observability are consistent across paths.
        transport = resolve_transport(config.llm)

        # Wire registry: 11 stubs first, then upgrade chat + tool_react
        # to real patterns sharing the transport.
        registry = SubgraphRegistry()
        register_all(registry)
        register_basic_patterns(
            registry,
            transport=transport,
            llm_config=config.llm,
        )

        # Phase 3-A/3-B: wire tool layer into the GLOBAL ToolRegistry
        # (the one tool_react pattern resolves lazily). This is separate
        # from the SubgraphRegistry above — SubgraphRegistry holds
        # LangGraph subgraphs; ToolRegistry holds function-call tools.
        _wire_tool_registry(config)

        # Phase 3-B: start MCP servers if enabled
        mcp_manager = _build_mcp_manager(config)

        # Intent classifier — reuses Phase 2.6-2.7 cascade.
        classifier = build_intent_classifier(config)

        # Phase 2.8: mode_judge. Fail-soft — if construction raises
        # (e.g., empty registry, httpx missing), proceed without it;
        # supervisor falls back to intent-only routing.
        mode_judge = None
        try:
            from paradise.core.mode_judge import ModeJudge
            ollama_url = os.environ.get(
                "OLLAMA_BASE_URL",
                os.environ.get("OLLAMA_API_URL", "http://localhost:11434"),
            )
            mode_judge = ModeJudge(registry, base_url=ollama_url)
        except Exception as exc:
            import logging
            logging.getLogger("paradise.factory").warning(
                "mode_judge construction failed (%s) — intent-only routing",
                exc,
            )

        # Phase 5: SqliteSaver checkpoint persistence.
        # Replaces the in-memory MemorySaver default so graph state
        # survives Docker restarts. Falls back to MemorySaver (None)
        # if langgraph-checkpoint-sqlite is not installed.
        if checkpointer is None:
            checkpointer = _build_sqlite_checkpointer()

        # Phase 5: MemoryManager for cleanup_node turn writeback.
        # Only initialized when workspace is provided (agent.py always
        # passes one in prod). Without workspace, memory sync is skipped.
        memory_mgr = _build_memory_manager(agent_id, workspace)

        # Phase 5: ReflectionEngine for cleanup_node post-turn fact
        # extraction + memory.md writeback. Uses config.reflection.llm
        # (default qwen2.5:3b). Fail-soft — reflection is best-effort.
        reflection_eng = _build_reflection_engine(config, workspace)

        # Phase 5 Step 5: JudgePattern — independent SubgraphPattern for
        # LLM-as-judge quality gating. Registered ONLY when
        # config.reflection.judge_enabled is True (default off). When
        # enabled, the "judge" mode becomes available for intent routing
        # and mode_judge selection.
        if getattr(config.reflection, "judge_enabled", False):
            try:
                register_judge_pattern(
                    registry,
                    transport=transport,
                    llm_config=config.llm,
                    judge_transport=None,  # lazy-resolves to reflection.llm
                    judge_llm_config=config.reflection.llm,
                    threshold=config.reflection.judge_threshold,
                    max_retries=config.reflection.judge_max_retries,
                )
                import logging
                logging.getLogger("paradise.factory").info(
                    "JudgePattern registered (threshold=%.2f max_retries=%d)",
                    config.reflection.judge_threshold,
                    config.reflection.judge_max_retries,
                )
            except Exception as exc:
                import logging
                logging.getLogger("paradise.factory").warning(
                    "JudgePattern registration failed (%s) — judge mode "
                    "unavailable", exc,
                )

        # Phase 6: AgentTeamPattern — multi-agent orchestration for
        # complex queries. Registered ONLY when
        # config.agent_team.enabled is True (default off). When enabled,
        # the complexity router sends complex turns here.
        if getattr(config, "agent_team", None) and config.agent_team.enabled:
            try:
                register_agent_team_pattern(
                    registry,
                    transport=transport,
                    llm_config=config.llm,
                    max_agents=config.agent_team.max_agents,
                )
                import logging
                logging.getLogger("paradise.factory").info(
                    "AgentTeamPattern registered (max_agents=%d)",
                    config.agent_team.max_agents,
                )
            except Exception as exc:
                import logging
                logging.getLogger("paradise.factory").warning(
                    "AgentTeamPattern registration failed (%s) — complex "
                    "turns fall back to plan_execute", exc,
                )

        # Phase 6: Query preprocessor (correction + rewrites) and
        # complexity assessor (simple/complex routing). Both fail-soft
        # to None → supervisor passes through / uses intent-only routing.
        preprocessor = _build_preprocessor(config)
        complexity_assessor = _build_complexity_assessor(config)

        compiled = build_supervisor_graph(
            registry,
            intent_classifier=classifier,
            mode_judge=mode_judge,
            checkpointer=checkpointer,
            atif_exporter=build_atif_exporter(config),
            memory_provider=memory_mgr,
            reflection_engine=reflection_eng,
            preprocessor=preprocessor,
            complexity_assessor=complexity_assessor,
        )
        return compiled, registry, mcp_manager
    except Exception as exc:
        # Lazy import inside the try so a missing prod-only dep doesn't
        # crash module load. Log and signal fallback.
        import logging
        logging.getLogger("paradise.factory").exception(
            "build_supervisor failed: %s — agent will fall back to "
            "run_via_graph or handle_message", exc,
        )
        # Best-effort cleanup of half-started MCP manager
        if mcp_manager is not None:
            try:
                import asyncio
                asyncio.get_event_loop().create_task(mcp_manager.aclose())
            except Exception:
                pass
        return None, None, None


def _build_sqlite_checkpointer():
    """Phase 5: construct an AsyncSqliteSaver for durable graph state.

    Uses WAL mode + busy_timeout for Docker-volume compatibility.
    Falls back to None (→ MemorySaver inside build_supervisor_graph)
    when langgraph-checkpoint-sqlite/aiosqlite is not installed or DB
    init fails.

    DB path: ``data/checkpoints/langgraph.db`` (single file, thread_id
    distinguishes sessions).

    Implementation note: we subclass AsyncSqliteSaver so the instance
    passes LangGraph's isinstance(checkpointer, BaseCheckpointSaver)
    validation in graph.compile(). A composition wrapper with
    __getattr__ would fail this check.
    """
    import logging
    log = logging.getLogger("paradise.factory")
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        import aiosqlite

        db_path = Path("data/checkpoints/langgraph.db")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # aiosqlite.connect() returns immediately; the actual connection
        # is deferred to the first await (inside setup()). This lets us
        # construct the checkpointer synchronously in the factory.
        #
        # WAL mode is intentionally NOT set via PRAGMA here. Setting
        # journal_mode via aiosqlite's background thread causes cursor
        # state issues ("SQL statements in progress" on subsequent
        # commit). The default journal mode (DELETE) still persists data
        # to disk reliably — WAL is a performance optimization for
        # concurrent read/write, not a durability requirement.
        conn = aiosqlite.connect(str(db_path))
        log.info("AsyncSqliteSaver checkpoint at %s", db_path)
        return AsyncSqliteSaver(conn)
    except ImportError:
        log.warning(
            "langgraph-checkpoint-sqlite or aiosqlite not installed — "
            "falling back to MemorySaver (state lost on restart)"
        )
        return None
    except Exception as exc:
        log.warning("AsyncSqliteSaver init failed (%s) — MemorySaver fallback", exc)
        return None


def _build_preprocessor(config: ParadiseConfig):
    """Phase 6: construct QueryPreprocessor for query correction + rewrites.

    Fail-soft → None (supervisor preprocess_node becomes a pass-through).
    Skipped when config.query_preprocess.enabled is False.
    """
    import logging
    log = logging.getLogger("paradise.factory")
    try:
        cfg = getattr(config, "query_preprocess", None)
        if cfg is None or not cfg.enabled:
            return None
        from paradise.core.preprocess import QueryPreprocessor
        from paradise.transports import resolve_transport
        transport = resolve_transport(config.llm)
        return QueryPreprocessor(
            transport, config.llm, max_rewrites=cfg.max_rewrites,
        )
    except Exception as exc:
        log.warning("QueryPreprocessor init failed (%s) — disabled", exc)
        return None


def _build_complexity_assessor(config: ParadiseConfig):
    """Phase 6: construct ComplexityAssessor for simple/complex routing.

    Fail-soft → None (intent-only routing, complexity="unknown").
    Skipped when config.complexity.enabled is False.
    """
    import logging
    log = logging.getLogger("paradise.factory")
    try:
        cfg = getattr(config, "complexity", None)
        if cfg is None or not cfg.enabled:
            return None
        from paradise.core.complexity import ComplexityAssessor
        from paradise.transports import resolve_transport
        transport = resolve_transport(config.llm)
        return ComplexityAssessor(transport, config.llm)
    except Exception as exc:
        log.warning("ComplexityAssessor init failed (%s) — disabled", exc)
        return None


def _build_memory_manager(agent_id: str, workspace):
    """Phase 5: construct + initialize MemoryManager for cleanup_node.

    Returns None when workspace is not provided (tests / dev without
    workspace) so cleanup_node skips memory sync gracefully.
    """
    import logging
    log = logging.getLogger("paradise.factory")
    if workspace is None:
        log.debug("MemoryManager skipped — no workspace provided")
        return None
    try:
        from paradise.memory.manager import MemoryManager
        mgr = MemoryManager()
        mgr.initialize(agent_id, workspace)
        log.info("MemoryManager initialized for agent=%s", agent_id)
        return mgr
    except Exception as exc:
        log.warning("MemoryManager init failed (%s) — memory sync disabled", exc)
        return None


def _build_reflection_engine(config: ParadiseConfig, workspace):
    """Phase 5: construct ReflectionEngine for post-turn fact extraction.

    Uses config.reflection.llm transport (default qwen2.5:3b).
    Returns None on failure — cleanup_node skips reflection gracefully.
    """
    import logging
    log = logging.getLogger("paradise.factory")
    try:
        from paradise.reflection.engine import ReflectionEngine
        from paradise.transports import resolve_transport

        # Override the reflection LLM's api_url with the env var so it
        # works inside Docker (host.docker.internal instead of localhost).
        reflection_cfg = config.reflection
        ollama_url = os.environ.get(
            "OLLAMA_BASE_URL",
            os.environ.get("OLLAMA_API_URL", ""),
        )
        if ollama_url:
            reflection_cfg.llm.api_url = ollama_url

        reflection_transport = resolve_transport(reflection_cfg.llm)
        workspace_root = workspace.root if workspace is not None else Path("data/agents/default")
        workspace_root = Path(workspace_root)
        workspace_root.mkdir(parents=True, exist_ok=True)
        eng = ReflectionEngine(
            reflection_transport,
            reflection_cfg,
            workspace_root=workspace_root,
        )
        log.info("ReflectionEngine initialized (model=%s url=%s)",
                 reflection_cfg.llm.model, reflection_cfg.llm.api_url)
        return eng
    except Exception as exc:
        log.warning("ReflectionEngine init failed (%s) — reflection disabled", exc)
        return None


def _wire_tool_registry(config: ParadiseConfig):
    """Phase 3-A/3-B: inject config into bash tool + discover built-ins.

    Idempotent and fail-soft — any error is logged but never propagates
    so supervisor construction remains resilient.
    """
    import logging
    log = logging.getLogger("paradise.factory")
    try:
        # Inject bash config first so handlers see the right values
        # when discover_builtin_tools imports bash_tool.
        from paradise.tools.bash_tool import configure as configure_bash
        configure_bash(config.bash)

        # discover_builtin_tools is idempotent (importlib caches modules)
        from paradise.tools.registry import discover_builtin_tools
        imported = discover_builtin_tools()
        log.debug("Tool discovery imported %d modules", len(imported))
    except Exception as exc:
        log.warning("tool registry wiring failed (%s) — tools may be missing", exc)


def _build_mcp_manager(config: ParadiseConfig):  # noqa: ANN201
    """Phase 3-B: construct + start McpClientManager when mcp.enabled.

    Returns the manager (already started, tools registered into the
    global ToolRegistry) or None when disabled / failed.
    """
    import logging
    log = logging.getLogger("paradise.factory")
    if not config.mcp.enabled or not config.mcp.servers:
        return None
    try:
        from paradise.tools.mcp_tool import McpClientManager
        from paradise.tools.registry import registry as global_tool_registry
        manager = McpClientManager(config.mcp.servers)
        manager.start()  # blocks until all servers initialized
        n = manager.register_tools_into(global_tool_registry)
        log.info(
            "MCP: registered %d tools from %d server(s) (%d unavailable)",
            n, len(config.mcp.servers),
            len(manager.unavailable_servers),
        )
        return manager
    except Exception as exc:
        log.exception("MCP init failed — supervisor will run without MCP tools: %s", exc)
        return None


def build_memory_provider(config: ParadiseConfig):  # noqa: ANN201
    """Phase 5: return Redis/Qdrant MemoryProvider."""
    raise NotImplementedError("Populated in Phase 5 — Memory stack")


def build_atif_exporter(config: ParadiseConfig):  # noqa: ANN201
    """Phase 2.13: build an AtifExporter from config.

    Reads AtifConfig from `config.atif` (default OFF — enabled=False).
    When disabled, returns an AtifExporter whose `enabled` property is
    False so cleanup_node's fast-path skips trace building entirely.

    Never raises: any construction error (or a malformed `config.atif`
    field — e.g., set to a non-AtifConfig value by tests or buggy code)
    returns an inert exporter (enabled=False) so supervisor startup is
    never blocked by training pipeline misconfiguration.
    """
    try:
        from paradise.config import AtifConfig
        from paradise.observability.atif_exporter import AtifExporter
        atif_cfg = config.atif
        if not isinstance(atif_cfg, AtifConfig):
            import logging
            logging.getLogger("paradise.factory").warning(
                "config.atif is %r (expected AtifConfig) — using inert default",
                type(atif_cfg).__name__,
            )
            atif_cfg = AtifConfig(enabled=False)
        return AtifExporter(atif_cfg)
    except Exception as exc:
        import logging
        logging.getLogger("paradise.factory").warning(
            "AtifExporter construction failed (%s) — training export disabled",
            exc,
        )
        from paradise.config import AtifConfig
        return AtifExporter(AtifConfig(enabled=False))
