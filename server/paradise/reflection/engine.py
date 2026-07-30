"""ReflectionEngine — Agent self-reflection using a small model.

Three trigger modes:
  - post_turn: lightweight fact extraction after each conversation turn
  - idle: deeper reflection when agent has been idle for N minutes
  - scheduled: deep periodic self-review (daily)
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paradise.core.agent import ParadiseAgent

from paradise.config import ReflectionConfig
from paradise.emotion.engine import EmotionState
from paradise.reflection.triggers import TriggerPolicy
from paradise.reflection.storage import ReflectionStorage, ReflectionEntry
from paradise.reflection.prompts import (
    build_post_turn_prompt,
    build_idle_prompt,
    build_scheduled_prompt,
)
from paradise.transports.base import ProviderTransport

logger = logging.getLogger(__name__)


class ReflectionEngine:
    """Agent self-reflection engine powered by a small local model."""

    def __init__(self, transport: ProviderTransport, config: ReflectionConfig, workspace_root):
        self._transport = transport
        self._config = config
        self._triggers = TriggerPolicy(
            post_turn=config.post_turn,
            idle_minutes=config.idle_minutes,
            idle_max_context_turns=config.idle_max_context_turns,
        )
        self._storage = ReflectionStorage(workspace_root / "reflections")
        self._workspace_root = workspace_root  # for writing back to memory.md
        self._last_turn_time: float = time.monotonic()
        self._idle_task = None

    @property
    def storage(self) -> ReflectionStorage:
        return self._storage

    async def on_turn_complete(
        self,
        agent_name: str,
        user_message: str,
        agent_response: str,
        emotion_state: EmotionState,
    ) -> None:
        """Post-turn lightweight reflection: extract facts + calibrate emotion."""
        if not self._triggers.should_post_turn():
            return

        prompt = build_post_turn_prompt(agent_name, user_message, agent_response, emotion_state)

        try:
            result = await self._call_reflection_model(prompt)
            parsed = self._parse_json_response(result)

            # Extract facts
            facts = parsed.get("key_facts", [])
            entry = ReflectionEntry(
                type="post_turn",
                facts=facts,
                emotion_delta=parsed.get("emotion_delta", {}),
                insights=parsed.get("user_impression", ""),
                raw_response=result,
            )
            self._storage.append(entry)

            # Write facts back to memory.md (hermes-style: background memory update)
            if facts:
                self._write_facts_to_memory(facts)

            # Apply emotion delta
            if parsed.get("emotion_delta"):
                self._apply_emotion_delta(emotion_state, parsed["emotion_delta"])

            self._last_turn_time = time.monotonic()
            logger.debug("[Reflection] Post-turn: %d facts extracted", len(facts))

        except Exception as e:
            logger.warning("[Reflection] Post-turn failed: %s", e)

    async def on_idle(
        self,
        agent_name: str,
        idle_minutes: float,
        recent_conversations: str,
        current_memory: str,
        emotion_state: EmotionState,
    ) -> None:
        """Idle-time deeper reflection: review + summarize + memory update."""
        if not self._triggers.should_idle(idle_minutes):
            return

        prompt = build_idle_prompt(
            agent_name, idle_minutes, recent_conversations, current_memory, emotion_state
        )

        try:
            result = await self._call_reflection_model(prompt)
            parsed = self._parse_json_response(result)

            entry = ReflectionEntry(
                type="idle",
                memory_updates=parsed.get("memory_updates", ""),
                emotion_delta=parsed.get("emotion_adjustment", {}),
                insights=parsed.get("insights", ""),
                next_actions=parsed.get("next_actions", ""),
                raw_response=result,
            )
            self._storage.append(entry)

            # Apply emotion adjustments
            if parsed.get("emotion_adjustment"):
                self._apply_emotion_delta(emotion_state, parsed["emotion_adjustment"])

            # Write memory updates back to memory.md
            if parsed.get("memory_updates"):
                self._update_memory_md(parsed["memory_updates"])

            logger.info("[Reflection] Idle reflection completed for %s", agent_name)
            return entry

        except Exception as e:
            logger.warning("[Reflection] Idle reflection failed: %s", e)

    async def on_scheduled(
        self,
        agent_name: str,
        all_conversations: str,
    ) -> ReflectionEntry | None:
        """Scheduled deep reflection."""
        recent_reflections = self._storage.read_as_text(days=7)
        prompt = build_scheduled_prompt(agent_name, all_conversations, recent_reflections)

        try:
            result = await self._call_reflection_model(prompt)
            parsed = self._parse_json_response(result)

            entry = ReflectionEntry(
                type="scheduled",
                memory_updates=parsed.get("memory_compression", ""),
                insights=parsed.get("personality_growth", ""),
                emotion_delta=parsed.get("emotion_baseline", {}),
                raw_response=result,
            )
            self._storage.append(entry)
            logger.info("[Reflection] Scheduled deep reflection completed for %s", agent_name)
            return entry

        except Exception as e:
            logger.warning("[Reflection] Scheduled reflection failed: %s", e)
            return None

    def _write_facts_to_memory(self, facts: list[str]) -> None:
        """Append extracted facts to memory.md (hermes-style immediate write-back)."""
        from pathlib import Path
        memory_path = self._workspace_root / "memory.md"
        try:
            existing = ""
            if memory_path.exists():
                existing = memory_path.read_text(encoding="utf-8")

            # Normalize facts: handle both string and dict formats
            normalized = []
            for fact in facts:
                if isinstance(fact, dict):
                    name = fact.get("name", "")
                    value = fact.get("value", "")
                    if name and value:
                        normalized.append(f"用户的{name}是{value}")
                    elif value:
                        normalized.append(str(value))
                elif isinstance(fact, str):
                    fact = fact.strip()
                    if fact:
                        normalized.append(fact)

            # Deduplicate: skip facts already in memory
            existing_lines = set(existing.splitlines())
            new_lines = []
            for fact in normalized:
                formatted = f"- {fact}"
                if formatted not in existing_lines and fact not in existing:
                    new_lines.append(formatted)

            if new_lines:
                separator = "\n" if existing and not existing.endswith("\n") else ""
                with open(memory_path, "a", encoding="utf-8") as f:
                    f.write(separator + "\n".join(new_lines) + "\n")
                logger.info("[Reflection] Wrote %d facts to memory.md", len(new_lines))
        except Exception as e:
            logger.warning("[Reflection] Failed to write facts to memory.md: %s", e)

    def _update_memory_md(self, memory_updates: str) -> None:
        """Apply memory updates from idle/scheduled reflection."""
        if not memory_updates or not memory_updates.strip():
            return
        from pathlib import Path
        memory_path = self._workspace_root / "memory.md"
        try:
            existing = ""
            if memory_path.exists():
                existing = memory_path.read_text(encoding="utf-8")

            # Append the memory updates section
            separator = "\n" if existing and not existing.endswith("\n") else ""
            with open(memory_path, "a", encoding="utf-8") as f:
                f.write(separator + memory_updates.strip() + "\n")
            logger.info("[Reflection] Updated memory.md with idle reflection")
        except Exception as e:
            logger.warning("[Reflection] Failed to update memory.md: %s", e)

    async def _call_reflection_model(self, prompt: str) -> str:
        """Call the reflection model via Transport. Non-streaming."""
        messages = [{"role": "user", "content": prompt}]
        try:
            if hasattr(self._transport, 'chat'):
                # Build kwargs based on transport type
                kwargs = {
                    "model": self._config.llm.model,
                    "system_prompt": "You are a JSON-only response system. Output valid JSON only.",
                    "messages": messages,
                    "temperature": self._config.llm.temperature,
                }
                mode = getattr(self._transport, 'api_mode', '')
                if mode == "ollama_native":
                    kwargs["api_url"] = self._config.llm.api_url
                else:
                    kwargs["api_url"] = self._config.llm.api_url
                    kwargs["api_key"] = self._config.llm.api_key
                    kwargs["max_tokens"] = self._config.llm.max_tokens

                result = await self._transport.chat(**kwargs)
                if hasattr(result, 'content'):
                    return result.content or ""
                return str(result)
            return ""
        except Exception as e:
            logger.error("[Reflection] Model call failed: %s", e)
            raise

    @staticmethod
    def _parse_json_response(raw: str) -> dict:
        """Try to extract JSON from model response."""
        # Try direct parse
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # Try extracting JSON from markdown code block
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # Try finding first { ... } block
        m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {}

    @staticmethod
    def _apply_emotion_delta(state: EmotionState, delta: dict) -> None:
        """Apply small emotion deltas from reflection."""
        for key in ("hunger", "energy", "affection", "boredom"):
            if key in delta:
                val = float(delta[key])
                val = max(-0.1, min(0.1, val))  # Clamp to ±0.1
                current = getattr(state, key, 0)
                setattr(state, key, current + val)
        state.clamp()
