"""
Converter — transforms joined labelled sessions into training-ready formats.

Supported output formats:
  SFT:  {"messages": [{"role":..., "content":...}, ...]}
  DPO:  {"prompt": [...], "chosen": [...], "rejected": [...]}
  GRPO: {"prompt": [{"role":"system",...}, {"role":"user",...}]}

Label vocabulary (set by loader.join):
  "like" | "dislike" | "retry" | "unmarked" | "llm_rewrite"

DPO uses a candidate-group scoring model — all assistant replies to the same
user turn are ranked, then preference pairs are formed (top vs bottom by
default; configurable via dpo_rules in the YAML config).
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("training_data.converter")

SYSTEM_PROMPT = "你是一个乐于助人的AI助手，请根据对话历史回答用户的问题。"

# Schema version stamp. Bump when changing the export schema.
# Roadmap: aichat-1.0 (now) → atif-1.0 (W2, after turn_logger keeps tool_calls).
SCHEMA_VERSION = "aichat-1.0"

# Default scoring table; overridable via config["dpo_rules"]["scoring"].
DEFAULT_SCORING = {
    "llm_rewrite": 4,
    "like": 3,
    "unmarked": 2,
    "retry": 1,
    "dislike": 0,
}


def _common_meta() -> dict:
    """Metadata attached to every exported sample for lineage tracing."""
    return {
        "environment": os.getenv("APP_ENV", "dev"),
        "exported_at": datetime.now(timezone.utc).isoformat() + "Z",
        "schema_version": SCHEMA_VERSION,
    }


# ── SFT ───────────────────────────────────────────────────────────


def _to_sft_samples(labelled_sessions: list[dict]) -> list[dict]:
    """Convert sessions to SFT format.

    Strategy:
      - Include every assistant reply whose label is NOT a hard rejection
        (dislike). Retries are kept because the response text itself is still
        a valid (if imperfect) teacher signal.
      - Sliding window: each user turn yields one sample with up to ~4
        preceding turn-pairs as context.
    """
    samples = []
    for sess in labelled_sessions:
        msgs = sess["messages"]
        labels = sess.get("labels", {})
        i = 0
        while i < len(msgs):
            if msgs[i]["role"] != "user":
                i += 1
                continue
            j = i + 1
            while j < len(msgs) and msgs[j]["role"] != "user":
                j += 1
            if j > i + 1:
                start = max(0, i - 8)
                context = msgs[start:j]
                # Collect all assistant labels in this window
                ai_labels = [
                    labels.get(m["message_id"], "unmarked")
                    for m in context
                    if m["role"] == "assistant"
                ]
                # Skip the whole window ONLY if every assistant turn is disliked
                if ai_labels and all(lab == "dislike" for lab in ai_labels):
                    pass
                else:
                    primary_label = ai_labels[0] if ai_labels else "unmarked"
                    samples.append({
                        "messages":
                            [{"role": "system", "content": SYSTEM_PROMPT}]
                            + [
                                {"role": m["role"], "content": m["content"]}
                                for m in context
                            ],
                        "source": "aichat",
                        "session_id": sess["session_id"],
                        "label": primary_label,
                        "labels": {
                            "thumb": primary_label,
                            "score": DEFAULT_SCORING.get(primary_label, 2),
                        },
                        "schema_version": SCHEMA_VERSION,
                        "metadata": _common_meta(),
                    })
            i = j
    logger.info("SFT: %d samples generated", len(samples))
    return samples


# ── DPO (scoring model) ───────────────────────────────────────────


def _split_user_turns(msgs: list[dict]) -> list[tuple[list[dict], list[dict]]]:
    """Split a flat message list into (context, assistant_candidates) groups.

    Each group is anchored at one user turn:
        context            — all messages from the start of the session up to
                             AND INCLUDING the user turn
        assistant_candidates — every assistant message that immediately follows
                             that user turn (handles v4.2.12 retry chains: a
                             completed reply that gets retried stays in the
                             history, then a new assistant message appears)
    Returns a list of (context, candidates) tuples, one per user turn.
    """
    groups: list[tuple[list[dict], list[dict]]] = []
    i = 0
    while i < len(msgs):
        if msgs[i]["role"] != "user":
            i += 1
            continue
        j = i + 1
        while j < len(msgs) and msgs[j]["role"] != "user":
            j += 1
        context = msgs[:i + 1]
        candidates = [m for m in msgs[i + 1:j] if m["role"] == "assistant"]
        if candidates:
            groups.append((context, candidates))
        i = j
    return groups


def _label_to_source(label: str | None) -> str:
    """Map a session label to a scoring source name."""
    if label in ("like", "dislike", "retry", "unmarked", "llm_rewrite"):
        return label or "unmarked"
    return "unmarked"


def _build_candidate_groups(
    labelled_sessions: list[dict],
    scoring: dict[str, int],
    positive_sources: set[str],
    negative_sources: set[str],
    min_response_length: int,
) -> list[dict]:
    """For each user turn, build a candidate group with scored entries.

    Returns:
        [{
            "session_id": str,
            "context": [msg, ...],             # ending with the user turn
            "user_msg": {role, content},
            "candidates": [{
                "content": str, "label": str, "score": int,
                "positive": bool, "negative": bool,
            }],
        }, ...]
    """
    groups = []
    for sess in labelled_sessions:
        labels = sess.get("labels", {})
        for context, candidates in _split_user_turns(sess["messages"]):
            user_msg = context[-1] if context else None
            if not user_msg:
                continue
            scored = []
            for c in candidates:
                label = labels.get(c.get("message_id", ""), "unmarked")
                source = _label_to_source(label)
                content = c.get("content", "")
                if len(content) < min_response_length:
                    continue
                score = scoring.get(source, 0)
                scored.append({
                    "content": content,
                    "label": source,
                    "score": score,
                    "positive": source in positive_sources,
                    "negative": source in negative_sources,
                })
            if not scored:
                continue
            groups.append({
                "session_id": sess["session_id"],
                "context": context,
                "user_msg": user_msg,
                "candidates": scored,
            })
    return groups


def _pair_group(group: dict, strategy: str, max_pairs: int,
                min_score_gap: int, rng: random.Random) -> list[dict]:
    """Form DPO pairs from one candidate group.

    Positives = candidates flagged positive. Negatives = candidates flagged
    negative.  Strategies:
      top_bottom  — highest-scoring positive vs lowest-scoring negative (1 pair)
      one_to_many — best positive paired with each negative (up to max_pairs)
      cartesian   — every (positive, negative) combination (capped at max_pairs)
    """
    positives = [c for c in group["candidates"] if c["positive"]]
    negatives = [c for c in group["candidates"] if c["negative"]]
    if not positives or not negatives:
        return []

    positives.sort(key=lambda c: -c["score"])
    negatives.sort(key=lambda c: c["score"])

    sys_prompt = {"role": "system", "content": SYSTEM_PROMPT}
    prompt = [sys_prompt] + [
        {"role": m["role"], "content": m["content"]} for m in group["context"]
    ]

    pairs: list[dict] = []
    if strategy == "top_bottom":
        chosen, rejected = positives[0], negatives[0]
        if chosen["score"] - rejected["score"] >= min_score_gap:
            pairs.append((chosen, rejected))
    elif strategy == "one_to_many":
        chosen = positives[0]
        for rejected in negatives[:max_pairs]:
            if chosen["score"] - rejected["score"] >= min_score_gap:
                pairs.append((chosen, rejected))
    elif strategy == "cartesian":
        for chosen, rejected in itertools.product(positives, negatives):
            if chosen["score"] - rejected["score"] >= min_score_gap:
                pairs.append((chosen, rejected))
            if len(pairs) >= max_pairs:
                break
    else:
        logger.warning("Unknown pair_strategy: %s — falling back to top_bottom", strategy)
        chosen, rejected = positives[0], negatives[0]
        if chosen["score"] - rejected["score"] >= min_score_gap:
            pairs.append((chosen, rejected))

    out = []
    for chosen, rejected in pairs:
        out.append({
            "prompt": prompt,
            "chosen": [{"role": "assistant", "content": chosen["content"]}],
            "rejected": [{"role": "assistant", "content": rejected["content"]}],
            "source": "aichat-dpo",
            "session_id": group["session_id"],
            "chosen_label": chosen["label"],
            "rejected_label": rejected["label"],
            "score_gap": chosen["score"] - rejected["score"],
            "schema_version": SCHEMA_VERSION,
            "metadata": _common_meta(),
        })
    return out


def _to_dpo_pairs(labelled_sessions: list[dict],
                  rules: dict | None = None,
                  augmented_refinements: dict[str, str] | None = None,
                  random_seed: int = 42) -> list[dict]:
    """Build DPO preference pairs via candidate-group scoring.

    Args:
        labelled_sessions: output of loader.join()
        rules:             dpo_rules dict from config (scoring/sources/etc.)
        augmented_refinements: optional {session_id: {user_content: refined_reply}}
                              produced by augmenter; injected as llm_rewrite
                              candidates (score=scoring["llm_rewrite"])
        random_seed:       for shuffling output
    """
    rules = rules or {}
    scoring = {**DEFAULT_SCORING, **(rules.get("scoring") or {})}
    positive_sources = set(rules.get("positive_sources") or
                           ["llm_rewrite", "like", "unmarked"])
    negative_sources = set(rules.get("negative_sources") or
                           ["retry", "dislike"])
    pair_strategy = rules.get("pair_strategy", "top_bottom")
    max_pairs = int(rules.get("max_pairs_per_group", 3))
    min_score_gap = int(rules.get("min_score_gap", 1))
    min_resp_len = int(rules.get("min_response_length", 10))

    groups = _build_candidate_groups(
        labelled_sessions, scoring, positive_sources, negative_sources,
        min_resp_len,
    )

    # Inject LLM-refined responses as additional chosen candidates.
    if augmented_refinements:
        llm_score = scoring.get("llm_rewrite", 4)
        for g in groups:
            sess_id = g["session_id"]
            user_content = g["user_msg"].get("content", "")
            refined = augmented_refinements.get(sess_id, {}).get(user_content)
            if refined and len(refined) >= min_resp_len:
                g["candidates"].append({
                    "content": refined,
                    "label": "llm_rewrite",
                    "score": llm_score,
                    "positive": "llm_rewrite" in positive_sources,
                    "negative": False,  # never a rejected source
                })

    rng = random.Random(random_seed)
    pairs: list[dict] = []
    skipped = 0
    for g in groups:
        new_pairs = _pair_group(g, pair_strategy, max_pairs, min_score_gap, rng)
        if new_pairs:
            pairs.extend(new_pairs)
        else:
            skipped += 1

    rng.shuffle(pairs)
    logger.info(
        "DPO: %d groups → %d pairs (skipped %d groups with no valid pair; "
        "strategy=%s, gap≥%d)",
        len(groups), len(pairs), skipped, pair_strategy, min_score_gap,
    )
    return pairs


# ── GRPO ──────────────────────────────────────────────────────────


def _to_grpo_prompts(labelled_sessions: list[dict]) -> list[dict]:
    """Convert sessions to GRPO prompt format (system + user only)."""
    prompts = []
    for sess in labelled_sessions:
        msgs = sess["messages"]
        i = 0
        while i < len(msgs):
            if msgs[i]["role"] != "user":
                i += 1
                continue
            j = i + 1
            while j < len(msgs) and msgs[j]["role"] != "user":
                j += 1
            start = max(0, i - 8)
            context = msgs[start:i + 1]
            prompts.append({
                "prompt": [{"role": "system", "content": SYSTEM_PROMPT}]
                          + [{"role": m["role"], "content": m["content"]} for m in context],
                "source": "aichat-grpo",
                "session_id": sess["session_id"],
                "schema_version": SCHEMA_VERSION,
                "metadata": _common_meta(),
            })
            i = j
    logger.info("GRPO: %d prompts generated", len(prompts))
    return prompts


# ── Orchestrator ──────────────────────────────────────────────────


def convert(labelled_sessions: list[dict], output_dir: Path,
            enabled_formats: list[str],
            split_ratios: tuple = (0.8, 0.1, 0.1),
            dpo_rules: dict | None = None,
            augmented_refinements: dict[str, str] | None = None,
            random_seed: int = 42):
    """Convert labelled sessions to specified training formats and save to disk."""
    converters = {
        "sft": lambda sessions: _to_sft_samples(sessions),
        "dpo": lambda sessions: _to_dpo_pairs(
            sessions, rules=dpo_rules,
            augmented_refinements=augmented_refinements,
            random_seed=random_seed,
        ),
        "grpo": lambda sessions: _to_grpo_prompts(sessions),
    }
    for fmt in enabled_formats:
        if fmt not in converters:
            logger.warning("Unknown format: %s", fmt)
            continue
        data = converters[fmt](labelled_sessions)
        if not data:
            logger.warning("No %s samples generated", fmt)
            continue

        rng = random.Random(random_seed)
        rng.shuffle(data)
        n = len(data)
        n_train = int(n * split_ratios[0])
        n_val = int(n * split_ratios[1])
        splits = {
            "train": data[:n_train],
            "val": data[n_train:n_train + n_val],
            "test": data[n_train + n_val:],
        }
        for split_name, split_data in splits.items():
            if not split_data:
                continue
            path = output_dir / f"{fmt}_{split_name}.jsonl"
            with open(path, "w", encoding="utf-8") as f:
                for item in split_data:
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
            logger.info("  %s/%s: %d samples → %s", fmt, split_name, len(split_data), path)
    logger.info("Conversion complete")
