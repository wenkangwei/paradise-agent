"""Web Search Skill — search the internet without an API key.

Self-registers as tool "web_search" in toolset "web".
Default backend: Bing (free, zero config). Also supports:
  - BRAVE_SEARCH_API_KEY: Brave Search free tier (2k queries/month)
  - SEARXNG_URL: self-hosted SearXNG

Normalized output format (same as hermes web_providers):
  {"success": true, "data": {"web": [{title, url, description, position}]}}

Usage in agent TOOL phase:
  tool_call: web_search(query="Python async tutorial", limit=5)
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse as urlparse
from typing import Any

import httpx

from paradise.tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────

BING_SEARCH_URL = "https://www.bing.com/search"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
TIMEOUT = 15.0
MAX_RESULTS_DEFAULT = 5
SEARCH_EMOJI = "\U0001f50d"


def _resolve_backend() -> str:
    """Determine which search backend to use."""
    if os.getenv("BRAVE_SEARCH_API_KEY", "").strip():
        return "brave"
    if os.getenv("SEARXNG_URL", "").strip():
        return "searxng"
    return "bing"


# ── Schema ──────────────────────────────────────────────────────────

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": (
        "Search the web for information. Returns up to 5 results by default "
        "with title, URL, and description for each result. "
        "Use this when you need to find current information, facts, or "
        "documentation that is not in your training data.\n\n"
        "Supported search operators (may work depending on backend):\n"
        "  site:example.com  — limit to a specific site\n"
        "  \"exact phrase\"   — search for exact phrase\n"
        "  -exclude          — exclude results with this term"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query. Supports operators like site:, \"phrase\", -term.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results (default 5, max 20)",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
            },
        },
        "required": ["query"],
    },
}


# ── Handler ─────────────────────────────────────────────────────────

def _handle_web_search(args: dict[str, Any]) -> str:
    """Execute a web search and return normalized results."""
    query = args.get("query", "")
    limit = min(max(int(args.get("limit", MAX_RESULTS_DEFAULT) or MAX_RESULTS_DEFAULT), 1), 20)

    if not query.strip():
        return tool_error("search query is empty")

    backend = _resolve_backend()
    logger.info("web_search: '%s' (limit=%d, backend=%s)", query, limit, backend)

    try:
        if backend == "brave":
            result = _search_brave(query, limit)
        elif backend == "searxng":
            result = _search_searxng(query, limit)
        else:
            result = _search_bing(query, limit)

        web_results = result.get("data", {}).get("web", [])
        result_count = len(web_results)
        logger.info("web_search: %d results for '%s'", result_count, query)

        # ── Auto-fetch content from top results ────────────────────
        auto_fetch = os.getenv("WEB_SEARCH_AUTO_FETCH", "1") not in ("0", "false", "no")
        fetch_count = min(int(os.getenv("WEB_SEARCH_FETCH_COUNT", "3")), 5)

        if auto_fetch and web_results:
            logger.info("web_search: auto-fetching content from top %d results", fetch_count)
            urls = [r["url"] for r in web_results[:fetch_count] if r.get("url")]
            if urls:
                from paradise.tools.web_fetch import fetch_and_extract
                for i, url in enumerate(urls):
                    try:
                        content, title, error = fetch_and_extract(url, max_chars=1500)
                        if not error and content:
                            web_results[i]["fetched_title"] = title
                            web_results[i]["fetched_content"] = content
                    except Exception as e:
                        logger.debug("Auto-fetch failed for %s: %s", url[:60], e)

        # ── Format as clean text (not JSON) for LLM consumption ────
        return _format_results(web_results)

    except Exception as e:
        logger.warning("web_search error: %s", e)
        return tool_error(f"Search failed: {type(e).__name__}: {e}")


# ── Bing (free, zero config, works in China) ────────────────────────

def _search_bing(query: str, limit: int) -> dict:
    """Search via Bing.com HTML scraping.

    No API key required. Parses Bing's search result page.
    Uses the global Bing endpoint which auto-redirects to regional (cn.bing.com).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        resp = httpx.get(
            BING_SEARCH_URL,
            params={"q": query, "count": min(limit, 20)},
            headers=headers,
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text

    except httpx.ConnectError:
        return {"success": False, "error": "Cannot reach Bing. Check your network connection."}
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": f"Bing returned HTTP {e.response.status_code}"}
    except Exception as e:
        return {"success": False, "error": f"Bing request failed: {e}"}

    results = _parse_bing_html(html, limit)
    return {"success": True, "data": {"web": results}}


def _parse_bing_html(html: str, limit: int) -> list[dict]:
    """Extract search results from Bing HTML.

    Bing search result structure (varies, so we try multiple strategies):
      <li class="b_algo">
        <h2><a href="...">Title</a></h2>
        <p>Description snippet...</p>
        <div class="b_caption">...</div>
      </li>
    """
    results = []

    # Strategy 1: find result blocks by class
    # Bing's main result container uses b_algo class
    block_pattern = re.compile(
        r'<li[^>]*class=["\'][^"\']*b_algo[^"\']*["\'][^>]*>(.*?)</li>',
        re.DOTALL | re.IGNORECASE,
    )
    blocks = block_pattern.findall(html)

    for block in blocks:
        if len(results) >= limit:
            break

        # Extract title + URL from <h2><a href="...">Title</a></h2>
        link_match = re.search(
            r'<a[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.+?)</a>',
            block, re.DOTALL | re.IGNORECASE,
        )

        if not link_match:
            # Try looser link pattern
            link_match = re.search(
                r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.+?)</a>',
                block, re.DOTALL | re.IGNORECASE,
            )

        if not link_match:
            continue

        url = link_match.group(1).strip()
        title = _clean_html(link_match.group(2))

        if not title or "bing.com" in url.lower():
            continue

        # Extract description from <p> or <div class="b_caption">
        desc_match = re.search(
            r'<(?:p|div)[^>]*class=["\'][^"\']*b_(?:caption|snippet|line|algoSlug)[^"\']*["\'][^>]*>(.*?)</(?:p|div)>',
            block, re.DOTALL | re.IGNORECASE,
        )
        if not desc_match:
            # Fallback: first <p> in the block
            desc_match = re.search(
                r'<p[^>]*>(.*?)</p>',
                block, re.DOTALL | re.IGNORECASE,
            )

        description = _clean_html(desc_match.group(1)) if desc_match else ""

        results.append({
            "title": title,
            "url": url,
            "description": description,
            "position": len(results) + 1,
        })

    # Strategy 2: fallback — broader link extraction
    if not results:
        link_pattern = re.compile(
            r'<a[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.+?)</a>',
            re.DOTALL | re.IGNORECASE,
        )
        for url, title in link_pattern.findall(html):
            if len(results) >= limit:
                break
            title = _clean_html(title)
            if (title and url
                    and "bing.com" not in url
                    and "microsoft.com" not in url
                    and len(title) > 2):
                results.append({
                    "title": title,
                    "url": url,
                    "description": "",
                    "position": len(results) + 1,
                })

    return results


