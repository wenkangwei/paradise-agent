"""Paradise Agent Framework — 配置管理."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class LLMConfig:
    """LLM 连接配置."""
    api_url: str = ""
    api_key: str = ""
    model: str = ""
    provider: str = "openai"  # openai | anthropic | ollama
    temperature: float = 0.7
    max_tokens: int = 2048
    api_mode: str = ""  # 自动检测，可强制指定

    @property
    def resolved_api_mode(self) -> str:
        """根据 provider 和 URL 推断 api_mode."""
        if self.api_mode:
            return self.api_mode
        if self.provider == "anthropic":
            return "anthropic_messages"
        if self.provider == "ollama":
            # Ollama 原生 API 用 /api/chat
            if "/v1" not in self.api_url and "/chat/completions" not in self.api_url:
                return "ollama_native"
            return "openai_compat"
        return "openai_compat"


@dataclass
class ReflectionConfig:
    """反思系统配置."""
    enabled: bool = True
    llm: LLMConfig = field(default_factory=lambda: LLMConfig(
        provider="ollama",
        model="qwen2.5:3b",
        api_url="http://localhost:11434",
        temperature=0.5,
        max_tokens=1024,
    ))
    # 触发策略
    post_turn: bool = True
    idle_minutes: float = 10.0
    scheduled_cron: str = "0 3 * * *"  # 每天凌晨3点
    # 参数
    max_facts_per_turn: int = 3
    idle_max_context_turns: int = 50
    # Phase 5: LLM-as-judge quality evaluation (default OFF).
    # When enabled AND atif is also enabled, reflect_node scores each
    # response via a small LLM; low score triggers one retry via chat.
    # Kept off by default because it adds ~1-2s latency per turn and
    # is only valuable when harvesting training data.
    judge_enabled: bool = False
    judge_threshold: float = 0.6
    judge_max_retries: int = 1


@dataclass
class HeartbeatConfig:
    """心跳配置."""
    enabled: bool = True
    interval_seconds: int = 300
    cooldown_seconds: int = 120
    jitter_max_seconds: int = 60


@dataclass
class PluginConfig:
    """插件配置."""
    enabled: bool = True
    directories: list[str] = field(default_factory=list)
    disabled_plugins: list[str] = field(default_factory=list)


@dataclass
class AtifConfig:
    """ATIF (Agent Trace & Instruction Format) 训练导出配置.

    Phase 2.13 — default OFF (enabled=False). When enabled, cleanup_node
    in the supervisor writes a JSONL record per turn to
    `{export_path}/{YYYY-MM-DD}.jsonl`. Schema in ARCHITECTURE_V2.md §7.2.

    Zero overhead when disabled: AtifExporter.export() returns immediately
    without touching disk or spawning the writer task.
    """
    enabled: bool = False
    export_path: str = "data/atif"
    include_handoff: bool = True
    include_cost: bool = True
    redact_pii: bool = True


@dataclass
class BashConfig:
    """Bash tool 配置 — read/write 权限分离.

    Phase 3-A — bash_read 用 allowlist（只允许 ls/cat/grep 等只读命令），
    bash_write 用 blocklist + ulimit（subprocess 隔离）。

    enabled=True 默认开启（替换原 builtin.py 中粗糙的 bash 工具）。
    """
    enabled: bool = True
    read_timeout_seconds: int = 10
    write_timeout_seconds: int = 30
    write_memory_limit_mb: int = 256
    sandbox_root: str = ""           # 空 = 复用 tools.sandbox.SANDBOX_ROOT
    full_access: bool = False        # True 时取消 cwd 锁定（dev 用，prod 不开）


@dataclass
class McpServerConfig:
    """单个 MCP server 配置.

    三种 transport:
      - stdio: command + args + env（最常用，本地子进程）
      - sse:   url（Server-Sent Events 流）
      - http:  url（Streamable HTTP）
    """
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    transport: str = "stdio"         # stdio | sse | http
    timeout_seconds: int = 60        # 单次 call_tool 超时
    connect_timeout_seconds: int = 30  # 初始连接超时


@dataclass
class McpConfig:
    """MCP (Model Context Protocol) 集成总配置.

    Phase 3-B — default OFF. When enabled, build_supervisor starts a
    McpClientManager that spawns each configured server (stdio/sse/http),
    lists their tools, and registers them into the global ToolRegistry
    with prefix `mcp.{server_name}.{tool_name}`.

    Zero overhead when disabled: McpClientManager is never constructed.
    """
    enabled: bool = False
    servers: list[McpServerConfig] = field(default_factory=list)


@dataclass
class QueryPreprocessConfig:
    """Query 预处理配置（纠正 + 并行改写）— Phase 6.

    Reference flow: 用户输入 → query纠正 + 并行改写(N≤3) → 意图识别.
    Default ON — one small LLM call per (non-trivial) turn. The
    heuristic gate in preprocess.py skips greetings / one-liners so
    the hot path pays zero added latency.
    """
    enabled: bool = True
    max_rewrites: int = 2


@dataclass
class ComplexityConfig:
    """复杂度判定配置（simple/complex 二元分类）— Phase 6.

    Reference flow: 意图识别 → 是否复杂意图? → 简单走 tool/rag agent，
    复杂走 agent_team 编排. Default ON — rules resolve most queries
    at 0ms; the LLM tier only fires when rules are inconclusive.
    """
    enabled: bool = True


@dataclass
class AgentTeamConfig:
    """AgentTeam 多 agent 编排配置 — Phase 6.

    Default OFF (same policy as judge): agent_team costs 2+ LLM calls
    per turn beyond the sub-agents themselves (compose + synthesize),
    so it's only worth enabling when complex-query orchestration is
    actually wanted. When enabled AND complexity routing classifies a
    turn as complex, the supervisor routes to "agent_team".
    """
    enabled: bool = False
    max_agents: int = 4
    default_agent_mode: str = "chat"


@dataclass
class ParadiseConfig:
    """Paradise Agent 完整配置."""
    agent_id: str = ""
    # Deployment mode — "dev" (main branch, default) or "prod" (prod branch).
    # Added in Phase 0; defaults to dev so main-branch code is unaffected.
    # When mode == "prod", server/main.py & agent_handler.py route through
    # prod-only components (LangGraph, ResilientTransport, Redis memory).
    mode: str = "dev"
    # Phase 2: when True, agent_handler routes through LangGraph StateGraph
    # instead of handle_message's inline loop. Defaults False so main is unaffected.
    langgraph_enabled: bool = False
    # Phase 2.9: when True, agent_handler routes through supervisor graph
    # (intent → mode-select → subgraph → reflect → cleanup) instead of
    # either handle_message or run_via_graph. Defaults False so existing
    # paths are untouched. supervisor_enabled takes precedence over
    # langgraph_enabled when both are True.
    supervisor_enabled: bool = False
    # LLM
    llm: LLMConfig = field(default_factory=LLMConfig)
    # 工具阶段用低温度
    tool_llm: LLMConfig = field(default_factory=lambda: LLMConfig(
        temperature=0.3,
        max_tokens=1024,
    ))
    # 反思
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    # 心跳
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    # 插件
    plugins: PluginConfig = field(default_factory=PluginConfig)
    # Phase 2.13: ATIF training export. Default OFF so main-branch code
    # is unaffected; flipped ON in prod when data collection is needed.
    atif: AtifConfig = field(default_factory=AtifConfig)
    # Phase 3-A: Bash tool with read/write privilege split.
    bash: BashConfig = field(default_factory=BashConfig)
    # Phase 3-B: MCP integration. Default OFF.
    mcp: McpConfig = field(default_factory=McpConfig)
    # Phase 6: query preprocessing + complexity routing + agent_team.
    query_preprocess: QueryPreprocessConfig = field(
        default_factory=QueryPreprocessConfig,
    )
    complexity: ComplexityConfig = field(default_factory=ComplexityConfig)
    agent_team: AgentTeamConfig = field(default_factory=AgentTeamConfig)
    # 工具
    enable_tools: bool = True
    max_tool_rounds: int = 5
    # Prompt
    max_history: int = 20

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParadiseConfig:
        """从字典创建配置."""
        config = cls()
        if "llm" in data:
            config.llm = LLMConfig(**{k: v for k, v in data["llm"].items() if k in LLMConfig.__dataclass_fields__})
        if "tool_llm" in data:
            config.tool_llm = LLMConfig(**{k: v for k, v in data["tool_llm"].items() if k in LLMConfig.__dataclass_fields__})
        if "reflection" in data:
            r = data["reflection"]
            config.reflection = ReflectionConfig(
                enabled=r.get("enabled", True),
                post_turn=r.get("post_turn", True),
                idle_minutes=r.get("idle_minutes", 10.0),
                scheduled_cron=r.get("scheduled_cron", "0 3 * * *"),
                max_facts_per_turn=r.get("max_facts_per_turn", 3),
                idle_max_context_turns=r.get("idle_max_context_turns", 50),
            )
            if "llm" in r:
                config.reflection.llm = LLMConfig(**{k: v for k, v in r["llm"].items() if k in LLMConfig.__dataclass_fields__})
        if "heartbeat" in data:
            config.heartbeat = HeartbeatConfig(**{k: v for k, v in data["heartbeat"].items() if k in HeartbeatConfig.__dataclass_fields__})
        if "plugins" in data:
            config.plugins = PluginConfig(**{k: v for k, v in data["plugins"].items() if k in PluginConfig.__dataclass_fields__})
        if "atif" in data:
            config.atif = AtifConfig(**{k: v for k, v in data["atif"].items() if k in AtifConfig.__dataclass_fields__})
        if "bash" in data:
            config.bash = BashConfig(**{k: v for k, v in data["bash"].items() if k in BashConfig.__dataclass_fields__})
        if "mcp" in data:
            m = data["mcp"]
            config.mcp = McpConfig(
                enabled=m.get("enabled", False),
                servers=[
                    McpServerConfig(**{k: v for k, v in s.items() if k in McpServerConfig.__dataclass_fields__})
                    for s in m.get("servers", [])
                ],
            )
        if "query_preprocess" in data:
            config.query_preprocess = QueryPreprocessConfig(**{k: v for k, v in data["query_preprocess"].items() if k in QueryPreprocessConfig.__dataclass_fields__})
        if "complexity" in data:
            config.complexity = ComplexityConfig(**{k: v for k, v in data["complexity"].items() if k in ComplexityConfig.__dataclass_fields__})
        if "agent_team" in data:
            config.agent_team = AgentTeamConfig(**{k: v for k, v in data["agent_team"].items() if k in AgentTeamConfig.__dataclass_fields__})
        config.enable_tools = data.get("enable_tools", True)
        config.max_tool_rounds = data.get("max_tool_rounds", 5)
        config.max_history = data.get("max_history", 20)
        # Phase 2: langgraph_enabled (defaults False, only True in prod)
        config.langgraph_enabled = data.get("langgraph_enabled", False)
        # Phase 2.9: supervisor_enabled (defaults False, takes precedence
        # over langgraph_enabled when True)
        config.supervisor_enabled = data.get("supervisor_enabled", False)
        return config

    def to_dict(self) -> dict[str, Any]:
        """转为字典（排除敏感信息）."""
        from dataclasses import asdict
        d = asdict(self)
        # 脱敏 API key
        for key in ("llm", "tool_llm"):
            if key in d and "api_key" in d[key]:
                val = d[key]["api_key"]
                if val:
                    d[key]["api_key"] = val[:4] + "..." + val[-4:] if len(val) > 8 else "***"
        if "reflection" in d and "llm" in d["reflection"]:
            val = d["reflection"]["llm"].get("api_key", "")
            if val:
                d["reflection"]["llm"]["api_key"] = val[:4] + "..." + val[-4:] if len(val) > 8 else "***"
        return d
