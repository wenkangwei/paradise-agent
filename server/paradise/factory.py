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

def build_graph(config: ParadiseConfig, agent=None, checkpointer=None):  # noqa: ANN201
    """Phase 2: return compiled LangGraph StateGraph."""
    from paradise.core.graph import build_agent_graph
    if agent is None:
        raise ValueError("build_graph requires an agent instance to wrap")
    return build_agent_graph(agent, checkpointer=checkpointer)


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


def build_memory_provider(config: ParadiseConfig):  # noqa: ANN201
    """Phase 5: return Redis/Qdrant MemoryProvider."""
    raise NotImplementedError("Populated in Phase 5 — Memory stack")
