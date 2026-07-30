"""OpenAI-compatible proxy route.

Bridges AiChat Android (and any OpenAI-compatible client) to a local
Ollama instance. MVP for Phase 1 of the PC-agent integration plan:

  Android app  →  POST /v1/chat/completions  →  this route  →  inference backend :8001

Why a thin proxy instead of using paradise's transport layer:
paradise is an agent harness (loop / memory / reflection / tools).
Phase 1 only needs "chat completion" — no agent features. Routing
through paradise would couple MVP code to harness internals we don't
need yet. Phase 2 will swap the inner httpx call for
`paradise.transports.openai_compat` once we want tool-use, retry,
and multi-provider routing.

Endpoint:
  POST /v1/chat/completions     (prefix /v1 mounted in main.py)

Request body: standard OpenAI ChatCompletionCreateRequest
  {model, messages, stream?, temperature?, tools?, ...}

Streaming response: text/event-stream (SSE)
  data: {choices: [{delta: {content: "..."}}]}
  ...
  data: [DONE]

Non-streaming response: application/json
  Standard ChatCompletion object, echoed from Ollama.

Auth: none (MVP, LAN-only). Phase 2 will add Bearer token.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter(tags=["openai-proxy"])

# Inference backend — Ollama on :11434 by default for latency stability.
# Override via env to point at the ORPO serve_orpo.py server (:8001).
INFER_BASE_URL = os.getenv("INFER_BASE_URL", "http://localhost:11434")
INFER_CHAT_URL = f"{INFER_BASE_URL}/v1/chat/completions"
INFER_MODELS_URL = f"{INFER_BASE_URL}/v1/models"
INFER_HEALTH_URL = f"{INFER_BASE_URL}/health"
# Backward-compat alias
if os.getenv("OLLAMA_BASE_URL"):
    INFER_BASE_URL = os.getenv("OLLAMA_BASE_URL")
    INFER_CHAT_URL = f"{INFER_BASE_URL}/v1/chat/completions"
    INFER_MODELS_URL = f"{INFER_BASE_URL}/v1/models"
    INFER_HEALTH_URL = f"{INFER_BASE_URL}/health"

# Connect/p/read timeouts for streaming. read=None lets the stream
# block indefinitely between chunks (Ollama can take 10s+ to emit
# the first token on cold models).
_TIMEOUTS = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)

# Module-level reference to httpx.AsyncClient. Tests patch this
# attribute only (not the global httpx module) so their own test
# clients keep working.
AsyncClient = httpx.AsyncClient


class ChatMessage(BaseModel):
    role: str
    # OpenAI has two content formats: legacy string, and multimodal
    # list of parts (`[{"type":"text","text":"..."},
    # {"type":"image_url",...}]`). Ollama only accepts the string
    # form, so we accept both here and flatten lists to text in
    # `_normalize_payload` before forwarding.
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    model_config = {"extra": "allow"}


def _flatten_content(content: Any) -> Any:
    """Normalize content for Ollama forwarding.

    - Plain text → return as-is
    - List with ONLY text parts → join into string (legacy plain-text Ollama)
    - List with image_url parts → return list as-is (Ollama supports
      OpenAI multimodal format natively)
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        has_non_text = any(
            isinstance(item, dict) and item.get("type") != "text"
            for item in content
        )
        if has_non_text:
            # Preserve multimodal content — Ollama handles image_url natively
            return content
        # All text — join into plain string for simpler processing
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text", "")
                if t:
                    parts.append(t)
        return "\n".join(parts) if parts else None
    # Unknown shape — best-effort string coercion
    return str(content)


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten multimodal content lists so Ollama can accept the request."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload
    for msg in messages:
        if isinstance(msg, dict) and "content" in msg:
            msg["content"] = _flatten_content(msg["content"])
    return payload


class ChatCompletionRequest(BaseModel):
    """OpenAI Chat Completions request schema (subset we forward).

    `model` is forwarded verbatim to Ollama — the app's modelName
    field ends up here. Extra fields Ollama doesn't understand are
    stripped by the model_extra ignore.
    """

    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    stop: str | list[str] | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    user: str | None = None

    model_config = {"extra": "allow"}


