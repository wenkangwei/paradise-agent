"""Agent Handler — wraps ParadiseAgent for the AiChat Android server.

Responsibilities:
  1. AgentSessionManager: manage ParadiseAgent instances per conversationId
  2. process_message_stream(): receive OpenAI-format messages, run agent loop, yield SSE
  3. Integration with TurnLogger for conversation logging + training data export
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

from paradise.config import ParadiseConfig, LLMConfig, HeartbeatConfig, ReflectionConfig
from paradise.core.agent import ParadiseAgent
from paradise.core.channel import Channel
from paradise.core.context import LoopContext

from turn_logger import TurnRecorder, TrainingExporter

logger = logging.getLogger("agent_handler")

# ── Config ────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg_path = Path(__file__).parent / "agent_config.json"
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


_CFG = _load_config()

AGENT_ENABLED = os.getenv("AGENT_ENABLED", str(_CFG.get("agent", {}).get("enabled", True))).lower() in ("1", "true", "yes")
THINK_PHASE = os.getenv("AGENT_THINK", str(_CFG.get("agent", {}).get("think_phase", True))).lower() in ("1", "true", "yes")
ENABLE_TOOLS = os.getenv("AGENT_TOOLS", str(_CFG.get("agent", {}).get("enable_tools", True))).lower() in ("1", "true", "yes")
MAX_TOOL_ROUNDS = int(os.getenv("AGENT_MAX_TOOL_ROUNDS", str(_CFG.get("agent", {}).get("max_tool_rounds", 3))))

OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", _CFG.get("ollama", {}).get("base_url", "http://localhost:11434"))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", _CFG.get("ollama", {}).get("default_model", "qwen2.5:7b"))
VISION_MODEL = os.getenv("VISION_MODEL", _CFG.get("ollama", {}).get("vision_model", "llava:7b"))

SAVE_TURNS = os.getenv("LOG_SAVE_TURNS", str(_CFG.get("logging", {}).get("save_turns", True))).lower() in ("1", "true", "yes")
SAVE_TRAINING = os.getenv("LOG_SAVE_TRAINING", str(_CFG.get("logging", {}).get("save_training_data", True))).lower() in ("1", "true", "yes")
LOG_DIR = Path(os.getenv("LOG_DIR", _CFG.get("logging", {}).get("log_dir", "logs")))

ATTACHMENTS_ENABLED = os.getenv("ATTACHMENTS_ENABLED", str(_CFG.get("attachments", {}).get("enabled", True))).lower() in ("1", "true", "yes")
ATTACH_IMAGE_MODE = os.getenv("ATTACH_IMAGE_MODE", _CFG.get("attachments", {}).get("image_mode", "base64_passthrough"))
ATTACH_FILE_MODE = os.getenv("ATTACH_FILE_MODE", _CFG.get("attachments", {}).get("file_mode", "read_tool"))

# ── Session Manager ───────────────────────────────────────────────

class AgentSessionManager:
    """Manages ParadiseAgent instances keyed by conversation ID.

    Mirrors the Android side's Map<convId, Job> pattern.
    """

    def __init__(self):
        self._agents: dict[str, ParadiseAgent] = {}
        self._turn_counters: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, conv_id: str, model: str = "") -> ParadiseAgent:
        async with self._lock:
            if conv_id in self._agents:
                return self._agents[conv_id]

            model_name = model or DEFAULT_MODEL
            config = ParadiseConfig(
                agent_id=conv_id,
                llm=LLMConfig(
                    api_url=f"{OLLAMA_BASE}/v1",
                    api_key="",
                    model=model_name,
                    provider="ollama",
                    temperature=0.7,
                    max_tokens=2048,
                ),
                enable_tools=ENABLE_TOOLS,
                max_tool_rounds=MAX_TOOL_ROUNDS,
                heartbeat=HeartbeatConfig(enabled=False),
                reflection=ReflectionConfig(enabled=False),
            )

            agent = ParadiseAgent(conv_id, config)
            # Skip full initialize() — no workspace files needed for android use case.
            # Just create a minimal channel for the session.
            self._agents[conv_id] = agent
            self._turn_counters[conv_id] = 0
            logger.info("Agent created: conv=%s model=%s tools=%s", conv_id, model_name, ENABLE_TOOLS)
            return agent

    def get_channel(self, conv_id: str) -> Channel:
        """Get or create a Channel for the conversation."""
        if not hasattr(self, '_channels'):
            self._channels: dict[str, Channel] = {}
        if conv_id not in self._channels:
            self._channels[conv_id] = Channel(channel_type="private")
        return self._channels[conv_id]

    def next_turn(self, conv_id: str) -> int:
        self._turn_counters[conv_id] = self._turn_counters.get(conv_id, 0) + 1
        return self._turn_counters[conv_id]

    async def remove(self, conv_id: str) -> None:
        async with self._lock:
            agent = self._agents.pop(conv_id, None)
            if agent:
                try:
                    await agent.destroy()
                except Exception:
                    pass
            self._turn_counters.pop(conv_id, None)
            if hasattr(self, '_channels'):
                self._channels.pop(conv_id, None)

    def get_turn_count(self, conv_id: str) -> int:
        return self._turn_counters.get(conv_id, 0)


# Global singleton
session_manager = AgentSessionManager()
training_exporter = TrainingExporter(LOG_DIR.resolve())


# ── Message Processing ────────────────────────────────────────────

def _extract_user_message(messages: list[dict]) -> tuple[str, str | list, list[dict]]:
    """Extract the last user message from OpenAI-format messages.

    Returns:
        (text_only, full_content, attachments_meta)
        - text_only: plain text extracted from all text parts
        - full_content: original content (str or list) for multimodal passthrough
        - attachments_meta: list of attachment metadata dicts
    """
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return "", "", []

    last = user_msgs[-1]
    content = last.get("content", "")

    text_parts = []
    attachments_meta = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    img = part.get("image_url", {})
                    url = img.get("url", "") if isinstance(img, dict) else str(img)
                    mime = _guess_mime_from_data_url(url)
                    attachments_meta.append({"mime": mime, "type": "image", "size_bytes": len(url)})
                else:
                    attachments_meta.append({"type": part.get("type", "unknown"), "raw": str(part)[:200]})
            elif isinstance(part, str):
                text_parts.append(part)

    text_only = "\n".join(text_parts) if text_parts else ""
    return text_only, content, attachments_meta


def _guess_mime_from_data_url(url: str) -> str:
    """Extract MIME type from data: URL."""
    if url.startswith("data:"):
        end = url.find(";")
        if end > 0:
            return url[5:end]
    return "unknown"


def _build_channel_history(messages: list[dict], conv_id: str) -> Channel:
    """Build a Channel with message history from OpenAI-format messages.

    Skips the last user message (it becomes the trigger).
    """
    channel = Channel(channel_type="private")
    # Skip last message (it's the current trigger), add the rest as history
    history_msgs = messages[:-1] if len(messages) > 1 else []

    for msg in history_msgs:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        # Extract text from multimodal content
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
            content = "\n".join(text_parts) if text_parts else ""
        if not isinstance(content, str):
            content = str(content)

        sender_id = "assistant" if role == "assistant" else "user"
        sender_name = "AI" if role == "assistant" else "用户"
        channel.add_message(sender_id=sender_id, content=content, sender_name=sender_name)

    return channel


async def _model_supports_vision(model: str) -> bool:
    """Check if the model supports vision (multimodal).

    Currently checks by model name pattern. Future: query Ollama /api/show.
    """
    vision_patterns = ["llava", "bakllava", "qwen2.5-vl", "qwen-vl", "llama3.2-vision",
                       "minicpm-v", "cogvlm", "fuyu", "gemini", "gpt-4o", "vision"]
    model_lower = model.lower()
    return any(p in model_lower for p in vision_patterns)


async def process_message_stream(
    messages: list[dict],
    model: str,
    conv_id: str = ""
) -> AsyncGenerator[str, None]:
    """Process chat messages through the Paradise agent, yielding SSE chunks.

    Args:
        messages: OpenAI-format message list
        model: model name (from request)
        conv_id: conversation ID for session management

    Yields:
        SSE-formatted bytes chunks: b'data: {...}\\n\\n'
    """
    conv_id = conv_id or str(uuid.uuid4())
    t0 = time.time()
    model_name = model or DEFAULT_MODEL

    # Extract user message and attachments
    text_only, full_content, attachments_meta = _extract_user_message(messages)

    # Quick check: multimodal content but no vision model
    has_images = any(a["type"] == "image" for a in attachments_meta)
    if has_images and not await _model_supports_vision(model_name):
        logger.warning(
            "Multimodal request but model '%s' does not support vision, "
            "dropping image parts. Consider using '%s' for vision tasks.",
            model_name, VISION_MODEL,
        )
        # Strip images, keep text only (agent will handle text normally)

    # ── Turn logger ───────────────────────────────────────────────
    turn = session_manager.next_turn(conv_id)
    recorder = TurnRecorder(LOG_DIR.resolve(), conv_id, turn) if SAVE_TURNS else None
    if recorder:
        recorder.turn_start(model_name)
        recorder.user_message(full_content, attachments_meta)

    # ── Agent processing ──────────────────────────────────────────
    if not AGENT_ENABLED:
        # Passthrough: agent disabled, caller should use thin proxy
        raise RuntimeError("Agent is disabled. Use /v1/chat/completions for thin proxy.")

    agent = await session_manager.get_or_create(conv_id, model_name)
    channel = session_manager.get_channel(conv_id)
    _build_channel_history(messages, conv_id)  # populate channel with history

    ctx = LoopContext(
        agent_id=conv_id,
        agent_name="AI",
        session_id=conv_id,
        user_message=text_only if isinstance(full_content, list) else full_content,
        channel=channel,
        enable_tools=ENABLE_TOOLS,
    )

    # Store multimodal content on ctx for _build_messages to use
    if isinstance(full_content, list):
        ctx._multimodal_content = full_content

    try:
        # Phase 0: Intent check (needs_tools)
        from paradise.core.agent import _needs_tools, _should_think
        needs_tools = ENABLE_TOOLS and _needs_tools(text_only)
        should_think = THINK_PHASE and _should_think(text_only)

        if recorder:
            recorder.intent(needs_tools, should_think)

        # Run agent loop
        content_full = []
        async for event in agent.handle_message(ctx):
            evt_type = event.get("type", "")

            if evt_type == "thinking":
                if recorder:
                    recorder.thinking(event.get("content", ""))
                # Yield thinking as reasoning_content (for Android thinking display)
                yield _sse_chunk({
                    "choices": [{
                        "index": 0,
                        "delta": {"reasoning_content": event.get("content", "")},
                        "finish_reason": None,
                    }],
                })

            elif evt_type == "content":
                text = event.get("content", "")
                content_full.append(text)
                if recorder:
                    recorder.content_delta(text)
                yield _sse_chunk({
                    "choices": [{
                        "index": 0,
                        "delta": {"content": text},
                        "finish_reason": None,
                    }],
                })

            elif evt_type == "tool_call":
                if recorder:
                    recorder.tool_call(
                        name=event.get("name", ""),
                        arguments=event.get("arguments", {}),
                        result=event.get("result", ""),
                        duration_ms=event.get("duration_ms", 0),
                    )
                # Emit as system content delta (Android shows in tool card)
                tool_info = f"\n🔧 {event.get('name', 'tool')}: {event.get('result', '')[:200]}\n"
                content_full.append(tool_info)
                yield _sse_chunk({
                    "choices": [{
                        "index": 0,
                        "delta": {"content": tool_info},
                        "finish_reason": None,
                    }],
                })

            elif evt_type == "error":
                if recorder:
                    recorder.error(event.get("content", ""))
                yield _sse_chunk({
                    "choices": [{
                        "index": 0,
                        "delta": {"content": f"\n⚠️ {event.get('content', 'Error')}\n"},
                        "finish_reason": None,
                    }],
                })

            elif evt_type == "done":
                # Final chunk with finish_reason
                final_content = event.get("content", "".join(content_full))
                if recorder:
                    recorder.turn_end("stop")
                    recorder.flush()

                    # Export training data
                    if SAVE_TRAINING:
                        entry = recorder.to_training_entry()
                        if entry:
                            training_exporter.append(entry)

                finish_reason = "stop"
                if event.get("done"):
                    finish_reason = "stop"

                yield _sse_chunk({
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": finish_reason,
                    }],
                })
                yield b"data: [DONE]\n\n"
                break

        # Update channel with assistant response
        if content_full:
            channel.add_message(
                sender_id="assistant",
                content="".join(content_full),
                sender_name="AI",
            )

    except Exception as e:
        logger.error("Agent processing error: %s", e, exc_info=True)
        if recorder:
            recorder.error(str(e))
            recorder.turn_end("error")
            recorder.flush()

        yield _sse_chunk({
            "error": {"message": f"Agent error: {e}", "type": "agent_error"},
        })
        yield b"data: [DONE]\n\n"

    elapsed = time.time() - t0
    logger.info(
        "Turn %d complete: conv=%s model=%s elapsed=%.2fs content_len=%d",
        turn, conv_id, model_name, elapsed,
        sum(len(c) for c in content_full),
    )


def _sse_chunk(data: dict) -> bytes:
    """Format a dict as an SSE 'data:' chunk."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


# ── Warmup ─────────────────────────────────────────────────────────

async def warmup_agent() -> None:
    """Pre-create a default agent on startup so first request is fast."""
    if not AGENT_ENABLED:
        logger.info("Agent disabled, skipping warmup")
        return
    try:
        await session_manager.get_or_create("__warmup__", DEFAULT_MODEL)
        logger.info("Agent warmup complete: model=%s tools=%s", DEFAULT_MODEL, ENABLE_TOOLS)
    except Exception as e:
        logger.warning("Agent warmup failed (non-fatal): %s", e)
