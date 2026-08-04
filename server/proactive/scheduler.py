"""ProactiveScheduler — periodic agent-initiated messaging.

For each registered conversation, runs a lightweight LLM call at a configurable
interval to decide whether to send a proactive message. If the LLM generates
content, it's queued for Android to pick up via long-polling.

Architecture:
  register(conv_id) → starts asyncio task
  _loop: sleep(interval) → _check_and_generate(conv_id) → queue message
  poll(conv_id, timeout): long-poll for next queued message

Decision prompt (lightweight, ~200 tokens):
  "Based on the recent conversation, decide if you should proactively
   reach out to the user. Consider: time since last message, conversation
   topics, user interests. If you decide to speak, generate a natural
   short message. If not, respond with exactly: SILENT"
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("proactive")


@dataclass
class ProactiveMessage:
    """A single proactive message queued for delivery to Android."""
    conv_id: str
    content: str
    reason: str = ""          # why the agent decided to speak
    timestamp: float = field(default_factory=time.time)
    delivered: bool = False

    def to_dict(self) -> dict:
        return {
            "conv_id": self.conv_id,
            "content": self.content,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "iso_time": datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat(),
        }


class ProactiveScheduler:
    """Manages periodic proactive message generation per conversation.

    Usage:
        scheduler = ProactiveScheduler(config)
        await scheduler.start()  # call once at server startup
        scheduler.register(conv_id, history_provider)
        message = await scheduler.poll(conv_id, timeout=30)
    """

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._enabled: bool = self._config.get("enabled", False)
        self._interval: int = self._config.get("interval_minutes", 30) * 60
        self._cooldown: int = self._config.get("cooldown_minutes", 60) * 60
        self._max_context_turns: int = self._config.get("max_context_turns", 10)
        self._model: str = self._config.get("model", "")
        self._ollama_base: str = self._config.get("ollama_base", "http://localhost:11434")

        # Per-conv state
        self._tasks: dict[str, asyncio.Task] = {}
        self._queues: dict[str, asyncio.Queue[ProactiveMessage]] = {}
        self._history_providers: dict[str, Any] = {}   # callable(conv_id) -> list[dict]
        self._last_user_message_ts: dict[str, float] = {}
        self._last_proactive_ts: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def update_config(self, config: dict) -> None:
        """Hot-reload configuration."""
        self._config.update(config)
        self._enabled = config.get("enabled", self._enabled)
        self._interval = config.get("interval_minutes", self._interval // 60) * 60
        self._cooldown = config.get("cooldown_minutes", self._cooldown // 60) * 60
        self._max_context_turns = config.get("max_context_turns", self._max_context_turns)
        self._model = config.get("model", self._model)
        logger.info(
            "Proactive config updated: enabled=%s interval=%ds cooldown=%ds model=%s",
            self._enabled, self._interval, self._cooldown, self._model,
        )

    def register(
        self,
        conv_id: str,
        history_provider: Any | None = None,
    ) -> None:
        """Register a conversation for proactive checks.

        Args:
            conv_id: Conversation ID
            history_provider: Optional async callable that returns recent messages
                              as list[{role, content, timestamp}]
        """
        if not self._enabled:
            logger.debug("Proactive disabled, skip register conv=%s", conv_id)
            return

        if conv_id in self._tasks:
            return  # already registered

        self._queues[conv_id] = asyncio.Queue()
        self._history_providers[conv_id] = history_provider
        self._tasks[conv_id] = asyncio.create_task(self._loop(conv_id))
        logger.info("Proactive registered: conv=%s", conv_id)

    def unregister(self, conv_id: str) -> None:
        """Stop proactive checks for a conversation."""
        task = self._tasks.pop(conv_id, None)
        if task:
            task.cancel()
        self._queues.pop(conv_id, None)
        self._history_providers.pop(conv_id, None)
        self._last_user_message_ts.pop(conv_id, None)
        self._last_proactive_ts.pop(conv_id, None)
        logger.info("Proactive unregistered: conv=%s", conv_id)

    def record_user_activity(self, conv_id: str) -> None:
        """Called when user sends a message — resets the cooldown timer."""
        self._last_user_message_ts[conv_id] = time.time()

    async def poll(self, conv_id: str, timeout: float = 30.0) -> dict | None:
        """Long-poll for the next proactive message.

        Returns a message dict, or None if timeout expires.
        """
        queue = self._queues.get(conv_id)
        if queue is None:
            return None

        try:
            msg = await asyncio.wait_for(queue.get(), timeout=timeout)
            msg.delivered = True
            return msg.to_dict()
        except asyncio.TimeoutError:
            return None

    def get_status(self) -> dict:
        """Return scheduler status for all registered conversations."""
        return {
            "enabled": self._enabled,
            "interval_seconds": self._interval,
            "cooldown_seconds": self._cooldown,
            "model": self._model,
            "registered_conversations": list(self._tasks.keys()),
            "pending_messages": {
                conv_id: queue.qsize()
                for conv_id, queue in self._queues.items()
            },
        }

    # ── Internal loop ──────────────────────────────────────────────

    async def _loop(self, conv_id: str) -> None:
        """Main proactive check loop for a single conversation."""
        # Wait one interval before first check (don't proactive immediately after register)
        await asyncio.sleep(self._interval)

        while True:
            try:
                await self._check_and_generate(conv_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Proactive check error conv=%s: %s", conv_id, e)
            await asyncio.sleep(self._interval)

    async def _check_and_generate(self, conv_id: str) -> None:
        """One proactive check cycle: GATHER → THINK → ACT."""
        now = time.time()

        # Cooldown: don't proactive if user recently sent a message
        last_user = self._last_user_message_ts.get(conv_id, 0)
        if now - last_user < self._cooldown:
            logger.debug(
                "Proactive skip (cooldown): conv=%s last_user=%.0fs ago",
                conv_id, now - last_user,
            )
            return

        # Also don't proactive if we recently sent one
        last_proactive = self._last_proactive_ts.get(conv_id, 0)
        if now - last_proactive < self._interval:
            return

        # GATHER: get recent conversation history
        provider = self._history_providers.get(conv_id)
        recent_messages = []
        if provider:
            try:
                if asyncio.iscoroutinefunction(provider):
                    recent_messages = await provider(conv_id)
                else:
                    recent_messages = provider(conv_id)
            except Exception as e:
                logger.warning("History provider error conv=%s: %s", conv_id, e)
                return

        if not recent_messages:
            # No conversation history — nothing to proactive about
            return

        # THINK: ask LLM if we should proactively reach out
        decision = await self._llm_decide(conv_id, recent_messages)

        # ACT: queue message if LLM decided to speak
        if decision and decision.get("should_speak"):
            content = decision.get("content", "").strip()
            reason = decision.get("reason", "")
            if content and content.upper() != "SILENT":
                msg = ProactiveMessage(
                    conv_id=conv_id,
                    content=content,
                    reason=reason,
                )
                self._queues[conv_id].put_nowait(msg)
                self._last_proactive_ts[conv_id] = now
                logger.info(
                    "Proactive message queued: conv=%s content=%s...",
                    conv_id, content[:60],
                )

    async def _llm_decide(self, conv_id: str, recent_messages: list[dict]) -> dict | None:
        """Call LLM to decide whether to proactively message the user.

        Returns {"should_speak": bool, "content": str, "reason": str} or None on error.
        """
        # Build a compact context from recent messages
        context_lines = []
        for msg in recent_messages[-self._max_context_turns:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")[:200]
            ts = msg.get("timestamp", 0)
            time_ago = ""
            if ts:
                ago = int(time.time() - ts / 1000) if ts > 1e12 else int(time.time() - ts)
                if ago < 60:
                    time_ago = f"({ago}s ago)"
                elif ago < 3600:
                    time_ago = f"({ago // 60}min ago)"
                else:
                    time_ago = f"({ago // 3600}h ago)"
            label = "用户" if role == "user" else "AI"
            context_lines.append(f"{label}{time_ago}: {content}")

        context_text = "\n".join(context_lines)
        now_str = datetime.now().strftime("%H:%M")

        system_prompt = (
            "你是一个友好的AI助手。根据最近的对话历史，决定是否应该主动给用户发一条消息。\n"
            "考虑因素：距离上次对话的时间、对话话题、用户可能的兴趣。\n"
            "如果决定说话，生成一条自然、简短（不超过50字）的消息。\n"
            "如果不应该说话（例如对话刚结束不久、没有合适的话题），"
            "请只回复两个大写字母: SILENT\n"
            "回复格式：\n"
            "- 说话: SPEAK: <你的消息内容>\n"
            "- 沉默: SILENT"
        )

        user_prompt = (
            f"当前时间: {now_str}\n"
            f"最近对话:\n{context_text}\n\n"
            f"请决定是否主动联系用户。"
        )

        model = self._model or "qwen2.5:3b"
        api_url = f"{self._ollama_base}/v1/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                resp = await client.post(api_url, json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.6,
                    "max_tokens": 100,
                    "stream": False,
                })
                resp.raise_for_status()
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

            if not content or content.upper().startswith("SILENT"):
                return {"should_speak": False, "content": "", "reason": "LLM chose SILENT"}

            # Parse "SPEAK: <message>" format
            if content.startswith("SPEAK:"):
                message_text = content[6:].strip()
            elif content.startswith("SPEAK："):
                message_text = content[6:].strip()
            else:
                message_text = content

            return {
                "should_speak": True,
                "content": message_text,
                "reason": f"scheduled_check_at_{now_str}",
            }

        except Exception as e:
            logger.error("Proactive LLM decide error conv=%s: %s", conv_id, e)
            return None


# ── Singleton ──────────────────────────────────────────────────────

_scheduler: ProactiveScheduler | None = None


def get_scheduler() -> ProactiveScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ProactiveScheduler()
    return _scheduler


def init_scheduler(config: dict) -> ProactiveScheduler:
    """Initialize the global scheduler with config. Call at startup."""
    global _scheduler
    _scheduler = ProactiveScheduler(config)
    return _scheduler