@router.post("/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint.

    Forwards to Ollama's native /v1/chat/completions. The `request:
    Request` param lets us read the raw Authorization header for
    Phase 2 auth pass-through without changing the schema now.
    """
    # Build the forwarded payload. model_dump drops None values so
    # Ollama doesn't see null fields (some older Ollama versions
    # reject unknown fields with null). Then flatten any multimodal
    # `content: [{"type":"text",...}]` lists into plain strings —
    # Ollama's OpenAI-compat endpoint only accepts string content.
    payload = _normalize_payload(body.model_dump(exclude_none=True))

    # Pass through Authorization header if present (Ollama ignores
    # it by default; we keep the hook for future auth scenarios).
    auth = request.headers.get("Authorization")
    headers = {"Authorization": auth} if auth else {}

    if body.stream:
        return StreamingResponse(
            _stream_from_ollama_with_headers(payload, headers),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    # Non-streaming path
    try:
        async with AsyncClient(timeout=_TIMEOUTS) as client:
            response = await client.post(
                INFER_CHAT_URL, json=payload, headers=headers
            )
            response.raise_for_status()
            return JSONResponse(content=response.json())
    except httpx.ConnectError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Inference backend unreachable at {INFER_CHAT_URL}: {e}",
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Inference backend error: {e.response.text}",
        )


async def _stream_from_ollama_with_headers(
    payload: dict[str, Any], headers: dict[str, str]
) -> Any:
    """Wrapper that injects Authorization header into the stream request.

    Emits SSE keep-alive comments (`: keep-alive\\n\\n`) every 5s while
    waiting for Ollama to produce the next chunk. Without this, Ollama's
    ~11s cold-start gap (loading model weights into VRAM on first
    request) gets RST'd by ngrok-free's idle timeout, causing the
    Android client to report "interrupted".

    SSE spec: lines starting with `:` are comments and must be ignored
    by clients, so OpenAI-compatible parsers (incl. OkHttp-based) will
    drop them automatically.
    """
    import asyncio
    import time
    import logging
    log = logging.getLogger("openai_proxy")
    t0 = time.time()
    total_bytes = 0
    chunk_count = 0
    keepalive_count = 0
    # 2s keep-alive interval — aggressive but necessary. OkHttp-based
    # clients (AiChat Android) cancel SSE streams after ~5s of no
    # application data, and SSE comment lines (`: keep-alive`) don't
    # count as "data" for some OkHttp builds. So we emit a real OpenAI
    # ping chunk with a single space content every 2s; this is treated
    # as a ContentDelta by the parser, preventing the retry logic from
    # throwing "已重试 3 次" before model tokens arrive. The leading
    # space is harmless and visible enough to keep the UI alive.
    KEEPALIVE_INTERVAL = 2
    _PING_CHUNK = (
        b'data: {"choices":[{"index":0,"delta":{"content":" "},"finish_reason":null}]}\n\n'
    )
    try:
        async with AsyncClient(timeout=_TIMEOUTS) as client:
            async with client.stream(
                "POST", INFER_CHAT_URL, json=payload, headers=headers
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    err = {
                        "error": {
                            "message": f"Ollama {response.status_code}: "
                            f"{body.decode('utf-8', errors='replace')}",
                            "type": "upstream_error",
                        }
                    }
                    yield f"data: {err}\n\ndata: [DONE]\n\n".encode("utf-8")
                    return
                # Iterate Ollama chunks, but if it stalls for more
                # than KEEPALIVE_INTERVAL, emit a comment line so the
                # transport (ngrok / OkHttp) sees traffic.
                aiter = response.aiter_bytes().__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            aiter.__anext__(), timeout=KEEPALIVE_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        keepalive_count += 1
                        yield _PING_CHUNK
                        continue
                    except StopAsyncIteration:
                        break
                    if chunk:
                        total_bytes += len(chunk)
                        chunk_count += 1
                        yield chunk
                # Stream complete — log diagnostics so we can see if
                # Android-side "interrupted" errors correspond to
                # fully-streamed responses on the PC side.
                elapsed = time.time() - t0
                log.warning(
                    "stream done: model=%s chunks=%d bytes=%d "
                    "keepalives=%d elapsed=%.2fs",
                    payload.get("model"),
                    chunk_count,
                    total_bytes,
                    keepalive_count,
                    elapsed,
                )
    except httpx.ConnectError as e:
        err = {"error": {"message": f"Ollama unreachable: {e}", "type": "connection_error"}}
        yield f"data: {err}\n\ndata: [DONE]\n\n".encode("utf-8")
    except asyncio.CancelledError:
        # Client (ngrok / OkHttp) closed the stream mid-flight.
        # FastAPI injects CancelledError into the generator at the next
        # yield — this is the signature of "已中断" on the Android side.
        log.warning(
            "stream cancelled by client: model=%s chunks=%d bytes=%d "
            "keepalives=%d elapsed=%.2fs (client/ngrok closed early)",
            payload.get("model"),
            chunk_count,
            total_bytes,
            keepalive_count,
            time.time() - t0,
        )
        # Do NOT re-raise: StreamingResponse already tore down. Re-yielding
        # would just be dropped.
        return
    except Exception as e:
        # Catch-all so we never silently lose the stream-done diagnostic.
        # If we see this fire, the type/traceback tells us the real cause.
        log.warning(
            "stream error: model=%s %s.%s: %s chunks=%d bytes=%d "
            "elapsed=%.2fs",
            payload.get("model"),
            type(e).__module__,
            type(e).__name__,
            e,
            chunk_count,
            total_bytes,
            time.time() - t0,
            exc_info=True,
        )
        err = {
            "error": {
                "message": f"stream aborted: {type(e).__name__}: {e}",
                "type": "stream_error",
            }
        }
        yield f"data: {err}\n\ndata: [DONE]\n\n".encode("utf-8")


async def warmup_infer() -> None:
    """Health-check the inference backend during FastAPI startup.

    serve_orpo.py loads the model at its own startup (takes ~30s), so
    by the time aipet backend boots the backend is either up or not —
    no cold-start to hide. We just ping /health and log the result so
    the operator sees a clear signal in the log.

    Best-effort: errors are logged and swallowed. The user gets a 502
    on actual chat calls if the inference backend is down.
    """
    import logging
    import time
    log = logging.getLogger("openai_proxy")
    t0 = time.time()
    try:
        async with AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            r = await client.get(INFER_HEALTH_URL)
            log.warning(
                "inference backend ok: status=%d body=%s elapsed=%.2fs",
                r.status_code, r.text[:200], time.time() - t0,
            )
    except Exception as e:
        log.warning("inference backend health-check failed (non-fatal): %s", e)


@router.get("/models")
async def list_models():
    """OpenAI-compatible /v1/models endpoint.

    AiChat app may call this to populate the model dropdown. Returns
    the inference backend's /v1/models output verbatim.
    """
    try:
        async with AsyncClient(timeout=_TIMEOUTS) as client:
            response = await client.get(INFER_MODELS_URL)
            response.raise_for_status()
            return JSONResponse(content=response.json())
    except httpx.ConnectError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Inference backend unreachable at {INFER_BASE_URL}: {e}",
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Inference backend error: {e.response.text}",
        )
