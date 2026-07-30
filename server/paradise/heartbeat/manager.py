"""HeartbeatManager — periodic heartbeat scheduling and execution."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Callable, Any

from paradise.config import HeartbeatConfig
from paradise.heartbeat.decision import HeartbeatDecision, parse_decision
from paradise.emotion.engine import EmotionEngine, EmotionState
from paradise.memory.workspace import Workspace
from paradise.prompt.builder import prompt_builder
from paradise.transports.base import ProviderTransport

logger = logging.getLogger(__name__)


class HeartbeatManager:
    """Manages heartbeat scheduling for agents.

    Uses Transport abstraction for LLM calls (no more hardcoded _quick_llm_call).
    """

    def __init__(self, transport: ProviderTransport, config: HeartbeatConfig):
        self._transport = transport
        self._config = config
        self._tasks: dict[str, asyncio.Task] = {}
        self._callbacks: dict[str, Callable] = {}
        self._workspaces: dict[str, Workspace] = {}
        self._emotions: dict[str, EmotionEngine] = {}
        self._states: dict[str, EmotionState] = {}
        self._llm_configs: dict[str, dict] = {}
        self._last_spoke: dict[str, float] = {}

    def register(
        self,
        agent_id: str,
        workspace: Workspace,
        emotion_engine: EmotionEngine,
        emotion_state: EmotionState,
        on_autonomous: Callable,
        llm_config: dict,
    ) -> None:
        """Register an agent for heartbeat scheduling."""
        self._workspaces[agent_id] = workspace
        self._emotions[agent_id] = emotion_engine
        self._states[agent_id] = emotion_state
        self._callbacks[agent_id] = on_autonomous
        self._llm_configs[agent_id] = llm_config

    def start(self, agent_id: str) -> None:
        """Start heartbeat loop for an agent."""
        if agent_id in self._tasks:
            return
        jitter = random.randint(0, self._config.jitter_max_seconds)
        task = asyncio.create_task(self._heartbeat_loop(agent_id, jitter))
        self._tasks[agent_id] = task
        logger.info("[Heartbeat] Started for %s (jitter=%ds)", agent_id, jitter)

    def stop(self, agent_id: str) -> None:
        """Stop heartbeat for an agent."""
        task = self._tasks.pop(agent_id, None)
        if task:
            task.cancel()
            logger.info("[Heartbeat] Stopped for %s", agent_id)

    def unregister(self, agent_id: str) -> None:
        """Stop and remove all data for an agent."""
        self.stop(agent_id)
        self._workspaces.pop(agent_id, None)
        self._emotions.pop(agent_id, None)
        self._states.pop(agent_id, None)
        self._callbacks.pop(agent_id, None)
        self._llm_configs.pop(agent_id, None)
        self._last_spoke.pop(agent_id, None)

    async def _heartbeat_loop(self, agent_id: str, jitter: int) -> None:
        """Main heartbeat loop."""
        try:
            await asyncio.sleep(jitter)
            while True:
                await self._execute_beat(agent_id)
                await asyncio.sleep(self._config.interval_seconds)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[Heartbeat] Loop error for %s: %s", agent_id, e)

    async def _execute_beat(self, agent_id: str) -> None:
        """Execute one heartbeat cycle: GATHER → THINK → ACT."""
        import time

        workspace = self._workspaces.get(agent_id)
        engine = self._emotions.get(agent_id)
        state = self._states.get(agent_id)
        if not workspace or not engine or not state:
            return

        # Rate limiting
        now = time.monotonic()
        last = self._last_spoke.get(agent_id, 0)
        if now - last < self._config.cooldown_seconds:
            return

        # GATHER
        engine.tick(state)
        workspace.save_state(state.to_dict())

        trigger = engine.on_heartbeat(state)
        soul_md = workspace.read_md("soul.md")
        heartbeat_md = workspace.read_md("heartbeat.md")
        agents_md = workspace.read_md("agents.md")
        recent = self._gather_recent_context(workspace)

        # THINK
        system_prompt, user_prompt = prompt_builder.build_heartbeat(
            agent_name=agent_id,
            soul_md=soul_md,
            heartbeat_md=heartbeat_md,
            agents_md=agents_md,
            emotion_state=state.to_dict(),
            recent_context=recent,
            trigger_reason=trigger or "定期心跳",
        )

        decision = await self._llm_decide(agent_id, system_prompt, user_prompt)

        # ACT
        if decision.action.value != "SILENT" and decision.message:
            self._last_spoke[agent_id] = time.monotonic()
            # Persist to heartbeat-plaza session
            workspace.append_session("heartbeat-plaza", {
                "role": "assistant",
                "content": decision.message,
                "action": decision.action.value,
                "target": decision.target,
                "timestamp": time.time(),
            })
            # Notify via callback
            callback = self._callbacks.get(agent_id)
            if callback:
                await callback(agent_id, decision)

    async def _llm_decide(self, agent_id: str, system_prompt: str, user_prompt: str) -> HeartbeatDecision:
        """Use Transport to get LLM decision."""
        llm_config = self._llm_configs.get(agent_id, {})
        try:
            messages = [{"role": "user", "content": user_prompt}]
            if hasattr(self._transport, 'chat'):
                api_mode = getattr(self._transport, 'api_mode', '')
                chat_kwargs = {
                    "model": llm_config.get("model", ""),
                    "system_prompt": system_prompt,
                    "messages": messages,
                    "temperature": 0.5,
                    "max_tokens": 512,
                }
                if api_mode == "ollama_native":
                    chat_kwargs["api_url"] = llm_config.get("api_url", "http://localhost:11434")
                else:
                    chat_kwargs["api_url"] = llm_config.get("api_url", "")
                    chat_kwargs["api_key"] = llm_config.get("api_key", "")

                result = await self._transport.chat(**chat_kwargs)
                content = result.content if hasattr(result, 'content') else str(result)
            else:
                return HeartbeatDecision()

            return parse_decision(content or "")
        except Exception as e:
            logger.error("[Heartbeat] LLM decide error for %s: %s", agent_id, e)
            return HeartbeatDecision()

    def _gather_recent_context(self, workspace: Workspace, max_items: int = 20) -> str:
        """Gather recent messages from all sessions."""
        parts = []
        sessions_dir = workspace.root / "sessions"
        if not sessions_dir.exists():
            return ""

        for session_dir in sorted(sessions_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            for jsonl_file in sorted(session_dir.glob("*.jsonl")):
                try:
                    lines = jsonl_file.read_text("utf-8").strip().splitlines()
                    for line in lines[-5:]:
                        import json
                        entry = json.loads(line)
                        role = entry.get("role", "?")
                        content = entry.get("content", "")[:100]
                        parts.append(f"[{session_dir.name}] {role}: {content}")
                except Exception:
                    continue

        if len(parts) > max_items:
            parts = parts[-max_items:]
        return "\n".join(parts)
