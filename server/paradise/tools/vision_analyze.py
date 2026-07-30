"""Vision Analyze Skill — interprets images using a vision-capable LLM.

Self-registers as tool "vision_analyze" in toolset "multimodal".
Sends image to Ollama vision model (llava / qwen2.5-vl / bakllava) via
Ollama native /api/chat endpoint, returns text description.

Usage in agent TOOL phase:
  tool_call: vision_analyze(path="/tmp/aichat_attachments/att_0.jpg",
                             question="描述这张图片里的内容")
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────

VISION_MODEL = os.getenv("VISION_MODEL", "llava:7b")
OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

SUPPORTED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_IMAGE_SIZE_MB = 10

# ── Schema ───────────────────────────────────────────────────────────

VISION_ANALYZE_SCHEMA = {
    "name": "vision_analyze",
    "description": (
        "Analyze an image file using a vision-capable LLM. "
        "Use when the user asks to describe, identify, or read text from an image. "
        "Supports JPEG, PNG, GIF, WebP, BMP. "
        "The image is sent to a vision model and a text description is returned."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the image file on disk",
            },
            "question": {
                "type": "string",
                "description": "Specific question about the image, e.g. '描述这张图片' or '图中的文字是什么'",
            },
        },
        "required": ["path"],
    },
}

IMAGE_EMOJI = "\U0001f5bc"


# ── Handler ──────────────────────────────────────────────────────────

async def _handle_vision_analyze(args: dict[str, Any]) -> str:
    """Execute vision analysis on an image file.

    Reads image from disk, sends to Ollama vision model via /api/chat,
    returns the model's text response.
    """
    path = args.get("path", "")
    question = args.get("question", "请详细描述这张图片的内容")

    if not path:
        return tool_error("no image path specified")

    filepath = Path(path)
    if not filepath.exists():
        return tool_error(f"image file not found: {path}")

    ext = filepath.suffix.lower()
    if ext not in SUPPORTED_IMAGE_TYPES:
        return tool_error(
            f"unsupported image type: {ext}. Supported: {', '.join(sorted(SUPPORTED_IMAGE_TYPES))}"
        )

    file_size_mb = filepath.stat().st_size / (1024 * 1024)
    if file_size_mb > MAX_IMAGE_SIZE_MB:
        return tool_error(
            f"image too large: {file_size_mb:.1f}MB (max {MAX_IMAGE_SIZE_MB}MB)"
        )

    try:
        # Read and base64-encode image
        image_bytes = filepath.read_bytes()
        b64 = base64.b64encode(image_bytes).decode("ascii")
        mime = _mime_from_ext(ext)

        # Build OpenAI-compatible multimodal request.
        # Both Ollama /v1/chat/completions and native /api/chat accept this format.
        ollama_url = f"{OLLAMA_BASE.rstrip('/')}/v1/chat/completions"

        payload = {
            "model": VISION_MODEL,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
        }

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0),
            proxy=None,  # Bypass system proxy for localhost Ollama
        ) as client:
            resp = await client.post(ollama_url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        # Extract content from OpenAI-compatible response
        choices = data.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
        else:
            content = data.get("message", {}).get("content", "")

        if not content:
            # Log the raw response for debugging
            logger.warning("vision_analyze empty response: %s", str(data)[:300])
            return tool_error("vision model returned empty response")

        logger.info(
            "vision_analyze: %s (%s, %.1fMB) → %d chars",
            filepath.name, VISION_MODEL, file_size_mb, len(content),
        )
        return tool_result({"description": content, "model": VISION_MODEL})

    except httpx.ConnectError:
        return tool_error(
            f"cannot connect to Ollama at {OLLAMA_BASE}. Is it running? "
            "Start with: ollama serve"
        )
    except httpx.HTTPStatusError as e:
        # Try to extract Ollama error body
        err_body = ""
        try:
            err_body = e.response.text[:300]
        except Exception:
            pass
        return tool_error(f"Ollama vision model error: HTTP {e.response.status_code} — {err_body}")
    except Exception as e:
        logger.exception("vision_analyze failed: %s", e)
        return tool_error(f"vision analysis failed: {type(e).__name__}: {e}")


def _mime_from_ext(ext: str) -> str:
    """Map file extension to MIME type."""
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
    }.get(ext, "image/jpeg")


# ── Self-register ────────────────────────────────────────────────────

registry.register(
    name="vision_analyze",
    toolset="multimodal",
    schema=VISION_ANALYZE_SCHEMA,
    handler=_handle_vision_analyze,
    is_async=True,
    description="Analyze images using vision LLM (requires llava or qwen2.5-vl in Ollama)",
    emoji=IMAGE_EMOJI,
    max_result_size_chars=5000,
)
