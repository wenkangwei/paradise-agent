"""Voice Transcribe Skill — STT via Whisper.

Self-registers as tool "voice_transcribe" in toolset "voice".
Uses faster-whisper (CTranslate2 Whisper) for fast CPU inference.
First run downloads the model (~1.5GB for medium, ~500MB for small).

Usage in agent TOOL phase:
  tool_call: voice_transcribe(path="/tmp/audio.m4a", language="zh")
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")  # tiny/base/small/medium/large
LANGUAGE_DEFAULT = "zh"
TRANSCRIBE_EMOJI = "\U0001f399"

VOICE_TRANSCRIBE_SCHEMA = {
    "name": "voice_transcribe",
    "description": (
        "Transcribe speech from an audio file using Whisper. "
        "Supports m4a, mp3, wav, ogg formats. "
        "Returns the transcribed text in Chinese (default) or specified language."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the audio file",
            },
            "language": {
                "type": "string",
                "description": "Language code (default: zh)",
            },
        },
        "required": ["path"],
    },
}

# Lazy-loaded model
_model = None


def _get_model():
    global _model
    if _model is not None:
        return _model
    try:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
        logger.info("Whisper model '%s' loaded", MODEL_SIZE)
    except ImportError:
        logger.warning("faster-whisper not installed")
        return None
    except Exception as e:
        logger.warning("Whisper model load failed: %s", e)
        return None
    return _model


def _handle_voice_transcribe(args: dict[str, Any]) -> str:
    path = args.get("path", "")
    language = args.get("language", LANGUAGE_DEFAULT) or LANGUAGE_DEFAULT

    if not path.strip():
        return tool_error("audio file path is required")

    filepath = Path(path)
    if not filepath.exists():
        return tool_error(f"audio file not found: {path}")

    ext = filepath.suffix.lower()
    if ext not in {".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac", ".opus"}:
        return tool_error(f"unsupported audio format: {ext}")

    model = _get_model()
    if model is None:
        return tool_error("Whisper model not available. Install: pip install faster-whisper")

    try:
        segments, info = model.transcribe(str(filepath), language=language, beam_size=5)
        detected_lang = info.language
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        logger.info("voice_transcribe: %s → '%s' (lang=%s, prob=%.2f)",
                     filepath.name, text[:100], detected_lang, info.language_probability)
        return tool_result({
            "text": text,
            "language": detected_lang,
            "confidence": round(info.language_probability, 3),
        })
    except Exception as e:
        return tool_error(f"Transcription failed: {e}")


registry.register(
    name="voice_transcribe",
    toolset="voice",
    schema=VOICE_TRANSCRIBE_SCHEMA,
    handler=_handle_voice_transcribe,
    is_async=False,
    description="Transcribe speech to text using Whisper (faster-whisper)",
    emoji=TRANSCRIBE_EMOJI,
    max_result_size_chars=5000,
)
