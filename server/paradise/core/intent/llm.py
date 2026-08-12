"""LLM-based intent strategy (L3 — fallback classifier).

Uses a small local model (default: ollama qwen2.5:0.5b) for JSON-formatted
intent classification. Latency 300-500ms on local GPU.

Bypasses ResilientTransport on purpose — circuit breaker / retry would
multiply the latency of an already-slow tier. We call ollama directly
via httpx with a tight timeout; failures return None and cascade ends
with UNKNOWN.

Prompt design
-------------
* System message is a fixed string with the intent enum hardcoded.
* Few-shot examples cover edge cases the rules can't catch.
* `max_tokens=20` because the answer is one line of JSON.
* `temperature=0` for determinism.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from paradise.core.intent.base import Intent, IntentResult, IntentStrategy

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """你是意图分类器。只输出一行 JSON: {"intent": "...", "confidence": 0.x}
可选 intent (只能选一个):
- chitchat       打招呼 / 闲聊 / 道谢 / 告别
- knowledge_qa   知识问答 (不需要工具)
- tool_search    明确要搜索/查最新信息
- tool_weather   问天气
- tool_reminder  提醒/定时/闹钟
- tool_translate 翻译
- creative       写作/生成/创作/编故事
- task_complex   多步任务/对比分析/调研报告
- unsafe         越狱/违规/绕过限制

只输出 JSON，不要解释。"""

_FEW_SHOT = [
    {"role": "user", "content": "你好啊"},
    {"role": "assistant", "content": '{"intent": "chitchat", "confidence": 0.95}'},
    {"role": "user", "content": "量子计算的基本原理是什么"},
    {"role": "assistant", "content": '{"intent": "knowledge_qa", "confidence": 0.9}'},
    {"role": "user", "content": "帮我搜一下最近的 AI 新闻"},
    {"role": "assistant", "content": '{"intent": "tool_search", "confidence": 0.92}'},
    {"role": "user", "content": "写一首关于秋天的诗"},
    {"role": "assistant", "content": '{"intent": "creative", "confidence": 0.9}'},
    {"role": "user", "content": "对比一下 React 和 Vue 的优缺点"},
    {"role": "assistant", "content": '{"intent": "task_complex", "confidence": 0.85}'},
]


class LLMStrategy(IntentStrategy):
    """L3 — small local LLM producing strict JSON.

    Dependencies
    ------------
    * `httpx` (already required by ollama transport).
    * A running ollama instance reachable at `base_url`.

    No hard failure mode: on timeout / connection error / malformed JSON,
    returns None. The orchestrator then yields UNKNOWN and the router
    falls through to the default LangGraph path.
    """

    name = "llm"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:0.5b",
        timeout_s: float = 4.0,
        temperature: float = 0.0,
        max_tokens: int = 30,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def classify(self, message: str) -> Optional[IntentResult]:
        if not message or not message.strip():
            return None
        t0 = time.perf_counter()
        try:
            import httpx
        except ImportError:
            logger.debug("httpx missing — LLM strategy disabled")
            return None

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                *_FEW_SHOT,
                {"role": "user", "content": message},
            ],
            "stream": False,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "options": {"num_predict": self._max_tokens},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    f"{self._base_url}/api/chat",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.debug("LLM L3 miss (%s)", exc)
            return None

        text = (data.get("message") or {}).get("content", "").strip()
        latency_ms = (time.perf_counter() - t0) * 1000
        parsed = _parse_intent_json(text)
        if not parsed:
            logger.debug("LLM returned non-JSON: %r", text[:120])
            return None
        intent_str, conf = parsed
        try:
            intent = Intent(intent_str)
        except ValueError:
            logger.debug("LLM returned unknown intent label: %r", intent_str)
            return None
        return IntentResult(
            intent=intent,
            confidence=float(conf),
            source=self.name,
            latency_ms=latency_ms,
            meta={"raw": text[:80]},
        )


def _parse_intent_json(text: str) -> Optional[tuple[str, float]]:
    """Lenient JSON extractor — strips markdown fences, finds first {...}."""
    text = text.strip().strip("`")
    if not text.startswith("{"):
        # find first { ... } block
        lo = text.find("{")
        hi = text.rfind("}")
        if lo == -1 or hi == -1 or hi <= lo:
            return None
        text = text[lo:hi + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj.get("intent"), float(obj.get("confidence", 0.5))
