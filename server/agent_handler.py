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
from context_compactor import get_compactor, estimate_messages_tokens, summary_index
from model_router import detect_model, ToolRouter, ModelCap

logger = logging.getLogger("agent_handler")

# ── Auto-discover tools ──────────────────────────────────────────────
# Import all tool modules in paradise/tools/ that have a
# top-level `registry.register(...)` call. After this, every tool is
# registered and discoverable via registry.get_all_tool_names().
#
# To add a new tool, just drop a .py file in paradise/tools/ with a
# `registry.register(...)` at module level — no other file changes needed.

from paradise.tools.registry import discover_builtin_tools
_TOOL_MODULES = discover_builtin_tools()
logger.info("Auto-discovered %d tool modules: %s", len(_TOOL_MODULES), _TOOL_MODULES)

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
VISION_MODEL = os.getenv("VISION_MODEL", _CFG.get("ollama", {}).get("vision_model", "qwen2.5vl:7b"))
SKIP_THINK = os.getenv("SKIP_THINK", "").lower() in ("1", "true", "yes")
SKIP_TOOLS = os.getenv("SKIP_TOOLS", "").lower() in ("1", "true", "yes")

SAVE_TURNS = os.getenv("LOG_SAVE_TURNS", str(_CFG.get("logging", {}).get("save_turns", True))).lower() in ("1", "true", "yes")
SAVE_TRAINING = os.getenv("LOG_SAVE_TRAINING", str(_CFG.get("logging", {}).get("save_training_data", True))).lower() in ("1", "true", "yes")
LOG_DIR = Path(os.getenv("LOG_DIR", _CFG.get("logging", {}).get("log_dir", "logs")))

ATTACHMENTS_ENABLED = os.getenv("ATTACHMENTS_ENABLED", str(_CFG.get("attachments", {}).get("enabled", True))).lower() in ("1", "true", "yes")
ATTACH_IMAGE_MODE = os.getenv("ATTACH_IMAGE_MODE", _CFG.get("attachments", {}).get("image_mode", "base64_passthrough"))
ATTACH_FILE_MODE = os.getenv("ATTACH_FILE_MODE", _CFG.get("attachments", {}).get("file_mode", "read_tool"))

# ── Phase 2 / 2.5: LangGraph + Intent routing flag ─────────────────
# In prod mode, inherit langgraph_enabled from config.prod.yaml so the
# graph (and its INTENT node) actually lights up. In dev/main mode this
# stays False → original 4-phase handle_message() path is untouched.
def _resolve_langgraph_enabled() -> bool:
    if os.getenv("PARADISE_MODE", "dev") != "prod":
        return False
    # Env override wins (lets operators toggle without editing yaml).
    env_val = os.getenv("PARADISE_LANGGRAPH_ENABLED", "")
    if env_val:
        return env_val.lower() in ("1", "true", "yes")
    try:
        from paradise.factory import load_prod_config
        return load_prod_config().langgraph_enabled
    except Exception:
        return False

LANGGRAPH_ENABLED = _resolve_langgraph_enabled()
logger.info("LangGraph+intent routing: enabled=%s (mode=%s)", LANGGRAPH_ENABLED, os.getenv("PARADISE_MODE", "dev"))

# ── Phase 2.9: Supervisor routing flag ──────────────────────────────
# When True, agent_handler routes through supervisor graph (intent →
# mode-select → subgraph → reflect → cleanup) instead of run_via_graph
# or handle_message. Takes precedence over langgraph_enabled.
def _resolve_supervisor_enabled() -> bool:
    if os.getenv("PARADISE_MODE", "dev") != "prod":
        return False
    env_val = os.getenv("PARADISE_SUPERVISOR_ENABLED", "")
    if env_val:
        return env_val.lower() in ("1", "true", "yes")
    try:
        from paradise.factory import load_prod_config
        return load_prod_config().supervisor_enabled
    except Exception:
        return False

