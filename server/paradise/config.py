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
        config.enable_tools = data.get("enable_tools", True)
        config.max_tool_rounds = data.get("max_tool_rounds", 5)
        config.max_history = data.get("max_history", 20)
        # Phase 2: langgraph_enabled (defaults False, only True in prod)
        config.langgraph_enabled = data.get("langgraph_enabled", False)
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
