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
from paradise.prompt.templates import TOOL_SYSTEM_PROMPT, THINK_SYSTEM_PROMPT, THINK_PROMPT, FORMAT_SUFFIX
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
        """Process a user message: INTENT → [TOOL] → THINK → RESPOND → [REFLECT]."""
        self._last_interaction_time = time.monotonic()

        # Update emotion on interaction
        self.emotion_engine.on_interaction(self.emotion_state, is_positive=True)
        self.workspace.save_state(self.emotion_state.to_dict())

        tool_results = ""
        tool_events = []

        # Phase 1: TOOL — LLM decides autonomously whether to call tools.
        # Tools are always passed; the model may return tool_calls or plain text.
        if ctx.enable_tools:
            async for event in self._tool_phase(ctx):
                if event.get("type") == "tool_call":
                    tool_events.append(event)
                    yield event
                elif event.get("type") == "tool_results":
                    tool_results = event.get("content", "")

        # Phase 2: THINK (skip if ctx._skip_think flag set)
        thinking_text = ""
        if _should_think(ctx.user_message) and not getattr(ctx, '_skip_think', False):
            thinking_text = await self._think_phase(ctx, tool_results)
            if thinking_text:
                # Include tool summary in thinking if tools were used
                if tool_events:
                    tool_summary = "\n".join(
                        f"工具 {e.get('name', '?')}: {e.get('result', '')[:100]}"
                        for e in tool_events
                    )
                    thinking_text = f"{thinking_text}\n\n[工具执行记录]\n{tool_summary}"
                yield {"type": "thinking", "content": thinking_text}

        # Phase 3: RESPOND (streaming)
        async for event in self._respond_phase(ctx, tool_results, thinking_text):
            yield event

        # Phase 4: REFLECT (async, non-blocking)
        if self.config.reflection.enabled:
            asyncio.create_task(self._reflection_phase(ctx))

    # ── Phase 1: TOOL ────────────────────────────────────────────

    async def _tool_phase(self, ctx: LoopContext):
        """Tool execution phase — yields tool_call events for client visibility.

        Supports two modes based on model capabilities:
        - tools_prompt: system-prompt ReAct for non-instruct models
        - tools_native: OpenAI function calling for instruct models (default)
        """
        messages = [{"role": "user", "content": ctx.user_message}]

        # Filter out vision_analyze if VL model handles images directly
        tools = get_builtin_tool_definitions()
        skip_vision = getattr(ctx, '_skip_vision_tool', False)
        if skip_vision and tools:
            tools = [t for t in tools if t.get("function", {}).get("name") != "vision_analyze"]
        if not tools:
            yield {"type": "tool_results", "content": ""}
            return

        # Check routing mode
        use_react = getattr(ctx, '_tools_prompt', False)

        results = []
        for _ in range(self.config.max_tool_rounds):
            try:
                if use_react:
                    # ── System-prompt ReAct mode ──────────────────
                    tool_calls = await self._react_tool_phase(messages, tools, ctx)
                elif hasattr(self.tool_transport, 'chat'):
                    # ── Native OpenAI function calling ─────────────
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
                else:
                    break

                if not tool_calls:
                    if 'content' in dir() and content:
                        results.append(content)
                    break

                # Execute tool calls — yield each as an event
                for tc in tool_calls:
                    import json
                    import time as _time
                    t0 = _time.time()
                    if isinstance(tc, dict):
                        name = tc.get("name", "")
                        args = tc.get("arguments", {})
                    else:
                        name = tc.name
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                    tool_result = await execute_tool(name, args)
                    elapsed = (_time.time() - t0) * 1000

                    results.append(f"[{name}] {tool_result}")
                    messages.append({"role": "tool", "content": tool_result,
                                     "tool_call_id": tc.id if hasattr(tc, 'id') and tc.id else ""})

                    # Yield tool_call event for client visibility
                    yield {
                        "type": "tool_call",
                        "name": name,
                        "arguments": args,
                        "result": tool_result[:500],
                        "duration_ms": elapsed,
                    }
            except Exception as e:
                logger.error("[Paradise] Tool phase error: %s", e)
                break

        yield {"type": "tool_results", "content": "\n".join(results)}

    async def _react_tool_phase(self, messages: list, tools: list, ctx: LoopContext) -> list[dict] | None:
        """System-prompt ReAct: describe tools in prompt, parse text output.

        For models that don't support native OpenAI function calling.
        The LLM outputs [TOOL:name]...[/TOOL] blocks which we parse.
        """
        import re as _re

        # Build prompt with tool descriptions
        tool_lines = ["Available tools:"]
        for t in tools:
            fn = t.get("function", {})
            name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters", {}).get("properties", {})
            param_str = ", ".join(
                f"{k}=<{v.get('type','str')}>" for k, v in params.items()
            )
            tool_lines.append(f"- {name}({param_str}): {desc}")

        react_prompt = (
            "You have access to tools. To use a tool, output:\n"
            "[TOOL: name]\nparam1=value1\n[/TOOL]\n\n"
            "Only use tools when necessary. If you don't need tools, just respond directly.\n\n"
            + "\n".join(tool_lines)
        )

        tool_kwargs = {
            "model": self.config.tool_llm.model or self.config.llm.model,
            "system_prompt": react_prompt,
            "messages": messages,
            "temperature": 0.3,
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

        if not content:
            return None

        # Parse [TOOL:name]...[/TOOL] blocks
        tool_blocks = _re.findall(
            r'\[TOOL:\s*(\w+)\]\s*(.*?)\s*\[/TOOL\]', content, _re.DOTALL
        )

        if not tool_blocks:
            return None  # Model chose not to use tools

        parsed = []
        for name, args_text in tool_blocks:
            args = {}
            for line in args_text.strip().split("\n"):
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    args[k.strip()] = v.strip()

            parsed.append({
                "name": name,
                "arguments": args,
                "id": "",
            })

        return parsed if parsed else None

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
        """ReAct Respond step — streaming with minimal prompt.

        Uses a simplified system prompt (persona + output rules only)
        instead of the 10-layer aipet prompt. Raw messages from Android
        are passed through directly for conversation continuity.
        """
        # Minimal system prompt for AiChat Android use case
        agent_name = ctx.agent_name or "AI助手"
        system_prompt = (
            f"你是{agent_name}，一个有用的AI助手。\n"
            f"{FORMAT_SUFFIX}"
        )

        # Add compacted context summaries if available
        compact_context = getattr(ctx, '_compact_context', '') or ''
        if compact_context:
            system_prompt = compact_context + "\n\n" + system_prompt

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
            logger.error("[Paradise] Respond phase error: %s", e, exc_info=True)
            yield {"type": "error", "content": f"Respond error: {e}"}

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
        """Build message array for LLM.

        Passes through raw messages from context (set by agent_handler)
        without reformatting. Supports multimodal content.
        """
        messages = []

        # Use raw messages if available (set by agent_handler from Android request)
        raw_messages = getattr(ctx, '_raw_messages', None)
        if raw_messages and isinstance(raw_messages, list) and len(raw_messages) > 0:
            # Pass through all messages as-is (Android already sends full history)
            for msg in raw_messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                messages.append({"role": role, "content": content})
        else:
            # Fallback: channel-based message building
            multimodal_content = getattr(ctx, '_multimodal_content', None)
            if ctx.channel:
                for msg in ctx.channel.get_history(last_n=20):
                    role = "assistant" if msg.get("sender_id") == ctx.agent_id else "user"
                    content = msg.get("content", "")
                    messages.append({"role": role, "content": content})
                if multimodal_content:
                    messages.append({"role": "user", "content": multimodal_content})
                else:
                    messages.append({"role": "user", "content": ctx.user_message})
            else:
                if multimodal_content:
                    messages.append({"role": "user", "content": multimodal_content})
                else:
                    messages.append({"role": "user", "content": ctx.user_message})

        # Inject tool results as system context (with collapse hint)
        if tool_results:
            messages.append({
                "role": "system",
                "content": f"[工具执行结果]\n{tool_results}\n[基于以上结果回复用户]",
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
