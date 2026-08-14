"""Shared context builder for subgraph system prompts + history extraction.

All subgraph patterns (chat, tool_react, plan_execute) call these helpers
to turn a HandoffRequest.context dict into:
  1. A persona-aware system prompt (soul_md + memory_md + user_profile + compact_context)
  2. A capped conversation-history message list

The builder is context-shape-aware: when ``focused_mode=True`` is set in
the context (by plan_execute when delegating to a nested sub-agent), it
produces a FOCUSED prompt that excludes full conversation history and
instead emphasizes the specific sub-goal + prior step results. This
implements the multi-agent orchestration pattern: each nested sub-agent
only sees what it needs for its assigned step.

Phase 3-D.
"""
from __future__ import annotations

import logging
from typing import Any

from paradise.core.handoff import HandoffRequest

logger = logging.getLogger("paradise.patterns.context_builder")


def build_subgraph_system_prompt(req: HandoffRequest, base_prompt: str) -> str:
    """Build a system prompt with persona/memory/profile prefixed to base.

    Reads from ``req.context`` (set by supervisor's _build_handoff_request):
      * soul_md:       persona / character definition
      * memory_md:     long-term memory (facts, preferences)
      * user_profile:  user name / personality / description
      * compact_context: folded summary of older conversation turns

    When ``focused_mode`` is True (nested sub-agent from plan_execute),
    produces a focused prompt instead:
      "You are executing step N of a multi-step plan.
       Task goal: {task_goal}. Prior results: {prior_results}.
       Focus ONLY on your assigned step: {message}"

    Returns ``base_prompt`` unchanged when context is empty or all fields
    are blank — preserving exact Phase 2.10 behavior for tests / dev path.
    """
    ctx: dict[str, Any] = req.context or {}

    # ── Focused mode: nested sub-agent from plan_execute ───────────
    if ctx.get("focused_mode"):
        return _build_focused_prompt(req, base_prompt)

    # ── Normal mode: full persona + memory + profile injection ─────
    parts: list[str] = []

    soul_md = (ctx.get("soul_md") or "").strip()
    if soul_md:
        parts.append(f"# 角色设定\n{soul_md[:800]}")

    memory_md = (ctx.get("memory_md") or "").strip()
    if memory_md:
        parts.append(f"# 长期记忆\n{memory_md[:1000]}")

    user_profile = (ctx.get("user_profile") or "").strip()
    if user_profile:
        parts.append(f"# 用户信息\n{user_profile[:300]}")

    compact = (ctx.get("compact_context") or "").strip()
    if compact:
        parts.append(f"# 早期对话摘要\n{compact[:1500]}")

    if not parts:
        return base_prompt

    return "\n\n".join(parts) + "\n\n" + base_prompt


def _build_focused_prompt(req: HandoffRequest, base_prompt: str) -> str:
    """Build a focused prompt for a nested sub-agent executing one step.

    The nested agent sees:
      * The overall task goal (so it understands the big picture)
      * Its specific sub-goal (req.message)
      * Results of prior steps (so it can build on them)
      * Persona (soul_md) for voice consistency

    It does NOT see:
      * Full conversation history (the parent already decomposed context)
      * compact_context (irrelevant to a single-step execution)
    """
    ctx: dict[str, Any] = req.context or {}

    task_goal = (ctx.get("task_goal") or "").strip()
    prior_results_raw = ctx.get("prior_results") or []
    step_index = ctx.get("parent_step", 0)

    # Format prior results as a concise list
    prior_str = ""
    if prior_results_raw:
        lines = []
        for i, r in enumerate(prior_results_raw):
            text = r if isinstance(r, str) else str(r.get("output", r))
            if text:
                lines.append(f"  [{i+1}] {text[:300]}")
        if lines:
            prior_str = "\n\nPrior step results:\n" + "\n".join(lines)

    soul_md = (ctx.get("soul_md") or "").strip()
    persona_line = f"\n\nPersona: {soul_md[:400]}" if soul_md else ""

    # agent_team sub-agents carry a role label — use it in the header
    # so the model knows it's a team member, not a step executor.
    agent_role = (ctx.get("agent_role") or "").strip()
    if agent_role:
        header_line = f"You are the '{agent_role}' agent in a multi-agent team."
    else:
        header_line = (
            f"You are executing step {step_index + 1} of a multi-step plan."
        )

    focus_header = (
        f"{header_line}"
        f"\n\nOverall task goal: {task_goal or '(not specified)'}"
        f"{prior_str}"
        f"\n\nYour assigned task: {req.message}"
        f"{persona_line}"
        f"\n\nFocus ONLY on your assigned task. Do not attempt other tasks."
    )

    return focus_header + "\n\n" + base_prompt


def extract_history_messages(
    req: HandoffRequest,
    max_turns: int = 10,
) -> list[dict]:
    """Pull conversation history from req.context, cap to last N messages.

    The history comes from ``req.context["conversation_history"]`` (set by
    the supervisor from LoopContext._raw_messages, which in turn comes from
    the Android app's full message array).

    Behavior:
      * Returns [] when no history is present (first turn, or focused_mode).
      * When ``focused_mode`` is True, returns [] — nested sub-agents don't
        get conversation history, only their focused prompt.
      * Caps to the last ``max_turns`` messages to bound token cost.
      * Strips the final user message if present — the subgraph adds its
        own ``[{"role":"user","content":req.message}]`` seed, so including
        it twice would duplicate the current turn.
      * Filters out messages with empty content and system messages that
        carry tool-result injections (the subgraph manages its own tools).
    """
    ctx: dict[str, Any] = req.context or {}

    # Focused mode: no history for nested sub-agents
    if ctx.get("focused_mode"):
        return []

    raw_history = ctx.get("conversation_history")
    if not raw_history or not isinstance(raw_history, list):
        return []

    # Filter: keep only user/assistant turns with non-empty content.
    # Skip tool/system messages — subgraph manages its own tool context.
    cleaned: list[dict] = []
    for msg in raw_history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if content is None:
            continue
        # Multimodal content (list of parts) — keep as-is
        if isinstance(content, str) and not content.strip():
            continue
        cleaned.append({"role": role, "content": content})

    # Cap to last max_turns messages
    if len(cleaned) > max_turns:
        cleaned = cleaned[-max_turns:]

    # Strip trailing user message if present — the subgraph will add its
    # own current-turn user message from req.message. This prevents
    # duplication when the Android app sends the full history including
    # the current turn.
    if cleaned and cleaned[-1].get("role") == "user":
        cleaned = cleaned[:-1]

    return cleaned


__all__ = [
    "build_subgraph_system_prompt",
    "extract_history_messages",
]
