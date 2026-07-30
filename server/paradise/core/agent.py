"""ParadiseAgent — main agent orchestrator.

Coordinates Transport, Tools, Memory, Emotion, Heartbeat, Reflection, and Plugins.
Uses ReAct two-step architecture: THINK (optional) → RESPOND (streaming).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncGenerator, Any

from paradise.config import ParadiseConfig
from paradise.core.channel import Channel
from paradise.core.context import LoopContext
from paradise.emotion.engine import EmotionEngine, EmotionState
from paradise.memory.workspace import Workspace
from paradise.memory.manager import MemoryManager
from paradise.heartbeat.manager import HeartbeatManager
from paradise.heartbeat.decision import HeartbeatDecision
from paradise.reflection.engine import ReflectionEngine
from paradise.reflection.storage import ReflectionEntry
from paradise.prompt.builder import prompt_builder
from paradise.prompt.templates import TOOL_SYSTEM_PROMPT, THINK_SYSTEM_PROMPT, THINK_PROMPT
from paradise.tools.builtin import get_builtin_tool_definitions, execute_tool
from paradise.transports import resolve_transport

logger = logging.getLogger(__name__)

# Intent keywords (fast local check)
_TOOL_KEYWORDS = {
    "文件", "目录", "数据", "读取", "搜索", "查找", "列出",
    "bash", "运行", "执行命令", "state.json", "config.json",
    "read_file", "search_files", "ls", "cat ", "find ", "grep",
    "帮我看看", "帮我查", "帮我找", "读一下", "搜一下",
    "有什么文件", "列出文件", "文件列表", "目录内容",
}

# Simple greeting/patterns that can skip the think phase
_SKIP_THINK_PATTERNS = {
    "hi", "hello", "你好", "嗨", "哈喽", "嘿", "在吗", "在不在",
    "早上好", "下午好", "晚上好", "早安", "晚安", " bye",
}


def _needs_tools(message: str) -> bool:
    """Fast local intent check."""
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in _TOOL_KEYWORDS)


def _should_think(message: str) -> bool:
    """Decide if think phase is needed. Skip for simple greetings."""
    msg_stripped = message.strip().lower()
    if len(msg_stripped) < 5:
        return False
    if msg_stripped in _SKIP_THINK_PATTERNS:
        return False
    # Check if it's a very simple greeting-only message
    for pattern in _SKIP_THINK_PATTERNS:
        if msg_stripped == pattern or msg_stripped == pattern + "～" or msg_stripped == pattern + "~":
            return False
    return True


class ParadiseAgent:
    """Paradise Agent — orchestrates all subsystems.

    Message flow (ReAct):
      INTENT → [TOOL] → [THINK] → RESPOND (streaming) → [REFLECT]

    Heartbeat: periodic GATHER → THINK → ACT
    Reflection: post-turn / idle / scheduled
    """

    def __init__(self, agent_id: str, config: ParadiseConfig):
        self.agent_id = agent_id
        self.config = config

        # Core subsystems
        self.workspace = Workspace(agent_id)
        self.emotion_engine = EmotionEngine()
        self.emotion_state = EmotionState()

        # Transport (main model)
        self.transport = resolve_transport(config.llm)
        # Transport for tool phase (lower temperature)
        self.tool_transport = resolve_transport(config.tool_llm)
        # Transport for reflection (small model)
        self.reflection_transport = resolve_transport(config.reflection.llm)

        # Memory
        self.memory = MemoryManager()

        # Heartbeat
        self.heartbeat = HeartbeatManager(self.transport, config.heartbeat)

        # Reflection
        self.reflection = ReflectionEngine(
            self.reflection_transport,
            config.reflection,
            self.workspace.root,
        )

        # State
        self._last_interaction_time = time.monotonic()
        self._initialized = False

    async def initialize(self, **kwargs) -> None:
        """Initialize the agent and all subsystems."""
        # Initialize workspace files
        soul_md = kwargs.get("soul_md", "")
        memory_md = kwargs.get("memory_md", "")
        heartbeat_md = kwargs.get("heartbeat_md", "")
        agents_md = kwargs.get("agents_md", "")
        self.workspace.initialize(soul_md, memory_md, heartbeat_md, agents_md)

        # Load emotion state
        state_dict = self.workspace.load_state()
        if state_dict:
            self.emotion_state = EmotionState.from_dict(state_dict)

        # Initialize memory
        self.memory.initialize(self.agent_id, self.workspace)

        # Register with heartbeat
        self.heartbeat.register(
            self.agent_id,
            self.workspace,
            self.emotion_engine,
            self.emotion_state,
            self._on_autonomous,
            {
                "api_url": self.config.llm.api_url,
                "api_key": self.config.llm.api_key,
                "model": self.config.llm.model,
            },
        )

        if self.config.heartbeat.enabled:
            self.heartbeat.start(self.agent_id)

        self._initialized = True
        logger.info("[Paradise] Agent %s initialized", self.agent_id)

    async def handle_message(self, ctx: LoopContext) -> AsyncGenerator[dict, None]:
        """Process a user message: INTENT → [TOOL] → [THINK] → RESPOND → [REFLECT]."""
        self._last_interaction_time = time.monotonic()

        # Update emotion on interaction
        self.emotion_engine.on_interaction(self.emotion_state, is_positive=True)
        self.workspace.save_state(self.emotion_state.to_dict())

        tool_results = ""

        # Phase 0: INTENT — check if tools needed
        if ctx.enable_tools and _needs_tools(ctx.user_message):
            # Phase 1: TOOL (silent)
            tool_results = await self._tool_phase(ctx)

        # Phase 2: THINK (optional, non-streaming)
        thinking_text = ""
        if _should_think(ctx.user_message):
            thinking_text = await self._think_phase(ctx, tool_results)
            if thinking_text:
                yield {"type": "thinking", "content": thinking_text}

        # Phase 3: RESPOND (streaming)
        async for event in self._respond_phase(ctx, tool_results, thinking_text):
            yield event

        # Phase 4: REFLECT (async, non-blocking)
        if self.config.reflection.enabled:
            asyncio.create_task(self._reflection_phase(ctx))

    # ── Phase 1: TOOL ────────────────────────────────────────────

    async def _tool_phase(self, ctx: LoopContext) -> str:
        """Silent tool execution phase."""
        messages = [{"role": "user", "content": ctx.user_message}]
        tools = get_builtin_tool_definitions()
        if not tools:
            return ""

        results = []
        for _ in range(self.config.max_tool_rounds):
            try:
                if hasattr(self.tool_transport, 'chat'):
                    tool_kwargs = {
                        "model": self.config.tool_llm.model or self.config.llm.model,
                        "system_prompt": TOOL_SYSTEM_PROMPT,
                        "messages": messages,
                        "temperature": self.config.tool_llm.temperature,
                        "tools": tools,
                    }
                    tool_mode = getattr(self.tool_transport, 'api_mode', '')
                    if tool_mode == "ollama_native":
                        tool_kwargs["api_url"] = self.config.tool_llm.api_url or self.config.llm.api_url
                    else:
                        tool_kwargs["api_url"] = self.config.tool_llm.api_url or self.config.llm.api_url
                        tool_kwargs["api_key"] = self.config.tool_llm.api_key or self.config.llm.api_key
                        tool_kwargs["max_tokens"] = self.config.tool_llm.max_tokens

                    resp = await self.tool_transport.chat(**tool_kwargs)

                    content = resp.content if hasattr(resp, 'content') else ""
                    tool_calls = resp.tool_calls if hasattr(resp, 'tool_calls') else None

                    if not tool_calls:
                        if content:
                            results.append(content)
                        break

                    # Execute tool calls
                    for tc in tool_calls:
                        import json
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                        tool_result = await execute_tool(tc.name, args)
                        results.append(f"[{tc.name}] {tool_result}")
                        messages.append({"role": "tool", "content": tool_result,
                                         "tool_call_id": tc.id or ""})
                else:
                    break
            except Exception as e:
                logger.error("[Paradise] Tool phase error: %s", e)
                break

        return "\n".join(results)

    # ── Phase 2: THINK (ReAct) ──────────────────────────────────

    async def _think_phase(self, ctx: LoopContext, tool_results: str) -> str:
        """ReAct Think step — non-streaming internal reasoning.

        Uses the same transport but a lightweight system prompt.
        Returns the thinking text, or empty string on failure.
        """
        soul_md = ctx.soul_md or self.workspace.read_md("soul")
        memory_md = ctx.memory_md or self.workspace.read_md("memory")

        # Build context for thinking
        context_parts = []
        if soul_md:
            context_parts.append(f"角色设定: {soul_md[:300]}")
        if memory_md:
            context_parts.append(f"长期记忆:\n{memory_md[:500]}")

        # Channel history summary
        if ctx.channel:
            history = ctx.channel.get_history_text(last_n=10)
            if history:
                context_parts.append(f"近期对话:\n{history[:800]}")

        context_text = "\n\n".join(context_parts)

        think_system = THINK_SYSTEM_PROMPT.format(agent_name=ctx.agent_name or self.agent_id)
        think_user = (
            f"{context_text}\n\n"
            f"最新消息: {ctx.user_message}\n\n"
            f"{THINK_PROMPT}"
        )

        messages = [{"role": "user", "content": think_user}]

        try:
            kwargs = {
                "model": self.config.llm.model,
                "system_prompt": think_system,
                "messages": messages,
                "temperature": 0.3,  # Lower temp for focused thinking
            }
            if hasattr(self.transport, 'api_mode'):
                mode = self.transport.api_mode
                if mode == "ollama_native":
                    kwargs["api_url"] = self.config.llm.api_url
                else:
                    kwargs["api_url"] = self.config.llm.api_url
                    kwargs["api_key"] = self.config.llm.api_key
                    kwargs["max_tokens"] = self.config.llm.max_tokens or 512

            if hasattr(self.transport, 'chat'):
                result = await self.transport.chat(**kwargs)
                thinking = result.content if hasattr(result, 'content') else str(result)
                # Strip any accidental tags from thinking output
                import re
                thinking = re.sub(r'</?(?:thinking|think|response|reasoning|thought)\b[^>]*>', '', thinking)
                thinking = re.sub(
                    r'^(?:thinking|think|response|reasoning|thought|assistant)\s*[:：]\s*',
                    '', thinking, flags=re.IGNORECASE,
                )
                return thinking.strip()
            return ""
        except Exception as e:
            logger.warning("[Paradise] Think phase error (skipping): %s", e)
            return ""

    # ── Phase 3: RESPOND (streaming) ────────────────────────────

    async def _respond_phase(
        self, ctx: LoopContext, tool_results: str, thinking_text: str
    ) -> AsyncGenerator[dict, None]:
        """ReAct Respond step — streaming, clean output (no tags to parse)."""
        # Build character prompt
        soul_md = ctx.soul_md or self.workspace.read_md("soul")
        memory_md = ctx.memory_md or self.workspace.read_md("memory")
        mood_modifier = ctx.mood_modifier or self.emotion_engine.get_mood_modifier(self.emotion_state)

        # Inject FrozenMemory facts
        longterm_summary = ""
        if self.memory.builtin:
            longterm_summary = self.memory.builtin.to_prompt_text()

        system_prompt = prompt_builder.build(
            agent_name=ctx.agent_name,
            soul_md=soul_md,
            user_name=ctx.user_name,
            user_personality=ctx.user_personality,
            user_description=ctx.user_description,
            channel=ctx.channel,
            memory_md=memory_md,
            longterm_summary=longterm_summary,
            mood_modifier=mood_modifier,
            enable_tools=bool(tool_results),
        )

        # Build messages
        messages = self._build_messages(ctx, tool_results)

        # Inject thinking as context if available
        if thinking_text:
            messages.insert(0, {
                "role": "system",
                "content": f"[你的内心思考]\n{thinking_text}\n[请基于以上思考进行回复]",
            })

        try:
            if hasattr(self.transport, 'stream_chat'):
                stream_kwargs = {
                    "model": self.config.llm.model,
                    "system_prompt": system_prompt,
                    "messages": messages,
                    "temperature": self.config.llm.temperature,
                }
                # Add provider-specific args
                if hasattr(self.transport, 'api_mode'):
                    mode = self.transport.api_mode
                    if mode == "openai_compat":
                        stream_kwargs["api_url"] = self.config.llm.api_url
                        stream_kwargs["api_key"] = self.config.llm.api_key
                        stream_kwargs["max_tokens"] = self.config.llm.max_tokens
                    elif mode == "anthropic_messages":
                        stream_kwargs["api_key"] = self.config.llm.api_key
                        stream_kwargs["max_tokens"] = self.config.llm.max_tokens
                    elif mode == "ollama_native":
                        stream_kwargs["api_url"] = self.config.llm.api_url

                full_content = []
                async for chunk in self.transport.stream_chat(**stream_kwargs):
                    if isinstance(chunk, str):
                        # Direct content streaming — no Thinker parser needed
                        full_content.append(chunk)
                        yield {"type": "content", "content": chunk}

                # Done event with full assembled content
                yield {
                    "type": "done",
                    "content": "".join(full_content),
                    "thinking": thinking_text,
                    "done": True,
                }
            else:
                yield {"type": "done"}

        except Exception as e:
            logger.error("[Paradise] Respond phase error: %s", e)
            yield {"type": "error", "content": str(e)}

    # ── Phase 4: REFLECT ────────────────────────────────────────

    async def _reflection_phase(self, ctx: LoopContext) -> None:
        """Post-turn reflection (async, non-blocking)."""
        try:
            agent_response = self._get_last_response()
            logger.debug("[Paradise] Reflection: user=%r response=%r",
                        ctx.user_message[:50], agent_response[:50] if agent_response else "(empty)")
            await self.reflection.on_turn_complete(
                agent_name=ctx.agent_name,
                user_message=ctx.user_message,
                agent_response=agent_response,
                emotion_state=self.emotion_state,
            )
        except Exception as e:
            logger.warning("[Paradise] Reflection phase error: %s", e)

    # ── Callbacks ────────────────────────────────────────────────

    async def _on_autonomous(self, agent_id: str, decision: HeartbeatDecision) -> None:
        """Callback from heartbeat when agent decides to speak autonomously."""
        logger.info("[Paradise] Autonomous: %s -> %s: %s",
                    agent_id, decision.action.value, decision.message[:50])

    # ── Helpers ──────────────────────────────────────────────────

    def _get_last_response(self) -> str:
        """Read the last assistant message from session files."""
        import json as _json
        sessions_dir = self.workspace.root / "sessions"
        if not sessions_dir.exists():
            return ""
        try:
            session_dirs = sorted(sessions_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            for session_dir in session_dirs[:3]:
                for jsonl_file in sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
                    lines = jsonl_file.read_text(encoding="utf-8").strip().splitlines()
                    for line in reversed(lines):
                        entry = _json.loads(line)
                        if entry.get("role") == "assistant":
                            return entry.get("content", "")[:500]
        except Exception:
            pass
        return ""

    def _build_messages(self, ctx: LoopContext, tool_results: str) -> list[dict]:
        """Build message array for LLM."""
        messages = []

        # Channel history
        if ctx.channel:
            for msg in ctx.channel.get_history(last_n=20):
                role = "assistant" if msg.get("sender_id") == ctx.agent_id else "user"
                name = msg.get("sender_name", msg.get("role", ""))
                content = msg.get("content", "")
                if name and role == "user":
                    content = f"{name}: {content}"
                messages.append({"role": role, "content": content})
        else:
            messages.append({"role": "user", "content": ctx.user_message})

        # Inject tool results as system context
        if tool_results:
            messages.append({
                "role": "system",
                "content": f"[工具执行结果]\n{tool_results}",
            })

        return messages

    async def check_idle_reflection(self) -> None:
        """Check if idle reflection should trigger."""
        idle_seconds = time.monotonic() - self._last_interaction_time
        idle_minutes = idle_seconds / 60

        if self.config.reflection.enabled:
            recent = self._gather_recent_for_reflection()
            memory_md = self.workspace.read_md("memory")
            await self.reflection.on_idle(
                agent_name=self.agent_id,
                idle_minutes=idle_minutes,
                recent_conversations=recent,
                current_memory=memory_md,
                emotion_state=self.emotion_state,
            )
            self.workspace.save_state(self.emotion_state.to_dict())

    def _gather_recent_for_reflection(self) -> str:
        """Gather recent conversations for reflection."""
        parts = []
        sessions_dir = self.workspace.root / "sessions"
        if not sessions_dir.exists():
            return ""
        import json
        for session_dir in sorted(sessions_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            for jsonl_file in sorted(session_dir.glob("*.jsonl")):
                try:
                    lines = jsonl_file.read_text("utf-8").strip().splitlines()
                    for line in lines[-10:]:
                        entry = json.loads(line)
                        role = entry.get("role", "?")
                        content = entry.get("content", "")[:150]
                        parts.append(f"[{session_dir.name}] {role}: {content}")
                except Exception:
                    continue
        return "\n".join(parts[-50:])

    def get_state(self) -> dict:
        """Get current agent state."""
        return self.emotion_state.to_dict()

    async def feed(self, amount: int = 1) -> dict:
        """Feed the agent."""
        self.emotion_engine.on_feed(self.emotion_state, amount)
        self.workspace.save_state(self.emotion_state.to_dict())
        return self.emotion_state.to_dict()

    async def destroy(self) -> None:
        """Clean shutdown."""
        self.heartbeat.unregister(self.agent_id)
        self.memory.shutdown()
        logger.info("[Paradise] Agent %s destroyed", self.agent_id)

    @property
    def has_api_config(self) -> bool:
        return bool(self.config.llm.api_url)


class ParadiseRuntimeManager:
    """Global manager for ParadiseAgent instances."""

    def __init__(self):
        self._agents: dict[str, ParadiseAgent] = {}

    def get_or_create(self, agent_id: str, config: ParadiseConfig) -> ParadiseAgent:
        if agent_id in self._agents:
            return self._agents[agent_id]
        agent = ParadiseAgent(agent_id, config)
        self._agents[agent_id] = agent
        return agent

    def remove(self, agent_id: str) -> None:
        agent = self._agents.pop(agent_id, None)
        if agent:
            asyncio.create_task(agent.destroy())

    def get(self, agent_id: str) -> ParadiseAgent | None:
        return self._agents.get(agent_id)


# Global singleton
runtime_manager = ParadiseRuntimeManager()
