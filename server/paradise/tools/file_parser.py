"""File Parser Skill — reads and extracts text from various file formats.

Self-registers as tool "file_parse" in toolset "multimodal".
Handles: TXT, MD, JSON, CSV, XML, HTML (direct read),
         PDF (PyPDF2 — optional), DOCX (python-docx — optional).
Images are deliberately NOT supported — use vision_analyze for images.

Usage in agent TOOL phase:
  tool_call: file_parse(path="/tmp/aichat_attachments/att_1.pdf")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ── Supported formats ────────────────────────────────────────────────

# Directly readable text formats
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv",
    ".xml", ".html", ".htm", ".yaml", ".yml", ".toml",
    ".py", ".js", ".ts", ".java", ".kt", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".sh", ".bash", ".zsh",
    ".sql", ".r", ".rb", ".php", ".swift", ".css", ".scss",
    ".cfg", ".ini", ".conf", ".env", ".log",
}

# Extensions that need special parsers
PDF_EXTENSIONS = {".pdf"}
DOCX_EXTENSIONS = {".docx", ".doc"}

MAX_CHARS_DEFAULT = 5000
MAX_CHARS_LIMIT = 20000

FILE_EMOJI = "\U0001f4c4"


# ── Schema ───────────────────────────────────────────────────────────

FILE_PARSE_SCHEMA = {
    "name": "file_parse",
    "description": (
        "Parse and extract text content from a file on disk. "
        "Supports plain text files (.txt, .md, .json, .csv, .py, .log, etc.), "
        "PDF files (.pdf, requires PyPDF2), "
        "and Word documents (.docx, requires python-docx). "
        "For images, use vision_analyze instead — this tool only handles text-based files. "
        "Returns extracted text content, truncated to max_chars (default 5000)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file on disk",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to return (default 5000, max 20000)",
            },
        },
        "required": ["path"],
    },
}


# ── Handler ──────────────────────────────────────────────────────────

def _handle_file_parse(args: dict[str, Any]) -> str:
    """Parse a file and return extracted text content."""
    path = args.get("path", "")
    max_chars = min(args.get("max_chars", MAX_CHARS_DEFAULT) or MAX_CHARS_DEFAULT, MAX_CHARS_LIMIT)

    if not path:
        return tool_error("no file path specified")

    filepath = Path(path)
    if not filepath.exists():
        return tool_error(f"file not found: {path}")
    if not filepath.is_file():
        return tool_error(f"not a file: {path}")

    ext = filepath.suffix.lower()
    file_size = filepath.stat().st_size

    if file_size > 50 * 1024 * 1024:  # 50MB
        return tool_error(f"file too large ({file_size / (1024*1024):.1f}MB)")

    # ── Image detection ──────────────────────────────────────────
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".svg"}:
        return tool_result(
            f"This is an image file ({ext}). "
            "Use the vision_analyze tool to describe this image instead of file_parse.",
            hint="vision_analyze",
        )

    # ── PDF ──────────────────────────────────────────────────────
    if ext in PDF_EXTENSIONS:
        content = _parse_pdf(filepath)

    # ── DOCX ─────────────────────────────────────────────────────
    elif ext in DOCX_EXTENSIONS:
        content = _parse_docx(filepath)

    # ── Plain text ───────────────────────────────────────────────
    elif ext in TEXT_EXTENSIONS or True:  # fallback: try reading as text
        content = _read_text(filepath)

    else:
        # Unknown format — try reading as text anyway
        content = _read_text(filepath)

    if content is None:
        return tool_error(f"failed to extract content from {path}")

    trimmed = _trim(content, max_chars)
    logger.info("file_parse: %s (%s, %d bytes) → %d chars", filepath.name, ext, file_size, len(trimmed))
    return tool_result({"content": trimmed, "file": filepath.name, "size_bytes": file_size})


# ── Parsers ──────────────────────────────────────────────────────────

def _read_text(filepath: Path) -> str | None:
    """Read a text file, trying UTF-8 first, then latin-1 as fallback."""
    for encoding in ("utf-8", "latin-1", "gbk", "gb2312"):
        try:
            return filepath.read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Last resort: read as binary and decode with errors=replace
    try:
        return filepath.read_bytes().decode("utf-8", errors="replace")
    except Exception:
        return None


def _parse_pdf(filepath: Path) -> str | None:
    """Parse a PDF file, returning extracted text.

    Tries PyPDF2 first, falls back to a clear error message.
    """
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return (
            f"[PDF file: {filepath.name}] PyPDF2 is not installed. "
            "Install with: pip install PyPDF2\n"
            "The agent cannot read this PDF directly. "
            "Suggest to the user to convert it to text or install PyPDF2."
        )

    try:
        reader = PdfReader(str(filepath))
        parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
        if not parts:
            return "[PDF file with no extractable text (possibly scanned/image-based)]"
        return "\n\n".join(parts)
    except Exception as e:
        return f"[PDF parse error: {type(e).__name__}: {e}]"


def _parse_docx(filepath: Path) -> str | None:
    """Parse a DOCX file, returning extracted text."""
    try:
        from docx import Document
    except ImportError:
        return (
            f"[DOCX file: {filepath.name}] python-docx is not installed. "
            "Install with: pip install python-docx\n"
            "The agent cannot read this DOCX directly."
        )

    try:
        doc = Document(str(filepath))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        if not parts:
            # Try extracting from tables too
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            parts.append(cell.text.strip())
        return "\n".join(parts) if parts else "[DOCX file with no text content]"
    except Exception as e:
        return f"[DOCX parse error: {type(e).__name__}: {e}]"


def _trim(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n... (truncated at {max_chars} chars, total {len(content)} chars)"


# ── Self-register ────────────────────────────────────────────────────

registry.register(
    name="file_parse",
    toolset="multimodal",
    schema=FILE_PARSE_SCHEMA,
    handler=_handle_file_parse,
    is_async=False,
    description="Parse text from files: TXT, MD, JSON, CSV, PDF, DOCX, and more",
    emoji=FILE_EMOJI,
    max_result_size_chars=MAX_CHARS_LIMIT,
)
