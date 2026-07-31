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
MAX_RESULTS_DEFAULT = int(os.getenv("WEB_SEARCH_DEFAULT_LIMIT", "10"))
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

async def _handle_web_search(args: dict[str, Any]) -> str:
    """Execute a web search with RAG pipeline: search → fetch → summarize → format."""
    query = args.get("query", "")
    context = args.get("_context", "")  # from agent: time, user profile, history context
    limit = min(max(int(args.get("limit", MAX_RESULTS_DEFAULT) or MAX_RESULTS_DEFAULT), 1), 20)

    if not query.strip():
        return tool_error("search query is empty")

    backend = _resolve_backend()
    logger.info("web_search: '%s' (limit=%d, backend=%s)", query, limit, backend)

    try:
        # ── Query rewriting ─────────────────────────────────────
        search_queries = [query]
        rewritten = await _rewrite_query(query, context)
        if rewritten:
            for rq in rewritten:
                if rq and rq != query:
                    search_queries.append(rq)
        logger.info("web_search queries: %s", search_queries)

        # ── Multi-source search ─────────────────────────────────
        # Run all available backends in parallel
        import asyncio as _asyncio
        all_results = []
        seen_urls = set()

        async def _search_one(q: str, src: str):
            """Search one query on one backend."""
            results = []
            try:
                if src == "bing":
                    r = _search_bing(q, limit)
                elif src == "searxng":
                    r = _search_searxng(q, limit)
                elif src == "brave":
                    r = _search_brave(q, limit)
                else:
                    return []
                for item in r.get("data", {}).get("web", []):
                    item["_source"] = src
                    results.append(item)
            except Exception as e:
                logger.debug("Search %s on %s failed: %s", q[:30], src, e)
            return results

        # Determine which backends to use
        backends = ["bing"]  # always use Bing
        if os.getenv("BRAVE_SEARCH_API_KEY", "").strip():
            backends.append("brave")
        if os.getenv("SEARXNG_URL", "").strip():
            backends.append("searxng")

        # Run all backends × all queries in parallel
        tasks = [_search_one(q, src) for q in search_queries for src in backends]
        batch_results = await _asyncio.gather(*tasks)

        # Merge: interleave results from different sources for diversity
        sources = {src: [] for src in backends}
        for i, results in enumerate(batch_results):
            src = backends[i % len(backends)]
            sources[src].extend(results)

        # Round-robin merge: take 1 from each source
        max_per_source = max(len(v) for v in sources.values()) if sources else 0
        merged = []
        for i in range(max_per_source):
            for src in backends:
                src_results = sources.get(src, [])
                if i < len(src_results):
                    r = src_results[i]
                    url = r.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        merged.append(r)
        all_results = merged[:limit * 2]  # fetch more for reranking
        logger.info("web_search: %d results from %d sources", len(all_results), len(backends))

        # ── LLM Re-ranking: score by relevance to query ─────────────
        if len(all_results) > 3 and os.getenv("WEB_SEARCH_RERANK", "1") not in ("0", "false", "no"):
            all_results = await _rerank_results(all_results, query)
        web_results = all_results[:limit]
        result_count = len(web_results)
        logger.info("web_search: %d results for '%s' after rerank", result_count, query)

        # ── RAG Pipeline: Search → Fetch → Summarize → Format ──────
        auto_fetch = os.getenv("WEB_SEARCH_AUTO_FETCH", "1") not in ("0", "false", "no")
        auto_summarize = os.getenv("WEB_SEARCH_AUTO_SUMMARIZE", "1") not in ("0", "false", "no")
        fetch_count = min(int(os.getenv("WEB_SEARCH_FETCH_COUNT", "3")), 5)

        if auto_fetch and web_results:
            urls = [r["url"] for r in web_results[:fetch_count] if r.get("url")]
            if urls:
                from paradise.tools.web_fetch import fetch_and_extract
                for i, url in enumerate(urls):
                    try:
                        content, title, error = fetch_and_extract(url, max_chars=5000)
                        if not error and content:
                            web_results[i]["fetched_title"] = title
                            web_results[i]["fetched_content"] = content
                    except Exception as e:
                        logger.debug("Auto-fetch failed for %s: %s", url[:60], e)

                # ── Summarize each fetched page via small LLM ──────
                if auto_summarize:
                    logger.info("web_search: summarizing %d fetched pages", fetch_count)
                    fetched = [r for r in web_results[:fetch_count] if r.get("fetched_content")]
                    if fetched:
                        summaries = await _summarize_pages(
                            [(r.get("fetched_content", ""), r.get("fetched_title", ""),
                              r.get("url", ""), query)
                             for r in fetched]
                        )
                        for i, summary in enumerate(summaries):
                            if summary:
                                fetched[i]["fetched_content"] = summary

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


