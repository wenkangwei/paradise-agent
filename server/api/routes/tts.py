"""Text-to-Speech Routes — edge-tts (Microsoft online, zero-cost) endpoints.

Endpoints:
  POST /api/tts/synthesize              Simple {text, voice?, rate?, pitch?}
  POST /v1/audio/speech   (mounted at /v1 from main.py via prefix kwarg)
                                        OpenAI-compatible: {model, input, voice, response_format}

Both return audio/mpeg streams.

Notes:
  - edge-tts calls Microsoft public TTS API (speech.platform.bing.com).
    Needs HTTPS outbound. WSL2 with proxy env may interfere — if 502/timeout,
    check HTTPS_PROXY and ensure Microsoft domains are reachable.
  - Returns full mp3 (buffered). For very long text, response latency = TTS
    generation time. MVP fine for short companion-chat replies (≤300 chars).
"""

from __future__ import annotations

import io
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("tts_routes")

# Two routers: one mounted at /api/tts (simple), one at /v1 (OpenAI-compatible)
simple_router = APIRouter(prefix="/api/tts", tags=["tts"])
openai_router = APIRouter(prefix="/v1", tags=["tts"])

# Sensible defaults. Voice list: `python -m edge_tts --list-voices`
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"  # 中文女声；二次元感换 ja-JP-NanamiNeural
DEFAULT_RATE = "+0%"
DEFAULT_PITCH = "+0Hz"

# OpenAI's TTS API ships six canonical voices (alloy/echo/fable/onyx/nova/shimmer).
# edge-tts uses Microsoft neural voice names instead. When an OpenAI-compatible
# client (Android HttpTtsProvider, openai-python, etc.) sends `voice: "alloy"`,
# we transparently remap so the request just works.
_OPENAI_VOICE_MAP = {
    "alloy":   "zh-CN-XiaoxiaoNeural",   # neutral, default female
    "nova":    "zh-CN-XiaoyiNeural",      # young female
    "shimmer": "zh-CN-liaoning-XiaobeiNeural",  # warm female
    "echo":    "zh-CN-YunyangNeural",     # adult male
    "fable":   "zh-CN-YunxiNeural",       # narrator male
    "onyx":    "zh-CN-YunjianNeural",     # deep male
}


def _normalize_voice(voice: str | None) -> str:
    """Translate OpenAI voice aliases to edge-tts names; pass through otherwise."""
    if not voice:
        return DEFAULT_VOICE
    return _OPENAI_VOICE_MAP.get(voice.strip().lower(), voice)


# ── Simple endpoint ────────────────────────────────────────────────────

class TTSRequest(BaseModel):
    text: str
    voice: str | None = None
    rate: str | None = None     # e.g. "+10%", "-5%"
    pitch: str | None = None    # e.g. "+0Hz"


@simple_router.post("/synthesize")
async def synthesize(body: TTSRequest):
    """Simple edge-tts endpoint. See module docstring."""
    return await _do_synthesize(body.text, body.voice, body.rate, body.pitch)


# ── OpenAI-compatible endpoint ─────────────────────────────────────────
#
# Android's HttpTtsProvider posts {model, input, voice, response_format}
# to /v1/audio/speech. We map `voice` to an edge-tts voice name (caller
# should put e.g. "zh-CN-XiaoxiaoNeural" in the voice config), and `input`
# to the text. `model` and `response_format` are accepted but ignored —
# edge-tts always emits mp3.

class OpenAISpeechRequest(BaseModel):
    model: str | None = None       # ignored; OpenAI uses tts-1 / tts-1-hd
    input: str
    voice: str | None = None
    response_format: str | None = "mp3"  # only mp3 supported here


@openai_router.post("/audio/speech")
async def openai_speech(body: OpenAISpeechRequest):
    """OpenAI-compatible TTS endpoint — wraps edge-tts.

    Configure the Android app's VoiceConfig.ttsUrl to:
      http://<server>:8000/v1/audio/speech
    """
    if not body.input or not body.input.strip():
        return JSONResponse(status_code=400, content={"error": "input required"})
    return await _do_synthesize(body.input, body.voice, None, None)


# ── Shared synth implementation ────────────────────────────────────────

async def _do_synthesize(
    text: str,
    voice: str | None,
    rate: str | None,
    pitch: str | None,
):
    if not text or not text.strip():
        return JSONResponse(status_code=400, content={"error": "text required"})

    v = _normalize_voice(voice)
    r = rate or DEFAULT_RATE
    p = pitch or DEFAULT_PITCH

    try:
        import edge_tts
    except ImportError:
        logger.error("edge-tts not installed. Run: pip install edge-tts")
        return JSONResponse(
            status_code=500,
            content={"error": "edge-tts dependency missing on server"},
        )

    try:
        mp3_buf = io.BytesIO()
        communicate = edge_tts.Communicate(text, v, rate=r, pitch=p)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_buf.write(chunk["data"])
        mp3_buf.seek(0)

        if mp3_buf.getbuffer().nbytes == 0:
            return JSONResponse(
                status_code=502,
                content={"error": "TTS produced empty audio (check voice name / proxy)"},
            )

        return StreamingResponse(mp3_buf, media_type="audio/mpeg")
    except Exception as e:
        logger.exception("TTS synthesis failed")
        return JSONResponse(
            status_code=502,
            content={"error": f"tts_failed: {type(e).__name__}: {e}"},
        )


# ── Voice listing ──────────────────────────────────────────────────────

@simple_router.get("/voices")
async def list_vvoices(limit: int = 20):
    """List available edge-tts voices (sampled). Useful for UI voice picker."""
    try:
        import edge_tts
    except ImportError:
        return JSONResponse(status_code=500, content={"error": "edge-tts not installed"})

    import inspect
    if inspect.iscoroutinefunction(edge_tts.list_voices):
        all_voices = await edge_tts.list_voices()
    else:
        all_voices = edge_tts.list_voices()

    voices = [
        {
            "name": v.get("ShortName"),
            "gender": v.get("Gender"),
            "locale": v.get("Locale"),
        }
        for v in all_voices[:limit]
    ]
    return {"voices": voices, "count": len(voices), "total": len(all_voices)}
