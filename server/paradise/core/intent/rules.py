"""Rule-based intent strategy (L1 — fastest tier).

Pure-Python regex + keyword matching. Latency < 1ms.

Rules are declarative: a list of (pattern, intent, confidence) tuples.
First match wins, so order matters — put the most specific patterns first.

This is the ONLY tier that's guaranteed to work without any external service.
Even when Qdrant / ollama are down, L1 keeps the agent functional for the
common cases (greetings, obvious tool calls).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from paradise.core.intent.base import Intent, IntentResult, IntentStrategy

logger = logging.getLogger(__name__)


# (pattern, intent, confidence)
# Patterns are matched with re.IGNORECASE | re.UNICODE.
#
# IMPORTANT: `\b` in Python 3 regex (with re.UNICODE) treats CJK chars as `\w`.
# So `\b` does NOT fire between two CJK word chars (e.g. "你好啊" has no word
# boundary anywhere). For CJK anchors, use `^` (start) or explicit lookarounds
# like `(?<![A-Za-z0-9])` / `(?![A-Za-z0-9])` to fence ASCII keywords.
DEFAULT_RULES: list[tuple[str, Intent, float]] = [
    # ── Chitchat: greetings / farewells / fillers ──────────────────────
    # CJK patterns: anchored at start, no \b needed (word runs into the rest).
    (r"^(你好|您好|嗨|哈喽|嘿)",                                      Intent.CHITCHAT,     0.95),
    (r"^(早上?好|下午好|晚上?好|早安|晚安)",                          Intent.CHITCHAT,     0.95),
    (r"^(谢谢|感谢|多谢)",                                            Intent.CHITCHAT,     0.95),
    (r"^(再见|拜拜|在吗|在不在|有空吗)",                              Intent.CHITCHAT,     0.9),
    # ASCII greetings: \b is safe here.
    (r"^(hi|hello|hey|thx|thanks|thank\s+you|bye\b|byebye|good\s+(morning|evening))\b", Intent.CHITCHAT, 0.95),

    # ── Tool: search ───────────────────────────────────────────────────
    (r"(搜索|搜一下|查一下|帮我查)",                                  Intent.TOOL_SEARCH,  0.9),
    (r"(google一下|bing一下|search\s+for)",                           Intent.TOOL_SEARCH,  0.9),
    (r"(最新.*新闻|热点|trending)",                                   Intent.TOOL_SEARCH,  0.8),

    # ── Tool: explicit command / file operations ─────────────────────
    (r"(执行|运行|调用|使用).{0,4}(命令|bash|shell|脚本|工具)",       Intent.TOOL_SEARCH,  0.88),
    (r"(查看|列出|显示|读取|打开).{0,4}(目录|文件|文件夹|路径|列表)", Intent.TOOL_SEARCH,  0.85),
    (r"\b(bash|shell|terminal|cmd|ls|cat|grep|find|python|node)\b", Intent.TOOL_SEARCH,  0.8),
    (r"(删除|创建|修改|移动|复制|下载|上传|安装)",                    Intent.TOOL_SEARCH,  0.8),

    # ── Tool: weather ──────────────────────────────────────────────────
    (r"(天气|气温|下雨吗|温度多少)",                                  Intent.TOOL_WEATHER, 0.9),
    (r"\b(weather|forecast)\b",                                       Intent.TOOL_WEATHER, 0.9),

    # ── Tool: reminder ─────────────────────────────────────────────────
    (r"(提醒我|定时|设置闹钟)",                                       Intent.TOOL_REMINDER, 0.85),
    (r"(schedule\s+reminder|set\s+timer)",                            Intent.TOOL_REMINDER, 0.85),

    # ── Tool: translation ──────────────────────────────────────────────
    (r"(翻译成|翻译一下|^.+翻译$)",                                   Intent.TOOL_TRANSLATE, 0.9),
    (r"translate\s+\w+\s+to",                                         Intent.TOOL_TRANSLATE, 0.9),

    # ── Creative: writing / generation ─────────────────────────────────
    (r"(帮我写|写一篇|生成|创作|编一个)",                             Intent.CREATIVE,     0.85),
    (r"\b(write\s+(me\s+)?a|compose)\b",                              Intent.CREATIVE,     0.85),

    # ── Complex: multi-step / delegation candidate ─────────────────────
    (r"(帮我分析|对比.*和|总结.*报告|多步|调研)",                      Intent.TASK_COMPLEX, 0.75),
    (r"step.by.step",                                                 Intent.TASK_COMPLEX, 0.75),

    # ── Unsafe: jailbreak attempts ─────────────────────────────────────
    (r"(越狱模式|扮演.*不受限制)",                                    Intent.UNSAFE,       0.85),
    (r"ignore.*previous.*instructions",                               Intent.UNSAFE,       0.85),
    (r"\bDAN\s+mode\b",                                               Intent.UNSAFE,       0.85),
]


class RuleStrategy(IntentStrategy):
    """L1 — pure-Python regex classifier.

    Compiles all patterns at __init__ time (warmup) for O(1)-ish matching.
    Thread-safe (read-only after init).
    """

    name = "rule"

    def __init__(
        self,
        rules: list[tuple[str, Intent, float]] | None = None,
    ) -> None:
        self._rules: list[tuple[re.Pattern, Intent, float]] = []
        for pattern, intent, conf in (rules or DEFAULT_RULES):
            try:
                self._rules.append((re.compile(pattern, re.IGNORECASE | re.UNICODE), intent, conf))
            except re.error as exc:
                logger.error("Invalid intent regex %r: %s", pattern, exc)
        logger.info("RuleStrategy loaded %d compiled patterns", len(self._rules))

    async def classify(self, message: str) -> Optional[IntentResult]:
        if not message or not message.strip():
            return None
        t0 = time.perf_counter()
        for regex, intent, conf in self._rules:
            if regex.search(message):
                latency_ms = (time.perf_counter() - t0) * 1000
                return IntentResult(
                    intent=intent,
                    confidence=conf,
                    source=self.name,
                    latency_ms=latency_ms,
                    meta={"matched_pattern": regex.pattern},
                )
        return None

    async def warmup(self) -> None:
        # Patterns compiled in __init__; nothing to do here, but we expose
        # the hook so future strategies can pre-load models uniformly.
        return None
