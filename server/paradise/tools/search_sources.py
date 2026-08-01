"""Multi-source platform search via Playwright headless browser.

Searches JS-rendered platforms (Zhihu, CSDN, Juejin, Bilibili, Weibo)
by opening their search URLs in Chromium and extracting results.

A shared browser instance is kept warm between searches to avoid
cold-start overhead (~3s → ~0.5s per search).

Configure via SEARCH_PLATFORM_SOURCES env var (comma-separated):
  zhihu,csdn,juejin,bilibili,weibo

Default: zhihu,csdn,juejin (most useful for technical/lifestyle queries)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from playwright.async_api import async_playwright, Browser, BrowserContext

logger = logging.getLogger(__name__)

# ── Shared browser ─────────────────────────────────────────────────

# Garbage URL patterns to filter out
_GARBAGE_PATTERNS = [
    "beian.miit.gov.cn", "tsm.miit.gov.cn",  # ICP filing
    "/term/privacy", "/term/", "/tos",  # Legal pages
    "ir.zhihu.com", "investor",  # Investor relations
    "/project-square", "/tag/", "/user/",  # Navigation pages
    "login", "signup", "register", "passport",
]

_browser: Browser | None = None
_browser_lock = asyncio.Lock()
_browser_refcount = 0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def _get_browser() -> Browser:
    """Get or create the shared Playwright browser instance."""
    global _browser, _browser_refcount
    async with _browser_lock:
        _browser_refcount += 1
        if _browser is None:
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-gpu", "--disable-blink-features=AutomationControlled"]
            )
            logger.info("Playwright browser started")
        return _browser


async def _release_browser():
    """Decrement refcount, close browser when no more users."""
    global _browser, _browser_refcount
    async with _browser_lock:
        _browser_refcount -= 1
        if _browser_refcount <= 0 and _browser is not None:
            await _browser.close()
            _browser = None
            logger.info("Playwright browser closed")


# ── Platform definitions ──────────────────────────────────────────

class Platform:
    """Search configuration for a single platform."""
    def __init__(self, key: str, name: str, search_url: str, wait_selector: str,
                 extractor: callable):
        self.key = key
        self.name = name
        self.search_url = search_url
        self.wait_selector = wait_selector
        self.extractor = extractor


PLATFORMS: dict[str, Platform] = {
    "zhihu": Platform("zhihu", "知乎",
        "https://www.zhihu.com/search?type=content&q={query}",
        ".List-item, .SearchResultCard, [class*='SearchResult']",
        lambda el, _: _extract_zhihu(el)),
    "csdn": Platform("csdn", "CSDN",
        "https://so.csdn.net/so/search?q={query}&t=all",
        ".search-list-item, [class*='search'] a[href*='blog.csdn.net']",
        lambda el, _: _extract_csdn(el)),
    "juejin": Platform("juejin", "掘金",
        "https://juejin.cn/search?query={query}&type=0",
        ".search-result-item, [class*='result'] a[href*='/post/']",
        lambda el, _: _extract_juejin(el)),
    "bilibili": Platform("bilibili", "B站",
        "https://search.bilibili.com/all?keyword={query}",
        ".video-list-item, [class*='video']",
        lambda el, page: _extract_bilibili(el, page)),
    "weibo": Platform("weibo", "微博",
        "https://s.weibo.com/weibo?q={query}",
        ".card-wrap, [class*='card']",
        lambda el, _: _extract_weibo(el)),
}


def get_enabled_platforms() -> list[str]:
    configured = os.getenv("SEARCH_PLATFORM_SOURCES", "zhihu,csdn,juejin").strip()
    return [p.strip() for p in configured.split(",") if p.strip() in PLATFORMS]


# ── Search entry ──────────────────────────────────────────────────

async def search_platforms(query: str, limit: int = 5) -> list[dict]:
    """Search all enabled platforms using Playwright, return merged results."""
    platforms = get_enabled_platforms()
    if not platforms:
        return []

    browser = await _get_browser()
    try:
        tasks = [_search_one_platform(p, query, limit) for p in platforms]
        all_results = await asyncio.gather(*tasks)

        # Round-robin merge
        max_len = max(len(r) for r in all_results) if all_results else 0
        merged = []
        seen_urls = set()
        for i in range(max_len):
            for results in all_results:
                if i < len(results):
                    r = results[i]
                    url = r.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        merged.append(r)

        logger.info("Platform search: %d results from %d sources",
                     len(merged), len(platforms))
        return merged
    finally:
        await _release_browser()


async def _search_one_platform(platform_key: str, query: str, limit: int) -> list[dict]:
    """Search a single platform via Playwright."""
    platform = PLATFORMS.get(platform_key)
    if not platform:
        return []

    url = platform.search_url.format(query=query)
    browser = await _get_browser()
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
    )
    page = await context.new_page()

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        # Wait for search results to render
        await page.wait_for_selector(platform.wait_selector, timeout=10000)
        await asyncio.sleep(0.5)  # Extra time for lazy loading

        # Extract results from page
        results = []
        # Get all result elements using JS evaluation (more robust than selectors)
        items = await page.evaluate(f"""
            () => {{
                const links = Array.from(document.querySelectorAll(
                    'a[href]'
                )).filter(a => {{
                    const href = a.getAttribute('href') || '';
                    const text = (a.textContent || '').trim();
                    return text.length > 5 && text.length < 200
                        && !href.startsWith('#')
                        && !href.startsWith('javascript:')
                        && !href.includes('login')
                        && !href.includes('signup');
                }});
                return links.map(a => ({{
                    href: a.getAttribute('href'),
                    text: (a.textContent || '').trim().substring(0, 150),
                    parentText: (a.parentElement?.textContent || '').trim().substring(0, 300),
                }})).slice(0, {limit * 3});
            }}
        """)

        seen = set()
        for item in items:
            href = item.get("href", "")
            title = item.get("text", "")
            desc = item.get("parentText", "")

            if not href or not title:
                continue

            # Resolve relative URLs by platform
            url = _resolve_url(href, platform_key)
            if url in seen or len(title) < 5:
                continue
            # Filter garbage pages
            if any(p in url.lower() for p in _GARBAGE_PATTERNS):
                continue
            seen.add(url)

            results.append({
                "title": title,
                "url": url,
                "description": desc[:200] if desc != title else "",
                "source": platform.name,
            })
            if len(results) >= limit:
                break

        return results
    except Exception as e:
        logger.debug("Platform %s search failed: %s", platform_key, str(e)[:100])
        return []
    finally:
        await context.close()


# ── Platform-specific extractors (fallback for non-JS extraction) ──

def _extract_zhihu(el) -> list[dict]:
    return []


def _extract_csdn(el) -> list[dict]:
    return []


def _extract_juejin(el) -> list[dict]:
    return []


def _extract_bilibili(el, page) -> list[dict]:
    return []


def _extract_weibo(el) -> list[dict]:
    return []


# ── Helpers ────────────────────────────────────────────────────────

def _resolve_url(href: str, platform_key: str) -> str:
    """Resolve relative URLs to absolute based on platform."""
    bases = {
        "zhihu": "https://www.zhihu.com",
        "csdn": "https://blog.csdn.net",
        "juejin": "https://juejin.cn",
        "bilibili": "https://www.bilibili.com",
        "weibo": "https://weibo.com",
    }
    base = bases.get(platform_key, "")
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("/"):
        return f"{base}{href}"
    return f"{base}/{href}"
