"""AiChat Agent Server — FastAPI entry point.

Two chat endpoints:
  - /v1/chat/completions   Thin proxy → Ollama (Phase 1, always available)
  - /v1/agent/chat/completions  Paradise agent loop (Phase 2, toggle via agent_config.json)
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from api.routes import openai_proxy

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

logger = logging.getLogger("main")
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
    model = body.model or DEFAULT_MODEL
    messages = [m.model_dump(exclude_none=True) for m in body.messages]

    if body.stream:
        return StreamingResponse(
            process_message_stream(messages, model, conv_id),
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
    yield
    # Shutdown
    from agent_handler import session_manager
    for conv_id in list(session_manager._agents.keys()):
        await session_manager.remove(conv_id)
    logger.info("AiChat Agent Server stopped")


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

# Thin proxy (Phase 1, always available as fallback)
app.include_router(openai_proxy.router, prefix="/v1")
# Agent loop (Phase 2, toggle via agent_config.json)
app.include_router(agent_router, prefix="/v1")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": "1.1.0",
        "agent_enabled": AGENT_ENABLED,
    }


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
