"""Web Fetch Skill — extract readable text from web page URLs.

Self-registers as tool "web_fetch" in toolset "web".
Uses trafilatura (professional web extraction) as primary, with
HTML regex fallback. Handles WeChat articles, common Chinese sites.

Extraction strategy (priority order):
  1. trafilatura — handles 90% of pages, strips nav/ads/sidebar
  2. Site-specific: WeChat (js_content div), Jimeng (JSON data)
  3. Regex fallback — remove scripts/styles, extract visible text
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

TIMEOUT = 20.0
MAX_CHARS_DEFAULT = 3000
MAX_CHARS_LIMIT = 10000
FETCH_EMOJI = "\U0001f310"

_STRIP_COMMENTS = re.compile(r'<!--.*?-->', re.DOTALL)
_STRIP_TAGS_ALL = re.compile(r'<[^>]+>')
_STRIP_ENTITIES = re.compile(r'&[a-z]+;|&#\d+;|&#x[0-9a-f]+;', re.IGNORECASE)
_STRIP_WHITESPACE = re.compile(r'\s+')

# Unscrapable URL patterns (login, auth, non-HTML files)
_UNSUPPORTED_PATTERNS = [
    (re.compile(r"login\.|passport\.|auth\.|sso\.", re.I), "登录/认证页面"),
    (re.compile(r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|gz|tar|mp4|mp3)$", re.I), "非HTML文件"),
]


WEB_FETCH_SCHEMA = {
    "name": "web_fetch",
    "description": (
        "Fetch and extract readable text content from a web page URL. "
        "Uses professional extraction (trafilatura) for clean article text. "
        "Handles WeChat articles, news sites, blogs, documentation. "
        "Returns extracted text, truncated to max_chars (default 3000)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch content from"},
            "max_chars": {"type": "integer", "description": "Max chars to return (default 3000, max 10000)"},
        },
        "required": ["url"],
    },
}


def _handle_web_fetch(args: dict[str, Any]) -> str:
    url = args.get("url", "")
    max_chars = min(max(int(args.get("max_chars", MAX_CHARS_DEFAULT) or MAX_CHARS_DEFAULT), 100), MAX_CHARS_LIMIT)
    if not url.strip():
        return tool_error("url is required")

    # Check unsupported URLs
    for pattern, reason in _UNSUPPORTED_PATTERNS:
        if pattern.search(url):
            return tool_error(f"{reason}，无法抓取: {url}")

    content, title, error = fetch_and_extract(url, max_chars)
    if error:
        return tool_error(error)
    return tool_result({"url": url, "title": title, "content": content, "length": len(content)})


def fetch_and_extract(url: str, max_chars: int = 3000) -> tuple[str, str, str | None]:
    """Fetch URL and extract readable text. Returns (content, title, error)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=TIMEOUT, follow_redirects=True)
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
    title, content = _extract_article(html, url)
    content = _trim(content, max_chars)

    if not content.strip():
        return "", title or url, "No readable content found"

    logger.info("web_fetch: %s → %d chars (title: %s)", url[:60], len(content), title[:50])
    return content, title or url, None


def fetch_urls_batch(urls: list[str], max_chars_per_url: int = 1500) -> list[dict]:
    """Fetch multiple URLs and return extracted content (used by web_search)."""
    results = []
    for url in urls[:5]:
        try:
            content, title, error = fetch_and_extract(url, max_chars_per_url)
            results.append({"url": url, "title": title, "content": content if not error else "", "error": error})
        except Exception as e:
            results.append({"url": url, "title": "", "content": "", "error": str(e)})
    return results


# ── Article extraction ──────────────────────────────────────────

def _extract_article(html: str, url: str) -> tuple[str, str]:
    """Extract title + content from HTML. Returns (title, content)."""
    # Priority 1: trafilatura (professional web extraction library)
    title, content = _extract_trafilatura(html)
    if content and len(content) > 100:
        return title, content

    # Priority 2: Site-specific extractors
    if "mp.weixin.qq.com" in url:
        title, content = _extract_wechat(html)
        if content and len(content) > 50:
            return title, content

    # Priority 3: Regex fallback (smart content area detection)
    title, content = _extract_regex(html)
    return title, content


