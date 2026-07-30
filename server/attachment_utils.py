"""Attachment Utilities — decode, persist, and inject file paths into agent context.

Pre-processing pipeline for multimodal chat requests:
  1. Detect image_url content parts in OpenAI-format messages
  2. Decode base64 data: URLs to temp files
  3. Inject file paths into the user message text so the agent's TOOL phase
     can call vision_analyze / file_parse on them
"""

from __future__ import annotations

import base64
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default temp directory for decoded attachments
DEFAULT_ATTACH_DIR = os.getenv("AICHAT_ATTACH_DIR", "/tmp/aichat_attachments")

# MIME type → file extension mapping
MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/html": ".html",
    "application/zip": ".zip",
    "application/x-tar": ".tar",
    "application/gzip": ".gz",
}


def decode_and_save(content_parts: list[dict], base_dir: str = "") -> list[dict]:
    """Decode base64 data: URLs in content parts to temp files.

    Args:
        content_parts: OpenAI-format content list, e.g.
            [{"type":"text","text":"..."},
             {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}]
        base_dir: temp directory root (default: /tmp/aichat_attachments/)

    Returns:
        list of attachment metadata dicts:
            [{"type":"image","mime":"image/jpeg","path":"/tmp/.../att_0.jpg",
              "original_url":"data:image/jpeg;base64,...(truncated)"}]
    """
    base = Path(base_dir or DEFAULT_ATTACH_DIR)
    base.mkdir(parents=True, exist_ok=True)
    attachments = []

    for i, part in enumerate(content_parts):
        if not isinstance(part, dict):
            continue
        if part.get("type") != "image_url":
            continue

        img = part.get("image_url", {})
        if not isinstance(img, dict):
            continue

        url = img.get("url", "")
        if not isinstance(url, str) or not url.startswith("data:"):
            # Non-base64 URL (http/https) — skip, can't decode
            if url.startswith("http"):
                attachments.append({
                    "type": "remote_image",
                    "url": url,
                })
            continue

        # Parse data: URL
        # Format: data:[<mime>][;base64],<data>
        header, _, b64_data = url.partition(",")
        if not b64_data:
            logger.warning("Invalid data URL (no comma): %.50s...", url)
            continue

        mime = "image/jpeg"
        if ";" in header:
            mime_part = header[len("data:"):]
            mime_parts = mime_part.split(";")
            mime = mime_parts[0] if mime_parts[0] else "image/jpeg"

        ext = MIME_TO_EXT.get(mime, ".bin")
        filename = f"att_{uuid.uuid4().hex[:8]}{ext}"
        filepath = base / filename

        try:
            decoded = base64.b64decode(b64_data)
            original_size = len(decoded)
            data_url_resized = ""

            # Resize large images to reduce vision model processing time
            if mime.startswith("image/"):
                resized_bytes = _resize_image(decoded, max_dim=1024)
                if resized_bytes and len(resized_bytes) < original_size:
                    logger.debug("Resized image: %d → %d bytes", original_size, len(resized_bytes))
                    decoded = resized_bytes
                    b64_resized = base64.b64encode(resized_bytes).decode("ascii")
                    data_url_resized = f"data:{mime};base64,{b64_resized}"

            filepath.write_bytes(decoded)
            att = {
                "type": "image" if mime.startswith("image/") else "file",
                "mime": mime,
                "path": str(filepath),
                "size_bytes": len(decoded),
                "original_url": url[:80] + "..." if len(url) > 80 else url,
            }
            if data_url_resized:
                att["data_url_resized"] = data_url_resized
            attachments.append(att)
            logger.debug("Saved attachment: %s (%d bytes, %s)", filepath, len(decoded), mime)
        except Exception as e:
            logger.warning("Failed to decode attachment %d: %s", i, e)

    return attachments


def inject_context(user_message: str, attachments: list[dict]) -> str:
    """Append attachment file paths to the user message so the agent sees them.

    The agent's TOOL phase will pick up these paths and call
    vision_analyze / file_parse as needed.

    Args:
        user_message: original user text
        attachments: list from decode_and_save()

    Returns:
        augmented user message with file path hints
    """
    if not attachments:
        return user_message

    lines = [user_message.rstrip()] if user_message.strip() else []
    lines.append("")

    for i, att in enumerate(attachments):
        att_type = att.get("type", "unknown")
        mime = att.get("mime", "")
        path = att.get("path", "")
        size = att.get("size_bytes", 0)

        if att_type == "image":
            lines.append(f"[附件{i + 1}: 图片文件 {path} ({mime}, {_format_size(size)})]")
        elif att_type == "file":
            lines.append(f"[附件{i + 1}: 文档文件 {path} ({mime}, {_format_size(size)})]")
        elif att_type == "remote_image":
            lines.append(f"[附件{i + 1}: 远程图片 {att.get('url', '')}]")
        else:
            lines.append(f"[附件{i + 1}: {path}]")

    return "\n".join(lines)


def _resize_image(data: bytes, max_dim: int = 1024) -> bytes | None:
    """Resize image to max_dim on the long edge. Returns None on failure."""
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        w, h = img.size
        if w <= max_dim and h <= max_dim:
            return None  # Already small enough
        # Maintain aspect ratio
        if w > h:
            new_w = max_dim
            new_h = int(h * max_dim / w)
        else:
            new_h = max_dim
            new_w = int(w * max_dim / h)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = BytesIO()
        fmt = img.format or "JPEG"
        if fmt.upper() == "GIF":
            fmt = "PNG"  # GIF → PNG to avoid animation issues
        img.save(buf, format=fmt, optimize=True)
        return buf.getvalue()
    except ImportError:
        logger.warning("Pillow not installed — skipping image resize")
        return None
    except Exception as e:
        logger.debug("Image resize skipped: %s", e)
        return None


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def cleanup_old_attachments(base_dir: str = "", max_age_hours: int = 24) -> int:
    """Remove attachment files older than max_age_hours.

    Called at server startup to prevent /tmp bloat.
    Returns count of removed files.
    """
    import time
    base = Path(base_dir or DEFAULT_ATTACH_DIR)
    if not base.exists():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in base.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("Cleaned up %d old attachment files from %s", removed, base)
    return removed
