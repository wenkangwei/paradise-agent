"""
Training Data Pipeline — main entry point.

Usage:
    # Use default config
    python -m scripts.training_data.pipeline

    # Use custom YAML config
    python -m scripts.training_data.pipeline --config my_config.yaml

    # Quick overrides
    python -m scripts.training_data.pipeline --augment --deploy
    python -m scripts.training_data.pipeline --formats sft,dpo --log-dir /tmp/logs

    # Generate sample config
    python -m scripts.training_data.pipeline --sample-config
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .loader import load_sessions, load_behaviours, join
from .converter import (
    convert as _convert,
    _build_candidate_groups,
    DEFAULT_SCORING,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("training_data.pipeline")


def _generate_sample_config(path: str = "training_config.yaml"):
    """Write a sample YAML config file."""
    import yaml
    from .config import DEFAULT_CONFIG  # noqa: F811
    cfg = DEFAULT_CONFIG.copy()
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    logger.info("Sample config written to: %s", path)


def main():
    parser = argparse.ArgumentParser(
        description="Convert AiChat conversation logs into LLM fine-tuning training data",
        epilog="Config priority: CLI > env > YAML > defaults. See docs for details.",
    )
    parser.add_argument("--config", type=str, default=None,
                        help="Path to YAML config file (default: ~/.aichat/training_config.yaml)")
    parser.add_argument("--log-dir", type=str, default=None,
                        help="Override log source directory")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Override training data output directory")
    parser.add_argument("--augment", action="store_true",
                        help="Enable LLM augmentation (question rewrites for SFT)")
    parser.add_argument("--refine", action="store_true",
                        help="Enable LLM response refinement (DPO llm_rewrite candidates)")
    parser.add_argument("--deploy", action="store_true",
                        help="Generate Ollama Modelfile + vLLM deployment config")
    parser.add_argument("--formats", type=str, default=None,
                        help="Comma-separated: sft,dpo,grpo (default: all three)")
    parser.add_argument("--sample-config", action="store_true",
                        help="Generate a sample training_config.yaml and exit")
    args = parser.parse_args()

    if args.sample_config:
        _generate_sample_config()
        return

    # Load config (YAML → env → CLI)
    cfg = load_config(config_path=args.config, cli_args=args)
    if args.refine:
        cfg["augment"]["enabled"] = True
        cfg["augment"]["refine_responses"] = True
    logger.info("Log dir:    %s", cfg["log_dir"])
    logger.info("Output dir: %s", cfg["output_dir"])
    logger.info("Formats:    %s", ", ".join(cfg["formats"]))
    logger.info("Augment:    %s (provider=%s, refine=%s)",
                cfg["augment"]["enabled"],
                cfg["augment"]["provider"],
                cfg["augment"].get("refine_responses", False))

    # ── Step 1: Load & Join ────────────────────────────────────────
    logger.info("Step 1/4: loading data")
    sessions = load_sessions(Path(cfg["log_dir"]), min_messages=cfg["min_messages_per_session"])
    behaviours = load_behaviours(Path(cfg["log_dir"]))
    labelled = join(sessions, behaviours)

    if not labelled:
        logger.error("No labelled sessions found — is LOG_DIR correct?")
        sys.exit(1)

    # ── Step 2: (Optional) LLM-refine responses for DPO chosen ─────
    refinements: dict[str, dict[str, str]] = {}
    if (cfg["augment"]["enabled"]
            and cfg["augment"].get("refine_responses")
            and "dpo" in cfg["formats"]):
        from .augmenter import run_refine
        logger.info("Step 2/4: refining responses via %s for DPO chosen",
                    cfg["augment"]["provider"])
        rules = cfg.get("dpo_rules", {})
        scoring = {**DEFAULT_SCORING, **(rules.get("scoring") or {})}
        positive_sources = set(rules.get("positive_sources") or
                               ["llm_rewrite", "like", "unmarked"])
        negative_sources = set(rules.get("negative_sources") or
                               ["retry", "dislike"])
        groups = _build_candidate_groups(
            labelled,
            scoring=scoring,
            positive_sources=positive_sources,
            negative_sources=negative_sources,
            min_response_length=int(rules.get("min_response_length", 10)),
        )
        # Decide which groups need an LLM-synthesised chosen reply.
        trigger = rules.get("refine_trigger", "no_positive")
        threshold = int(rules.get("llm_rewrite_when_max_score_below", 3))
        if trigger == "always":
            groups_to_refine = groups
        elif trigger == "below_threshold":
            groups_to_refine = [
                g for g in groups
                if max(c["score"] for c in g["candidates"]) < threshold
            ]
        else:  # "no_positive" (default)
            groups_to_refine = [
                g for g in groups
                if not any(c["positive"] for c in g["candidates"])
            ]
        logger.info("  refine_trigger=%s → refining %d / %d groups",
                    trigger, len(groups_to_refine), len(groups))
        if groups_to_refine:
            refinements = run_refine(groups_to_refine, cfg["augment"])
    else:
        logger.info("Step 2/4: skipping response refinement")

    # ── Step 3: Convert to training formats ─────────────────────────
    logger.info("Step 3/4: converting to training formats")
    _convert(
        labelled,
        output_dir=Path(cfg["output_dir"]),
        enabled_formats=cfg["formats"],
        split_ratios=tuple(cfg["split_ratios"]),
        dpo_rules=cfg.get("dpo_rules", {}),
        augmented_refinements=refinements,
        random_seed=cfg["random_seed"],
    )

    # ── Step 4: Augment SFT (question rewrites, optional) ──────────
    if cfg["augment"]["enabled"] and "sft" in cfg["formats"]:
        from .augmenter import run_augment
        logger.info("Step 4/4: augmenting SFT via %s", cfg["augment"]["provider"])
        out_dir = Path(cfg["output_dir"])
        for split in ("train", "val"):
            path = out_dir / f"sft_{split}.jsonl"
            if not path.exists():
                continue
            samples = [json.loads(l) for l in path.read_text("utf-8").strip().splitlines() if l.strip()]
            augmented = run_augment(samples, cfg["augment"])
            aug_path = out_dir / f"sft_{split}_augmented.jsonl"
            with open(aug_path, "w", encoding="utf-8") as f:
                for s in augmented:
                    f.write(json.dumps(s, ensure_ascii=False) + "\n")
            logger.info("  %s → %s (%d samples)", path.name, aug_path.name, len(augmented))

    # ── Step 5: Deploy (optional) ───────────────────────────────────
    if args.deploy:
        from .deploy import generate_modelfile, generate_vllm_config, generate_run_script
        logger.info("Step 5/5: generating deployment artifacts")
        generate_modelfile(cfg["deploy"])
        generate_vllm_config(cfg["deploy"], cfg["output_dir"])
        generate_run_script(cfg["deploy"])

    logger.info("Done. Output: %s", cfg["output_dir"])


if __name__ == "__main__":
    main()
