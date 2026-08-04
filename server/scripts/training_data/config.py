"""
Configuration loader — reads from YAML file, env vars, and CLI overrides.

Priority (low → high):
    1. Defaults in this file
    2. YAML config file (~/.aichat/training_config.yaml or --config)
    3. Environment variables
    4. CLI arguments
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml

# ── Built-in defaults ──────────────────────────────────────────────

DEFAULT_CONFIG = {
    "log_dir": str(Path(os.path.expanduser("~/workspace/ai_project/android-app/logs"))),
    "output_dir": str(Path(os.path.expanduser("~/workspace/ai_project/android-app/logs/training"))),
    "deploy_dir": str(Path(os.path.expanduser("~/workspace/ai_project/android-app/logs/training/deploy"))),
    "formats": ["sft", "dpo", "grpo"],
    "min_messages_per_session": 2,
    "dpo_negatives_per_positive": 1,
    "split_ratios": [0.8, 0.1, 0.1],
    "random_seed": 42,
    "dpo_rules": {
        # Candidate scoring (higher = better, used to rank assistants per user turn)
        "scoring": {
            "llm_rewrite": 4,
            "like": 3,
            "unmarked": 2,
            "retry": 1,
            "dislike": 0,
        },
        # Which sources to admit as positive/negative candidates
        "positive_sources": ["llm_rewrite", "like", "unmarked"],
        "negative_sources": ["retry", "dislike"],
        # Pairing strategy once (chosen, rejected) pool is built per user turn:
        #   top_bottom  — single best vs single worst per group
        #   cartesian   — every positive × every negative
        #   one_to_many — each positive paired with up to N negatives
        "pair_strategy": "top_bottom",
        "max_pairs_per_group": 3,
        "min_score_gap": 1,            # |score(chosen) - score(rejected)| must be ≥ this
        "min_response_length": 10,     # drop candidates shorter than this (chars)
        # When augment.enabled=True, generate llm_rewrite candidates for groups
        # whose top positive score is below this threshold (e.g. no like, only unmarked).
        "llm_rewrite_when_max_score_below": 3,
        # Refine trigger — when do we ask the LLM to synthesise a chosen reply?
        #   no_positive     — group has NO positive candidate (only dislike/retry)
        #   below_threshold — group's max score < llm_rewrite_when_max_score_below
        #   always          — refine every group (expensive, mainly for debugging)
        "refine_trigger": "no_positive",
    },
    "augment": {
        "enabled": False,
        "provider": "ollama",          # ollama|openai|dashscope|deepseek|zhipu|kimi
        "ollama_url": "http://localhost:11434/v1",
        "ollama_model": "qwen2.5:7b-instruct",
        "openai_url": "https://api.openai.com/v1",
        "openai_model": "gpt-4o-mini",
        "openai_key": "",
        "dashscope_key": "",
        "dashscope_model": "qwen-max",
        "deepseek_key": "",
        "deepseek_model": "deepseek-chat",
        "deepseek_url": "https://api.deepseek.com/v1",
        "zhipu_key": "",
        "zhipu_model": "glm-4-flash",
        "kimi_key": "",
        "kimi_model": "moonshot-v1-8k",
        "kimi_url": "https://api.moonshot.cn/v1",
        "concurrency": 3,
        "variants_per_question": 2,
        # Response refinement: when True, augmenter also calls LLM to produce
        # an improved assistant reply per user turn (used as chosen for DPO).
        "refine_responses": True,
    },
    "deploy": {
        "ollama_base_model": "qwen2.5:0.5b",
        "ollama_model_name": "aichat-sft",
        "vllm_gpu_memory": 0.9,
        "vllm_max_model_len": 4096,
    },
}

# ── Runtime config (populated by load_config) ─────────────────────

cfg: dict[str, Any] = {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_dotenv(search_paths: list[str | Path] | None = None) -> dict[str, str]:
    """Load a .env file into os.environ WITHOUT overriding existing values.

    Priority: real os.environ > .env file. Returns the dict of values loaded
    (so callers can inspect or log them). Lines starting with `#` are ignored;
    values may be quoted with single or double quotes.
    """
    if search_paths is None:
        search_paths = [
            Path.cwd() / ".env",
            Path.home() / ".aichat" / ".env",
            Path(__file__).resolve().parent.parent.parent / ".env",  # server/.env
        ]
    loaded: dict[str, str] = {}
    for p in search_paths:
        p = Path(p).expanduser()
        if not p.exists():
            continue
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                # Strip inline comments and surrounding quotes
                value = value.strip()
                if value.startswith(('"', "'")):
                    quote = value[0]
                    end = value.find(quote, 1)
                    value = value[1:end if end > 0 else len(value)]
                else:
                    # strip trailing ` # comment`
                    if " #" in value:
                        value = value.split(" #", 1)[0].rstrip()
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded[key] = value
        except Exception as e:
            print(f"[training_data.config] WARNING: failed to read {p}: {e}", file=sys.stderr)
            continue
        # First existing .env wins; don't merge across multiple files.
        break
    return loaded


def _env_overrides(cfg: dict) -> dict:
    """Apply environment variable overrides."""
    env_map = {
        "AICHAT_LOG_DIR": ("log_dir", str),
        "AICHAT_OUTPUT_DIR": ("output_dir", str),
        "AICHAT_FORMATS": ("formats", lambda v: v.split(",")),
        "AICHAT_AUGMENT_ENABLED": ("augment.enabled", lambda v: v.lower() in ("1", "true", "yes")),
        "AICHAT_AUGMENT_PROVIDER": ("augment.provider", str),
        "AICHAT_AUGMENT_OLLAMA_URL": ("augment.ollama_url", str),
        "AICHAT_AUGMENT_OLLAMA_MODEL": ("augment.ollama_model", str),
        "AICHAT_OPENAI_KEY": ("augment.openai_key", str),
        "AICHAT_DASHSCOPE_KEY": ("augment.dashscope_key", str),
        "AICHAT_DEEPSEEK_KEY": ("augment.deepseek_key", str),
        "AICHAT_DEEPSEEK_MODEL": ("augment.deepseek_model", str),
        "AICHAT_ZHIPU_KEY": ("augment.zhipu_key", str),
        "AICHAT_ZHIPU_MODEL": ("augment.zhipu_model", str),
        "AICHAT_KIMI_KEY": ("augment.kimi_key", str),
        "AICHAT_KIMI_MODEL": ("augment.kimi_model", str),
    }
    for env_key, (cfg_path, converter) in env_map.items():
        val = os.getenv(env_key)
        if val is not None:
            parts = cfg_path.split(".")
            target = cfg
            for p in parts[:-1]:
                if p not in target:
                    target[p] = {}
                target = target[p]
            target[parts[-1]] = converter(val)
    return cfg


def _cli_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    """Apply CLI argument overrides (only if explicitly set)."""
    if hasattr(args, "log_dir") and args.log_dir is not None:
        cfg["log_dir"] = args.log_dir
    if hasattr(args, "output_dir") and args.output_dir is not None:
        cfg["output_dir"] = args.output_dir
    if hasattr(args, "augment") and args.augment:
        cfg["augment"]["enabled"] = True
    if hasattr(args, "deploy") and args.deploy:
        cfg["deploy"] = cfg.get("deploy", {})
    if hasattr(args, "formats") and args.formats is not None:
        cfg["formats"] = args.formats.split(",")
    return cfg


def load_config(config_path: str | None = None,
                cli_args: argparse.Namespace | None = None) -> dict:
    """Load configuration from YAML file, env vars, and CLI.

    Priority (low → high):
        1. Defaults in this file
        2. YAML config file (~/.aichat/training_config.yaml or --config)
        3. .env file (cwd / ~/.aichat/ / server/.env) — does NOT override
           real environment variables
        4. Environment variables
        5. CLI arguments
    """
    cfg = DEFAULT_CONFIG.copy()

    # Try YAML config
    search_paths = [
        config_path,
        os.getenv("AICHAT_CONFIG"),
        os.path.expanduser("~/.aichat/training_config.yaml"),
        "training_config.yaml",
    ]
    for path in search_paths:
        if path and Path(path).exists():
            with open(path, "r", encoding="utf-8") as f:
                yaml_cfg = yaml.safe_load(f) or {}
            _deep_merge(cfg, yaml_cfg)
            break

    # .env file (does not override real env)
    dotenv_loaded = _load_dotenv()
    if dotenv_loaded:
        # Mask values for logging (only show keys)
        import logging
        logging.getLogger("training_data.config").info(
            "Loaded .env keys: %s", ", ".join(sorted(dotenv_loaded.keys())),
        )

    # Env overrides (real env + .env-loaded values)
    _env_overrides(cfg)

    # CLI overrides
    if cli_args:
        _cli_overrides(cfg, cli_args)

    # Resolve paths
    cfg["log_dir"] = str(Path(cfg["log_dir"]).expanduser().resolve())
    cfg["output_dir"] = str(Path(cfg["output_dir"]).expanduser().resolve())
    cfg["deploy_dir"] = str(Path(cfg.get("deploy_dir", cfg["output_dir"] + "/deploy")).expanduser().resolve())

    # Ensure output directories exist
    Path(cfg["output_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["deploy_dir"]).mkdir(parents=True, exist_ok=True)

    return cfg


# ── Convenience accessors ──────────────────────────────────────────


def get(key: str, default: Any = None) -> Any:
    """Get a config value by dot-notation key."""
    parts = key.split(".")
    val = cfg
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p, default)
        else:
            return default
    return val
