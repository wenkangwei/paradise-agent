"""Web Fetch Skill — extract readable text from web page URLs.

Self-registers as tool "web_fetch" in toolset "web".
Fetches a URL, strips HTML/CSS/JS, extracts the main text content.
Used by web_search as an automatic post-processing step, and also
available as a standalone tool for the LLM to call directly.

Extraction strategy:
  1. httpx GET with browser User-Agent
  2. Remove <script>, <style>, <nav>, <footer>, <header> elements
  3. Extract text from remaining HTML
  4. Collapse whitespace, trim to max_chars

Usage in agent TOOL phase:
  tool_call: web_fetch(url="https://example.com", max_chars=3000)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

TIMEOUT = 15.0
MAX_CHARS_DEFAULT = 3000
MAX_CHARS_LIMIT = 10000
FETCH_EMOJI = "\U0001f310"

# HTML elements to strip before text extraction
_STRIP_TAGS = re.compile(
    r'</?(?:script|style|nav|footer|header|aside|noscript|iframe|svg|canvas'
    r'|form|input|button|select|textarea|label)\b[^>]*>.*?</(?:script|style|nav|footer|header|aside|noscript|iframe|svg|canvas)>',
    re.DOTALL | re.IGNORECASE,
)
_STRIP_COMMENTS = re.compile(r'<!--.*?-->', re.DOTALL)
_STRIP_TAGS_ALL = re.compile(r'<[^>]+>')
_STRIP_ENTITIES = re.compile(r'&[a-z]+;|&#\d+;|&#x[0-9a-f]+;', re.IGNORECASE)
_STRIP_WHITESPACE = re.compile(r'\s+')
_STRIP_URLS_IN_TEXT = re.compile(r'https?://\S+')


WEB_FETCH_SCHEMA = {
    "name": "web_fetch",
    "description": (
        "Fetch and extract readable text content from a web page URL. "
        "Strips HTML, CSS, JavaScript and navigation elements. "
        "Returns the main text content, truncated to max_chars (default 3000). "
        "Use this to get the full content of a page found by web_search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch content from",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to return (default 3000, max 10000)",
            },
        },
        "required": ["url"],
    },
}


# ── Public API ──────────────────────────────────────────────────────

def _handle_web_fetch(args: dict[str, Any]) -> str:
    """Fetch URL content and return extracted text."""
    url = args.get("url", "")
    max_chars = min(
        max(int(args.get("max_chars", MAX_CHARS_DEFAULT) or MAX_CHARS_DEFAULT), 100),
        MAX_CHARS_LIMIT,
    )

    if not url.strip():
        return tool_error("url is required")

    content, title, error = fetch_and_extract(url, max_chars)
    if error:
        return tool_error(error)

    return tool_result({
        "url": url,
        "title": title,
        "content": content,
        "length": len(content),
    })


def fetch_and_extract(url: str, max_chars: int = 3000) -> tuple[str, str, str | None]:
    """Fetch URL and extract readable text. Returns (content, title, error).

    Args:
        url: The URL to fetch
        max_chars: Maximum characters to return

    Returns:
        (content, title, error) — error is None on success
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        resp = httpx.get(
            url,
            headers=headers,
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.ConnectError:
        return "", "", f"Cannot connect to {url}"
    except httpx.HTTPStatusError as e:
        return "", "", f"HTTP {e.response.status_code} from {url}"
    except httpx.TimeoutException:
        return "", "", f"Timeout fetching {url}"
    except Exception as e:
        return "", "", f"Fetch error: {e}"

    html = resp.text
    title = _extract_title(html)
    text = _extract_text(html)
    text = _trim(text, max_chars)

    if not text.strip():
        return "", title or url, "No readable text content found on this page"

    logger.info("web_fetch: %s → %d chars", url[:60], len(text))
    return text, title or url, None


def fetch_urls_batch(urls: list[str], max_chars_per_url: int = 1500) -> list[dict]:
    """Fetch multiple URLs and return extracted content.

    Used by web_search as automatic post-processing.
    Returns list of {url, title, content, error}.
    """
    results = []
    for url in urls[:5]:  # Max 5 URLs to avoid overwhelming
        try:
            content, title, error = fetch_and_extract(url, max_chars_per_url)
            results.append({
                "url": url,
                "title": title,
                "content": content if not error else "",
                "error": error,
            })
        except Exception as e:
            results.append({
                "url": url,
                "title": "",
                "content": "",
                "error": str(e),
            })
    return results


# ── HTML extraction helpers ─────────────────────────────────────────

def _extract_title(html: str) -> str:
    """Extract page title from HTML."""
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if m:
        title = _clean_text(m.group(1))[:200]
        return title
    # Try og:title meta
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return _clean_text(m.group(1))[:200]
    return ""


def _extract_text(html: str) -> str:
    """Extract readable text from HTML.

    Strategy:
    1. Remove script/style/nav/footer/header elements
    2. Remove HTML comments
    3. Remove all remaining tags
    4. Decode HTML entities
    5. Collapse whitespace
    """
    # Remove scripts, styles, nav, footer, header
    text = _STRIP_TAGS.sub(' ', html)
    # Remove comments
    text = _STRIP_COMMENTS.sub(' ', text)
    # Remove all remaining HTML tags
    text = _STRIP_TAGS_ALL.sub(' ', text)
    # Collapse whitespace + clean
    text = _clean_text(text)
    return text


def _clean_text(text: str) -> str:
    """Clean extracted text."""
    # Remove URLs (clutter in extracted text)
    text = _STRIP_URLS_IN_TEXT.sub(' ', text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    # Remove numeric entities
    text = _STRIP_ENTITIES.sub(' ', text)
    # Collapse whitespace
    text = _STRIP_WHITESPACE.sub(' ', text)
    return text.strip()


def _trim(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


# ── Self-register ────────────────────────────────────────────────────

registry.register(
    name="web_fetch",
    toolset="web",
    schema=WEB_FETCH_SCHEMA,
    handler=_handle_web_fetch,
    is_async=False,
    description="Fetch and extract text content from a web page URL",
    emoji=FETCH_EMOJI,
    max_result_size_chars=MAX_CHARS_LIMIT,
)