# ── Brave Search (free tier, needs API key) ─────────────────────────

def _search_brave(query: str, limit: int) -> dict:
    """Search via Brave Search API (free tier, requires BRAVE_SEARCH_API_KEY)."""
    api_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return {"success": False, "error": "BRAVE_SEARCH_API_KEY is not set"}

    count = max(1, min(limit, 20))

    try:
        resp = httpx.get(
            BRAVE_ENDPOINT,
            params={"q": query, "count": count},
            headers={
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": f"Brave Search HTTP {e.response.status_code}"}
    except httpx.RequestError as e:
        return {"success": False, "error": f"Brave Search unreachable: {e}"}

    raw = (data.get("web") or {}).get("results", []) or []
    web_results = [
        {
            "title": str(r.get("title", "")),
            "url": str(r.get("url", "")),
            "description": str(r.get("description", "")),
            "position": i + 1,
        }
        for i, r in enumerate(raw[:limit])
    ]
    return {"success": True, "data": {"web": web_results}}


# ── SearXNG (self-hosted) ───────────────────────────────────────────

def _search_searxng(query: str, limit: int) -> dict:
    """Search via a self-hosted SearXNG instance."""
    base_url = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
    if not base_url:
        return {"success": False, "error": "SEARXNG_URL is not set"}

    try:
        resp = httpx.get(
            f"{base_url}/search",
            params={"q": query, "format": "json", "categories": "general"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": f"SearXNG HTTP {e.response.status_code}"}
    except httpx.RequestError as e:
        return {"success": False, "error": f"SearXNG unreachable: {e}"}

    raw = data.get("results", [])[:limit]
    web_results = [
        {
            "title": str(r.get("title", "")),
            "url": str(r.get("url", "")),
            "description": str(r.get("content", "") or r.get("snippet", "")),
            "position": i + 1,
        }
        for i, r in enumerate(raw)
    ]
    return {"success": True, "data": {"web": web_results}}


# ── Helpers ──────────────────────────────────────────────────────────

def _clean_html(text: str) -> str:
    """Strip HTML tags and entities from text."""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&#x27;", "'")
    text = text.replace("&nbsp;", " ").replace("&middot;", "·").replace("&ensp;", " ")
    # Remove duplicate URL fragments in title (e.g. "python.orghttps://...")
    text = re.sub(r'https?://\S+', '', text)
    # Remove numeric HTML entities (&#0183; etc.)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ── Result formatting ────────────────────────────────────────────

def _format_results(web_results: list[dict]) -> str:
    """Format search results as clean markdown-like text for LLM.

    Avoids raw JSON — returns human-readable text with optional
    fetched content included.
    """
    if not web_results:
        return tool_result("No search results found.")

    lines = [f"搜索结果 ({len(web_results)}条):\n"]
    for i, r in enumerate(web_results):
        title = r.get("title", "无标题")
        url = r.get("url", "")
        desc = r.get("description", "")[:200]

        lines.append(f"{i + 1}. **{title}**")
        if url:
            lines.append(f"   链接: {url}")
        if desc:
            lines.append(f"   摘要: {desc}")

        # Include fetched content if available
        fetched = r.get("fetched_content", "")
        if fetched:
            lines.append(f"   内容: {fetched[:800]}")

        lines.append("")

    return "\n".join(lines)


# ── Self-register ────────────────────────────────────────────────────

registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=_handle_web_search,
    is_async=False,
    description="Search the web via Bing (free, no API key needed). Also supports Brave Search and SearXNG.",
    emoji=SEARCH_EMOJI,
    max_result_size_chars=5000,
)