def _extract_trafilatura(html: str) -> tuple[str, str]:
    """Use trafilatura for content extraction."""
    try:
        import trafilatura
        # Extract as markdown for clean formatting
        content = trafilatura.extract(html, output_format="markdown",
                                       include_comments=False, include_tables=True,
                                       with_metadata=True)
        if not content:
            content = trafilatura.extract(html, include_comments=False) or ""

        # Extract metadata
        title = ""
        meta_json = trafilatura.extract(html, output_format="json", with_metadata=True)
        if meta_json:
            import json
            try:
                meta = json.loads(meta_json)
                title = meta.get("title", "") or ""
            except Exception:
                pass

        # Strip YAML frontmatter from markdown output (trafilatura metadata)
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                content = content[end + 3:].strip()
        content = _clean_text(content)
        if not title:
            title = _extract_title_html(html)
        return title, content
    except ImportError:
        logger.debug("trafilatura not installed, using regex fallback")
        return "", ""
    except Exception as e:
        logger.debug("trafilatura extraction failed: %s", e)
        return "", ""


def _extract_wechat(html: str) -> tuple[str, str]:
    """Extract WeChat article content from js_content div."""
    title = ""
    content = ""

    title_match = re.search(r'var\s+msg_title\s*=\s*"(.*?)"', html)
    if title_match:
        title = title_match.group(1).strip()
    if not title:
        title_match = re.search(r'<h1[^>]*class="rich_media_title[^"]*"[^>]*>(.*?)</h1>', html, re.DOTALL)
        if title_match:
            title = _clean_html(title_match.group(1))

    content_match = re.search(r'id="js_content"[^>]*>(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL)
    if content_match:
        content = _clean_html(content_match.group(1))
    return title, content


def _extract_regex(html: str) -> tuple[str, str]:
    """Regex fallback: find main content, strip boilerplate, extract text."""
    title = _extract_title_html(html)
    # Try to find main content area
    main_html = html
    for pattern_name in ['article', 'main', 'content', 'post', 'entry', 'body']:
        m = re.search(
            rf'<(?:article|main|div)[^>]*(?:class|id)=["\'][^"\']*{pattern_name}[^"\']*["\'][^>]*>(.*?)</(?:article|main|div)>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if m and len(m.group(1)) > 200:
            main_html = m.group(1)
            break

    text = _STRIP_COMMENTS.sub(' ', main_html)
    text = re.sub(r'</?(?:script|style|nav|footer|header|aside|noscript|iframe|svg)\b[^>]*>.*?</(?:script|style|nav|footer|header|aside|noscript|iframe|svg)>',
                  ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = _STRIP_TAGS_ALL.sub(' ', text)
    text = _clean_text(text)

    # Filter boilerplate paragraphs
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    good = [p for p in paragraphs if _content_score(p) > 0.3]
    if len(good) >= 2:
        text = '\n'.join(good)
    return title, text


# ── Helpers ─────────────────────────────────────────────────────

def _extract_title_html(html: str) -> str:
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if m:
        return _clean_text(m.group(1))[:200]
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return _clean_text(m.group(1))[:200]
    return ""


def _clean_html(html_text: str) -> str:
    text = re.sub(r'<br\s*/?>', '\n', html_text, flags=re.IGNORECASE)
    text = re.sub(r'<p[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = _STRIP_TAGS_ALL.sub(' ', text)
    import html as _html
    text = _html.unescape(text)
    return _clean_text(text)


def _clean_text(text: str) -> str:
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    text = _STRIP_ENTITIES.sub(' ', text)
    text = _STRIP_WHITESPACE.sub(' ', text)
    return text.strip()


def _content_score(text: str) -> float:
    """Score paragraph quality. Higher = real content, lower = boilerplate."""
    if len(text) < 20:
        return 0.0
    boilerplate = ['cookie', 'privacy', 'copyright', '©', 'subscribe', 'newsletter',
                   '首页', '登录', '注册', '导航', '上一篇', '下一篇', '分享到', '点赞',
                   'function(', 'var ', 'const ', '=>']
    text_lower = text.lower()
    penalty = sum(1 for bp in boilerplate if bp in text_lower)
    base = min(len(text) / 200.0, 1.0)
    return max(0.0, base - penalty * 0.15)


def _trim(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


# ── Self-register ─────────────────────────────────────────────────

registry.register(
    name="web_fetch", toolset="web", schema=WEB_FETCH_SCHEMA,
    handler=_handle_web_fetch, is_async=False,
    description="Fetch and extract text from web pages using trafilatura + regex",
    emoji=FETCH_EMOJI, max_result_size_chars=MAX_CHARS_LIMIT,
)