SUPERVISOR_ENABLED = _resolve_supervisor_enabled()
logger.info("Supervisor routing: enabled=%s (mode=%s)", SUPERVISOR_ENABLED, os.getenv("PARADISE_MODE", "dev"))

# ── Phase 6: query preprocess / complexity / agent_team wiring ─────
# agent_handler constructs ParadiseConfig by hand (see get_or_create) and
# does NOT read config.prod.yaml for the Phase 6 knobs. In prod mode, pull
# them once here so factory.build_supervisor actually wires the
# preprocessor, complexity assessor, and (when enabled) AgentTeamPattern.
_PHASE6_CFG = None
if os.getenv("PARADISE_MODE", "dev") == "prod":
    try:
        from paradise.factory import load_prod_config
        _pc = load_prod_config()
        _PHASE6_CFG = {
            "query_preprocess": _pc.query_preprocess,
            "complexity": _pc.complexity,
            "agent_team": _pc.agent_team,
        }
        logger.info(
            "Phase 6 wiring: preprocess=%s complexity=%s agent_team=%s",
            _PHASE6_CFG["query_preprocess"].enabled,
            _PHASE6_CFG["complexity"].enabled,
            _PHASE6_CFG["agent_team"].enabled,
        )
    except Exception as exc:
        logger.warning("Phase 6 config load failed (%s) — using defaults", exc)
        _PHASE6_CFG = None

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
                # Phase 2.5: when True, run_via_graph() is used → INTENT node
                # classifies the message and routes chitchat/unsafe to the
                # RESPOND fast path. Other intents run the full 4-phase pipeline.
                langgraph_enabled=LANGGRAPH_ENABLED,
                # Phase 2.9: when True, run_via_supervisor() is used →
                # intent + mode-select + Handoff Protocol + reflect.
                # Takes precedence over langgraph_enabled.
                supervisor_enabled=SUPERVISOR_ENABLED,
            )

            # Phase 6: inherit query_preprocess/complexity/agent_team from
            # config.prod.yaml (prod mode only). get_or_create builds
            # ParadiseConfig manually and would otherwise leave these at
            # their defaults — agent_team.enabled=False would never route
            # complex turns to the multi-agent orchestration.
            if _PHASE6_CFG is not None:
                config.query_preprocess = _PHASE6_CFG["query_preprocess"]
                config.complexity = _PHASE6_CFG["complexity"]
                config.agent_team = _PHASE6_CFG["agent_team"]

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

def _extract_user_message(messages: list[dict]) -> tuple[str, str | list, list[dict], list[dict]]:
    """Extract the last user message from OpenAI-format messages.

    If multimodal content (image_url parts) is detected, decode base64
    attachments to temp files and inject file paths into the text.

    Returns:
        (text_only, full_content, attachments_meta, decoded_attachments)
        - text_only: plain text with injected attachment file paths
        - full_content: original content (str or list) for multimodal passthrough
        - attachments_meta: list of attachment metadata dicts (mime, type, size)
        - decoded_attachments: list from attachment_utils.decode_and_save()
    """
    from attachment_utils import decode_and_save, inject_context

    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return "", "", [], []

    last = user_msgs[-1]
    content = last.get("content", "")

    text_parts = []
    attachments_meta = []
    decoded_attachments: list[dict] = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        # Decode and save base64 image/file attachments
        decoded_attachments = decode_and_save(content)

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
    # Inject decoded attachment file paths so agent's TOOL phase can find them
    if decoded_attachments:
        text_only = inject_context(text_only, decoded_attachments)

    return text_only, content, attachments_meta, decoded_attachments


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