# ── Query Rewriting ─────────────────────────────────────────────

async def _rewrite_query(query: str, context: str = "") -> list[str]:
    """Ask a fast LLM to rewrite/expand the search query for better results.

    Uses available context (time, user profile, history) to optimize queries.
    Returns up to 2 rewritten variations (not including the original).
    """
    if os.getenv("WEB_SEARCH_REWRITE", "1") in ("0", "false", "no"):
        return []

    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("QUERY_REWRITE_MODEL", "qwen2.5:3b")

    context_block = ""
    if context:
        context_block = f"\nContext:\n{context}\n"

    prompt = (
        "You are a search query optimizer. Given a user's question, rewrite it "
        "into 1-2 alternative search queries that will find better results. "
        "Consider: the current time (for freshness), user location (for local results), "
        "user profile (for personalization), and conversation context.\n"
        f"{context_block}"
        f"User query: {query}\n\n"
        "Output each rewritten query on a separate line. No numbering, no prefixes. "
        "Make queries concise and search-engine friendly (keywords work better than "
        "full sentences). Output ONLY the queries, nothing else."
    )

    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
        ) as client:
            resp = await client.post(
                f"{ollama_base.rstrip('/')}/v1/chat/completions",
                json={
                    "model": model,
                    "stream": False,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 100,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            lines = [l.strip() for l in content.strip().split("\n") if l.strip()]
            return lines[:2]
    except Exception as e:
        logger.debug("Query rewrite failed: %s", e)
        return []


# ── Re-ranking ──────────────────────────────────────────────────

async def _rerank_results(results: list[dict], query: str) -> list[dict]:
    """Use a fast LLM to score search results by relevance to the query.

    Returns results sorted by relevance (highest first).
    """
    if not results:
        return results

    # Build prompt with titles + snippets
    items_text = []
    for i, r in enumerate(results):
        title = r.get("title", "")[:100]
        snippet = r.get("description", "")[:200]
        items_text.append(f"{i}: {title} | {snippet}")

    prompt = (
        "Rate each search result's relevance to the query on a scale of 0-10. "
        "Output ONLY the numbers, one per line, in the same order.\n\n"
        f"Query: {query}\n\n" + "\n".join(items_text) + "\n\nScores:"
    )

    try:
        import httpx as _httpx
        ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        async with _httpx.AsyncClient(
            timeout=_httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
        ) as client:
            resp = await client.post(
                f"{ollama_base.rstrip('/')}/v1/chat/completions",
                json={
                    "model": os.getenv("QUERY_REWRITE_MODEL", "qwen2.5:3b"),
                    "stream": False,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1, "max_tokens": len(results) * 5,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Parse scores
        scores = []
        import re as _re
        for line in content.strip().split("\n"):
            m = _re.search(r'(\d+)', line)
            if m:
                scores.append(int(m.group(1)))

        if scores and len(scores) >= len(results):
            # Sort by score descending
            scored = list(zip(results, scores))
            scored.sort(key=lambda x: x[1], reverse=True)
            logger.info("Reranked %d results, top score: %d", len(scored), scored[0][1] if scored else 0)
            return [r for r, _ in scored]

    except Exception as e:
        logger.debug("Reranking failed: %s", e)
    return results


# ── RAG Summarization ────────────────────────────────────────────

_SUMMARIZE_PROMPT = """You are a search result summarizer. Extract ONLY the information relevant to the user's search query from the web page content below.

CRITICAL RULES:
- Output 3-5 bullet points MAXIMUM
- Each bullet: ONE specific fact, number, or insight directly answering the query
- Strip ALL navigation, ads, cookie notices, login prompts, sidebars
- If the content is just a menu/header/footer with no real information, output "No relevant content found on this page"
- Use plain text, NO markdown formatting (no ##, no **, no code blocks)
- Be EXTREMELY concise — total output under 200 words
- Preserve original numbers, dates, version strings exactly

Output format (plain text, no markdown):
Page: [title]
- fact 1
- fact 2
- fact 3"""


async def _summarize_pages(pages: list[tuple[str, str, str, str]]) -> list[str]:
    """Summarize multiple fetched pages via a small/fast LLM.

    Args:
        pages: list of (content, title, url, query) tuples

    Returns:
        list of summary strings (same order as input)
    """
    import asyncio

    async def _summarize_one(content: str, title: str, url: str, query: str) -> str:
        """Summarize a single page."""
        if len(content) < 200:
            return content  # Too short, use as-is

        user_prompt = (
            f"Search query: {query}\n"
            f"Page URL: {url}\n\n"
            f"CONTENT:\n{content[:4000]}"
        )

        try:
            result = await _call_summarizer_llm(_SUMMARIZE_PROMPT, user_prompt, title)
            return result if result else _fallback_summary(content, title)
        except Exception:
            return _fallback_summary(content, title)

    # Run all summaries in parallel
    tasks = [_summarize_one(c, t, u, q) for c, t, u, q in pages]
    return list(await asyncio.gather(*tasks))


async def _call_summarizer_llm(system: str, user: str, title: str) -> str | None:
    """Call Ollama with a small/fast model for summarization."""
    import httpx

    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("SUMMARY_MODEL", "qwen2.5:3b")

    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "max_tokens": 600,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        ) as client:
            resp = await client.post(
                f"{ollama_base.rstrip('/')}/v1/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip() if content else None
    except Exception as e:
        logger.warning("Summarizer LLM failed for '%s': %s", title, e)
        return None


def _fallback_summary(content: str, title: str) -> str:
    """Fallback: return first 300 chars as summary."""
    cleaned = _clean_text(content)
    if len(cleaned) <= 300:
        return cleaned
    return cleaned[:300] + "..."


# ── Result formatting ────────────────────────────────────────────

def _format_results(web_results: list[dict]) -> str:
    """Format search results as clean brief summaries for LLM consumption.

    Each result gets a compact one-line entry. Summarized content
    (already processed by the RAG pipeline) is included cleanly.
    """
    if not web_results:
        return "No search results found."

    lines = [f"Web search results ({len(web_results)} items):\n"]
    for i, r in enumerate(web_results):
        title = r.get("title", "").strip()
        url = r.get("url", "").strip()
        desc = r.get("description", "").strip()[:150]
        fetched = r.get("fetched_content", "").strip()

        # Compact entry header
        lines.append(f"### {i + 1}. {title}")
        lines.append(f"URL: {url}")

        if fetched:
            # Already summarized by RAG pipeline — display cleanly
            lines.append(fetched)
        elif desc:
            lines.append(f"*{desc}*")

        lines.append("")  # blank line between results

    return "\n".join(lines)


# ── Self-register ────────────────────────────────────────────────────

registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=_handle_web_search,
    is_async=True,
    description="Search the web with RAG pipeline: search → fetch → summarize → format",
    emoji=SEARCH_EMOJI,
    max_result_size_chars=5000,
)
