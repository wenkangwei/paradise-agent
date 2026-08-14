"""AiChat Agent Server — FastAPI entry point.

Two chat endpoints:
  - /v1/chat/completions   Thin proxy → Ollama (Phase 1, always available)
  - /v1/agent/chat/completions  Paradise agent loop (Phase 2, toggle via agent_config.json)
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from api.routes import openai_proxy
# Routes ported from feat/live2d-interact-tab (additive; only matter when
# the imported modules exist, which they do in this prod branch).
from api.routes import data_upload, proactive
from api.routes.tts import simple_router as tts_simple_router
from api.routes.tts import openai_router as tts_openai_router

logger = logging.getLogger("main")

# ── Prod-mode bootstrap ────────────────────────────────────────────
# Phase 1: when PARADISE_MODE=prod, load prod config + register middleware.
# On main branch (PARADISE_MODE unset / "dev"), this entire block is skipped
# and main.py behaves identically to before — no new deps, no new behaviour.
_PARADISE_MODE = os.getenv("PARADISE_MODE", "dev")
_PROD_CONFIG = None
if _PARADISE_MODE == "prod":
    try:
        from paradise.factory import load_prod_config
        _PROD_CONFIG = load_prod_config()
        logger.info("[prod] Loaded config.prod.yaml (mode=%s)", _PROD_CONFIG.mode)
    except Exception as e:
        # Fail loud — prod should not silently degrade to dev
        raise RuntimeError(f"PARADISE_MODE=prod but config load failed: {e}") from e

# ── Agent route ────────────────────────────────────────────────────

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent_handler import (
    process_message_stream,
    warmup_agent,
    AGENT_ENABLED,
    DEFAULT_MODEL,
)

agent_router = APIRouter(tags=["agent"])


class AgentChatMessage(BaseModel):
    role: str
    content: str | list[dict] | None = None
    name: str | None = None

    model_config = {"extra": "allow"}


class AgentChatRequest(BaseModel):
    model: str = ""
    messages: list[AgentChatMessage]
    stream: bool = True
    temperature: float | None = None

    model_config = {"extra": "allow"}


@agent_router.post("/agent/chat/completions")
async def agent_chat_completions(request: Request, body: AgentChatRequest):
    """OpenAI-compatible endpoint powered by Paradise agent loop.

    Unlike the thin proxy (/v1/chat/completions), this route runs through
    the full ReAct agent: INTENT → [TOOL] → [THINK] → RESPOND.
    """
    if not AGENT_ENABLED:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"error": "Agent is disabled. Set AGENT_ENABLED=1 or enable in agent_config.json."},
        )

    conv_id = request.headers.get("X-Conversation-Id", "")
    user_profile = request.headers.get("X-User-Profile", "")
    model = body.model or DEFAULT_MODEL
    messages = [m.model_dump(exclude_none=True) for m in body.messages]

    if body.stream:
        return StreamingResponse(
            process_message_stream(messages, model, conv_id, user_profile),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Agent-Enabled": "true",
            },
        )

    # Non-streaming: collect full response
    from fastapi.responses import JSONResponse
    full_content = []
    async for chunk in process_message_stream(messages, model, conv_id):
        import json
        if isinstance(chunk, bytes):
            line = chunk.decode("utf-8").strip()
            if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                try:
                    data = json.loads(line[6:])
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    c = delta.get("content", "")
                    if c:
                        full_content.append(c)
                except Exception:
                    pass
    return JSONResponse(content={
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(full_content)},
            "finish_reason": "stop",
        }],
    })


# ── App ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("AiChat Agent Server starting...")
    if AGENT_ENABLED:
        import asyncio
        asyncio.create_task(warmup_agent())
    # Initialize proactive scheduler (ported from feat/live2d-interact-tab)
    _init_proactive()
    # Phase 6: prune expired compaction-summary files (TTL 15 days) from disk
    try:
        from context_compactor import get_compaction_service
        _pruned = get_compaction_service().index.prune_all()
        if _pruned:
            logger.info("Pruned %d expired compaction-summary files", _pruned)
    except Exception:
        logger.warning("Compaction-summary prune failed", exc_info=True)
    yield
    # Shutdown
    from agent_handler import session_manager
    for conv_id in list(session_manager._agents.keys()):
        await session_manager.remove(conv_id)
    # Stop proactive scheduler
    from proactive.scheduler import get_scheduler
    scheduler = get_scheduler()
    for conv_id in list(scheduler._tasks.keys()):
        scheduler.unregister(conv_id)
    logger.info("AiChat Agent Server stopped")


def _init_proactive():
    """Initialize proactive scheduler from agent_config.json."""
    import json
    from pathlib import Path
    from proactive.scheduler import init_scheduler

    cfg_path = Path(__file__).parent / "agent_config.json"
    proactive_cfg = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            full_cfg = json.load(f)
        proactive_cfg = full_cfg.get("proactive", {})

    # Environment variables override config
    if os.getenv("PROACTIVE_ENABLED"):
        proactive_cfg["enabled"] = os.getenv("PROACTIVE_ENABLED", "").lower() in ("1", "true", "yes")
    if os.getenv("PROACTIVE_INTERVAL_MINUTES"):
        proactive_cfg["interval_minutes"] = int(os.getenv("PROACTIVE_INTERVAL_MINUTES", "30"))

    scheduler = init_scheduler(proactive_cfg)
    logger.info(
        "Proactive scheduler initialized: enabled=%s interval=%dmin",
        scheduler.enabled, scheduler._interval // 60,
    )


app = FastAPI(
    title="AiChat Agent Server",
    description="OpenAI-compatible proxy + Paradise agent loop bridging Android AiChat to Ollama",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Prod middleware chain (Phase 1) ────────────────────────────────
# Register only when prod config loaded successfully. Order matters:
# FastAPI's add_middleware inserts at front of stack, so we add in
# reverse-of-execution: rate_limit first (innermost), guardrails last (outermost).
# Net request flow:  Guardrails → Auth → RateLimit → CORS → handler
if _PROD_CONFIG is not None and _PROD_CONFIG.mode == "prod":
    from middleware import (
        AuthMiddleware,
        RateLimitMiddleware,
        GuardrailsMiddleware,
    )
    _ingress = (_PROD_CONFIG.prod_raw or {}).get("ingress", {})  # type: ignore[attr-defined]
    _api_keys = _ingress.get("api_keys", [])
    _rpm = int(_ingress.get("rate_limit_per_minute", 60))
    _cost_usd = float(_ingress.get("cost_budget_per_request_usd", 0.05))

    # innermost → outermost (add in reverse)
    app.add_middleware(RateLimitMiddleware, rate_limit_per_minute=_rpm)
    app.add_middleware(AuthMiddleware, api_keys=_api_keys)
    app.add_middleware(
        GuardrailsMiddleware,
        cost_budget_per_request_usd=_cost_usd,
    )
    logger.info("[prod] Middleware chain registered: Guardrails → Auth → RateLimit(%d rpm)", _rpm)

# Thin proxy (Phase 1, always available as fallback)
app.include_router(openai_proxy.router, prefix="/v1")
# Agent loop (Phase 2, toggle via agent_config.json)
app.include_router(agent_router, prefix="/v1")
# Data collection (feedback, sessions, profiles) — ported from live2d
app.include_router(data_upload.router)
# Proactive agent messaging — ported from live2d
app.include_router(proactive.router)
# TTS (edge-tts): simple endpoint + OpenAI-compatible /v1/audio/speech
app.include_router(tts_simple_router)
app.include_router(tts_openai_router)


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": "1.1.0",
        "agent_enabled": AGENT_ENABLED,
    }


# ── Phase 6: Metrics endpoint (prometheus exposition) ────────────
@app.get("/metrics")
async def metrics():
    """Prometheus exposition — histograms of TTFT / tool latency / cost."""
    from fastapi import Response
    try:
        from paradise.observability.metrics import render_prometheus
        content = render_prometheus()
        return Response(content=content, media_type="text/plain; version=0.0.4")
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": "metrics_unavailable", "detail": str(exc)},
        )


@app.post("/api/stt/transcribe")
async def stt_transcribe(file: UploadFile = File(...), language: str = "zh"):
    """Transcribe uploaded audio file using Whisper.

    Accepts multipart file upload, returns {"text": "...", "language": "..."}
    """
    import tempfile
    suffix = Path(file.filename or "audio.m4a").suffix or ".m4a"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        from paradise.tools.voice_transcribe import _handle_voice_transcribe
        import json
        result = _handle_voice_transcribe({"path": tmp_path, "language": language})
        data = json.loads(result) if isinstance(result, str) else result
        if "error" in data:
            return JSONResponse(status_code=500, content=data)
        return data
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