async def process_message_stream(
    messages: list[dict],
    model: str,
    conv_id: str = "",
    user_profile: str = ""
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

    def _tick(label: str) -> None:
        """Log timing since request start."""
        elapsed = (time.time() - t0) * 1000
        logger.warning("[⏱ %6.0fms] %s", elapsed, label)

    _tick("request received")
    logger.warning("[REQ] model=%s conv=%s msgs=%d", model_name, conv_id, len(messages))

    # ── Model capability detection & routing ───────────────────────
    model_cap = detect_model(model_name)
    logger.info(
        "Model router: %s → multimodal=%s tool_calling=%s vision_model=%s",
        model_name, model_cap.is_multimodal, model_cap.has_tool_calling, model_cap.vision_model,
    )
    _tick(f"model detection done")

    # Extract user message and attachments
    text_only, full_content, attachments_meta, decoded_attachments = _extract_user_message(messages)
    _tick(f"extract message done (attachments={len(decoded_attachments)})")

    has_images = any(a["type"] == "image" for a in attachments_meta)

    # ── Image routing: all models use pre-analysis ────────────────
    # Images are decoded to temp files, analyzed by vision model,
    # results injected as text. This is faster and more reliable than
    # sending base64 inline (Ollama VL models reject some formats).
    if has_images:
        logger.info("Pre-analyzing images with %s", model_cap.vision_model)
        vision_results = []
        for att in decoded_attachments:
            if att.get("type") == "image" and att.get("path"):
                try:
                    yield _sse_chunk({
                        "choices": [{"index": 0, "delta": {
                            "content": "\n🔍 analyzing image...\n"
                        }, "finish_reason": None}],
                    })

                    import os as _os
                    _os.environ["VISION_MODEL"] = model_cap.vision_model
                    from paradise.tools.vision_analyze import _handle_vision_analyze
                    result_str = await _handle_vision_analyze({
                        "path": att["path"],
                        "question": text_only or "描述这张图片的内容",
                    })
                    import json as _json
                    result = _json.loads(result_str) if isinstance(result_str, str) else result_str
                    desc = result.get("description", "")
                    if desc:
                        vision_results.append(f"[图片分析结果] {desc}")
                        logger.info("Vision pre-analysis: %s → %s", att["path"], desc[:100])
                except Exception as ve:
                    logger.warning("Vision pre-analysis failed: %s", ve)

        if vision_results:
            text_only = text_only + "\n\n" + "\n".join(vision_results)
        _tick(f"image pre-analysis done ({len(vision_results)} results)")

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

    # Store decoded attachment paths for tool phase
    if decoded_attachments:
        ctx._decoded_attachments = decoded_attachments

    # ── Model routing info for agent ───────────────────────────────
    ctx._model_cap = model_cap
    if SKIP_TOOLS:
        ctx.enable_tools = False
        logger.info("SKIP_TOOLS=1 — tool phase disabled")
    if SKIP_THINK:
        ctx._skip_think = True
        logger.info("SKIP_THINK=1 — think phase disabled")
    if model_cap.tools_prompt and not SKIP_TOOLS:
        ctx._tools_prompt = True
        logger.info("Model '%s' → system-prompt ReAct mode", model_name)
    if model_cap.is_multimodal:
        ctx._skip_vision_tool = True

    # ── Context compaction ───────────────────────────────────────────
    # Check if conversation context exceeds 50% of model window.
    # If so, compact older messages into summaries (fold/expand model).
    compactor = get_compactor(model_name)
    usage = compactor.current_usage_ratio(messages)
    _compact_context = ""
    if compactor.should_compact(messages):
        logger.info(
            "Context at %.0f%% of %d window — compacting (conv=%s, msgs=%d)",
            usage * 100, compactor.window_size, conv_id, len(messages),
        )
        try:
            messages = await compactor.compact(messages, conv_id)
            _compact_context = summary_index.render_context(conv_id)
            logger.info("Compacted to %d messages", len(messages))
        except Exception as e:
            logger.warning("Context compaction failed: %s", e)
            # Continue with original messages

    # All models use pre-analysis — strip image_url parts from messages
    if has_images:
        messages = _strip_image_parts(messages)

    ctx._raw_messages = messages
    if _compact_context:
        ctx._compact_context = _compact_context

    # ── User profile from Android app ────────────────────────────────
    if user_profile:
        ctx._user_profile = user_profile

    try:
        # Phase 0: Intent check (needs_tools)
        from paradise.core.agent import _needs_tools, _should_think
        needs_tools = ENABLE_TOOLS and _needs_tools(text_only)
        should_think = THINK_PHASE and _should_think(text_only)

        if recorder:
            recorder.intent(needs_tools, should_think)

        # Run agent loop with keep-alive pings.
        # The THINK phase uses non-streaming chat() which can take 5-10s.
        # Without keep-alive, OkHttp/ngrok/ZeroNews may drop the connection.
        # We use a queue: background task feeds agent events, main loop
        # yields SSE chunks or keep-alive comments.
        content_full = []
        event_queue: asyncio.Queue = asyncio.Queue()

        async def _feed_events():
            """Pull events from agent and put them in the queue."""
            try:
                # Phase 2.9: prod branch with supervisor_enabled takes
                # precedence over langgraph_enabled. Three-way dispatch:
                #   supervisor_enabled → run_via_supervisor
                #   langgraph_enabled  → run_via_graph
                #   else               → handle_message
                _is_prod = os.getenv("PARADISE_MODE", "dev") == "prod"
                _use_supervisor = _is_prod and \
                    getattr(agent.config, "supervisor_enabled", False)
                _use_graph = _is_prod and not _use_supervisor and \
                    getattr(agent.config, "langgraph_enabled", False)
                if _use_supervisor:
                    agent_method = agent.run_via_supervisor
                elif _use_graph:
                    agent_method = agent.run_via_graph
                else:
                    agent_method = agent.handle_message
                async for evt in agent_method(ctx):
                    await event_queue.put(evt)
                await event_queue.put(None)  # Sentinel: stream complete
            except Exception as exc:
                await event_queue.put({"type": "error", "content": str(exc)})
                await event_queue.put(None)

        feed_task = asyncio.create_task(_feed_events())

        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                # No event yet — send keep-alive so clients don't disconnect
                yield b": keep-alive\n\n"
                continue

            if event is None:
                _tick("agent stream complete")
                break  # Stream complete

            evt_type = event.get("type", "")

            if evt_type == "intent":
                # Phase 3-C: supervisor emits intent as soon as classification
                # completes, giving the client early visibility into routing.
                # Format as reasoning_content so the Android app shows it in
                # the "thinking" panel (same delta shape as thinking events).
                intent_label = event.get("content", "unknown")
                intent_mode = event.get("mode", "chat")
                intent_info = f"🧭 意图: {intent_label} → 模式: {intent_mode}\n"
                yield _sse_chunk({
                    "choices": [{
                        "index": 0,
                        "delta": {"reasoning_content": intent_info},
                        "finish_reason": None,
                    }],
                })

            elif evt_type == "thinking":
                _tick("agent → thinking")
                if recorder:
                    recorder.thinking(event.get("content", ""))
                yield _sse_chunk({
                    "choices": [{
                        "index": 0,
                        "delta": {"reasoning_content": event.get("content", "")},
                        "finish_reason": None,
                    }],
                })

            elif evt_type == "content":
                if not content_full:
                    _tick("agent → first content token")
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
                tool_name = event.get("name", "tool")
                tool_emoji = _tool_emoji(tool_name)
                tool_duration = event.get("duration_ms", 0)
                tool_info = f"{tool_emoji} {tool_name} ({(tool_duration/1000):.1f}s)"
                yield _sse_chunk({
                    "choices": [{
                        "index": 0,
                        "delta": {"reasoning_content": tool_info + "\n"},
                        "finish_reason": None,
                    }],
                })
                # web_search: emit results as structured ToolCard for Android UI
                if tool_name == "web_search":
                    query = event.get('arguments', {}).get('query', '')
                    cards = _parse_search_result_cards(event.get("result", ""))
                    if cards:
                        # Emit tool_cards for SearchResultsSection button
                        yield _sse_chunk({
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "tool_cards": [{
                                        "type": "search_results",
                                        "title": f"搜索: {query}",
                                        "results": cards,
                                    }]
                                },
                                "finish_reason": None,
                            }],
                        })
                        # Emit search summary into reasoning for thinking section
                        lines = [f"\n[搜索词]: {query}", f"[搜索结果]:"]
                        for c in cards[:5]:
                            lines.append(f"  - {c.get('title', '')}")
                        yield _sse_chunk({
                            "choices": [{
                                "index": 0,
                                "delta": {"reasoning_content": "\n".join(lines) + "\n"},
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
                final_content = event.get("content", "")
                # Supervisor path: no "content" events were emitted (the
                # LLM ran inside the subgraph), so the actual response
                # lives only here. Stream it before the finish marker.
                # Dev path: content_full already has streamed tokens; skip.
                if not content_full and final_content:
                    content_full.append(final_content)
                    if recorder:
                        recorder.content_delta(final_content)
                    yield _sse_chunk({
                        "choices": [{
                            "index": 0,
                            "delta": {"content": final_content},
                            "finish_reason": None,
                        }],
                    })
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

    finally:
        # Ensure feed_task is cleaned up
        if 'feed_task' in locals():
            feed_task.cancel()

    elapsed = time.time() - t0
    logger.info(
        "Turn %d complete: conv=%s model=%s elapsed=%.2fs content_len=%d",
        turn, conv_id, model_name, elapsed,
        sum(len(c) for c in content_full),
    )


def _sse_chunk(data: dict) -> bytes:
    """Format a dict as an SSE 'data:' chunk."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _strip_image_parts(messages: list[dict]) -> list[dict]:
    """Remove image_url content parts from messages for non-VL models.

    Text parts are preserved; image_url parts are replaced with a brief
    note pointing to the vision_analyze tool result.
    """
    cleaned = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    # Skip image_url parts — handled by vision_analyze tool
            new_content = " ".join(text_parts) if text_parts else content
            cleaned.append({**msg, "content": new_content})
        else:
            cleaned.append(msg)
    return cleaned


def _tool_emoji(name: str) -> str:
    """Return an emoji for a tool name."""
    return {
        "web_search": "\U0001f50d",
        "web_fetch": "\U0001f310",
        "vision_analyze": "\U0001f5bc",
        "file_parse": "\U0001f4c4",
        "read_file": "\U0001f4d6",
        "bash": "\U0001f4bb",
        "search_files": "\U0001f50e",
        "context_expand": "\U0001f4c2",
    }.get(name, "\U0001f527")


def _parse_search_result_cards(result_text: str) -> list[dict]:
    """Parse web_search result text into structured card entries.

    Expected format (from web_search._format_results):
      ### 1. Title
      URL: https://...
      - bullet 1
      - bullet 2

    Returns list of {title, url, snippet} dicts.
    """
    import re
    cards = []
    # Split by "### N. Title" or "**Title**"
    entries = re.split(r'\n(?=(?:### \d+\.|Page:|\*\*))', result_text)
    for entry in entries:
        entry = entry.strip()
        if not entry or entry.startswith("Web search results") or entry.startswith("搜索结果"):
            continue
        # Extract title
        title_match = re.search(r'(?:### \d+\.\s*|Page:\s*|\*\*)(.+?)(?:\*\*|\n|$)', entry)
        title = title_match.group(1).strip() if title_match else ""
        # Extract URL
        url_match = re.search(r'URL:\s*(\S+)', entry)
        url = url_match.group(1) if url_match else ""
        # Extract snippet (first 120 chars after URL, skip bullets)
        rest = entry[url_match.end():].strip() if url_match else entry
        snippet = re.sub(r'^-\s+', '', rest.split('\n')[0] if rest else '')[:120]
        if title:
            cards.append({"title": title, "url": url, "snippet": snippet})

    return cards[:10]  # Max 10 cards


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
