"""
LLM Augmenter — calls external LLM APIs to:
  (1) rewrite user questions into semantic variants (SFT augmentation)
  (2) refine/improve assistant replies for a given user turn (DPO chosen)

Supported providers: ollama | openai | dashscope | deepseek | zhipu
All HTTP providers use OpenAI-compatible /chat/completions endpoints.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

# Config passed via run_augment()
_cfg: dict = {}

logger = logging.getLogger("training_data.augmenter")

_REWRITE_PROMPT = """你是一个对话数据增强助手。请将以下用户问题改写为 {n} 个语义相同但表达方式不同的变体。

原始问题：{question}

要求：
1. 保持原意不变
2. 使用不同的句式、措辞、长短
3. 每个变体单独一行，不要编号
4. 只输出变体，不要解释"""

_REFINE_RESPONSE_PROMPT = """你是一个高质量的AI助手。请基于下面的对话上下文，针对用户最后一个问题，写出一个**更优质、详细、有帮助**的回答。

要求：
1. 比原回答更准确、结构更清晰
2. 适当展开细节，但避免啰嗦
3. 使用与原回答不同的措辞和结构
4. 直接输出新回答正文，不要解释、不要前缀

==== 对话上下文 ====
{context}

==== 用户最后一个问题 ====
{question}

==== 原回答（仅作参考，请写出更好的版本） ====
{original}

==== 你的新回答 ====
"""


# ── HTTP client builders ──────────────────────────────────────────


def _build_ollama_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_cfg["ollama_url"],
        timeout=httpx.Timeout(120.0),
        trust_env=False,
    )


def _openai_compat_client(base_url: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url or "",
        timeout=httpx.Timeout(120.0),
        trust_env=False,
    )


# ── Provider call functions ───────────────────────────────────────
# Each takes (client, messages, **kwargs) and returns the assistant text.


async def _call_ollama(client: httpx.AsyncClient, messages: list[dict], **kwargs) -> str:
    resp = await client.post("/chat/completions", json={
        "model": _cfg["ollama_model"],
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 1024,
        **kwargs,
    })
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _call_openai(client: httpx.AsyncClient, messages: list[dict], **kwargs) -> str:
    resp = await client.post("/chat/completions", json={
        "model": _cfg["openai_model"],
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 1024,
        **kwargs,
    }, headers={
        "Authorization": f"Bearer {_cfg['openai_key']}",
        "Content-Type": "application/json",
    })
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _call_dashscope(client: httpx.AsyncClient, messages: list[dict], **kwargs) -> str:
    client.base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    resp = await client.post("/chat/completions", json={
        "model": _cfg["dashscope_model"],
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 1024,
        **kwargs,
    }, headers={
        "Authorization": f"Bearer {_cfg['dashscope_key']}",
        "Content-Type": "application/json",
    })
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _call_deepseek(client: httpx.AsyncClient, messages: list[dict], **kwargs) -> str:
    client.base_url = _cfg.get("deepseek_url", "https://api.deepseek.com/v1")
    resp = await client.post("/chat/completions", json={
        "model": _cfg.get("deepseek_model", "deepseek-chat"),
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 1024,
        **kwargs,
    }, headers={
        "Authorization": f"Bearer {_cfg['deepseek_key']}",
        "Content-Type": "application/json",
    })
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _call_zhipu(client: httpx.AsyncClient, messages: list[dict], **kwargs) -> str:
    client.base_url = "https://open.bigmodel.cn/api/paas/v4"
    resp = await client.post("/chat/completions", json={
        "model": _cfg.get("zhipu_model", "glm-4-flash"),
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 1024,
        **kwargs,
    }, headers={
        "Authorization": f"Bearer {_cfg['zhipu_key']}",
        "Content-Type": "application/json",
    })
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _call_kimi(client: httpx.AsyncClient, messages: list[dict], **kwargs) -> str:
    """Moonshot Kimi — OpenAI-compatible API."""
    client.base_url = _cfg.get("kimi_url", "https://api.moonshot.cn/v1")
    resp = await client.post("/chat/completions", json={
        "model": _cfg.get("kimi_model", "moonshot-v1-8k"),
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": 1024,
        **kwargs,
    }, headers={
        "Authorization": f"Bearer {_cfg['kimi_key']}",
        "Content-Type": "application/json",
    })
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


PROVIDER_CALLERS = {
    "ollama": _call_ollama,
    "openai": _call_openai,
    "dashscope": _call_dashscope,
    "deepseek": _call_deepseek,
    "zhipu": _call_zhipu,
    "kimi": _call_kimi,
}


def _make_client_for(provider: str) -> httpx.AsyncClient:
    if provider == "ollama":
        return _build_ollama_client()
    # All other providers set base_url inside their call function.
    return _openai_compat_client()


# ── Prompt wrappers ───────────────────────────────────────────────


async def _rewrite_one(client: httpx.AsyncClient, question: str, n: int,
                       caller) -> list[str]:
    prompt = _REWRITE_PROMPT.format(n=n, question=question)
    try:
        text = await caller(client, [{"role": "user", "content": prompt}])
        lines = [l.strip(" -•·1234567890.)、。") for l in text.split("\n") if l.strip()]
        return [l for l in lines if len(l) > 3 and l != question]
    except Exception as e:
        logger.warning("Rewrite failed for '%s...': %s", question[:30], e)
        return []


async def _refine_response(client: httpx.AsyncClient, context: str,
                            question: str, original: str, caller) -> str | None:
    prompt = _REFINE_RESPONSE_PROMPT.format(
        context=context, question=question, original=original,
    )
    try:
        return await caller(client, [{"role": "user", "content": prompt}])
    except Exception as e:
        logger.warning("Refine failed for '%s...': %s", question[:30], e)
        return None


# ── Main augment routines ─────────────────────────────────────────


async def augment(sft_samples: list[dict]) -> list[dict]:
    """Augment SFT samples with question rewrites (and optional chosen pairs).

    Returns a new list combining original + augmented samples.
    """
    if not sft_samples:
        return []

    caller = PROVIDER_CALLERS.get(_cfg["provider"])
    if caller is None:
        logger.error("Unknown augment provider: %s", _cfg["provider"])
        return sft_samples

    sem = asyncio.Semaphore(int(_cfg.get("concurrency", 3)))
    new_samples: list[dict] = list(sft_samples)

    async def _process(sample: dict) -> list[dict]:
        async with sem:
            results = []
            user_msgs = [m for m in sample["messages"] if m["role"] == "user"]
            if not user_msgs:
                return []
            last_question = user_msgs[-1]["content"]
            client = _make_client_for(_cfg["provider"])
            async with client:
                variants = await _rewrite_one(
                    client, last_question, int(_cfg.get("variants_per_question", 2)),
                    caller,
                )
                for v in variants:
                    new_msgs = [
                        m for m in sample["messages"] if m["role"] != "user"
                    ] + [{"role": "user", "content": v}]
                    results.append({
                        **sample,
                        "messages": new_msgs,
                        "augmented": True,
                        "original_question": last_question,
                    })
            return results

    liked = [s for s in sft_samples if s.get("label") == "like"]
    rest = [s for s in sft_samples if s.get("label") != "like"]
    to_augment = liked[:100] + rest[:50]

    tasks = [_process(s) for s in to_augment]
    for result_list in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(result_list, list):
            new_samples.extend(result_list)

    logger.info("Augmentation (rewrites): %d original → %d total",
                len(sft_samples), len(new_samples))
    return new_samples


async def refine_responses(
    candidate_groups: list[dict],
) -> dict[str, dict[str, str]]:
    """Refine assistant responses via LLM, used to inject llm_rewrite candidates.

    Args:
        candidate_groups: list of dicts each with keys
            {session_id, context, user_msg, candidates}

    Returns:
        {session_id: {user_content: refined_reply}}
    """
    if not candidate_groups:
        return {}

    caller = PROVIDER_CALLERS.get(_cfg["provider"])
    if caller is None:
        logger.error("Unknown augment provider: %s", _cfg["provider"])
        return {}

    sem = asyncio.Semaphore(int(_cfg.get("concurrency", 3)))
    out: dict[str, dict[str, str]] = {}
    succeeded = 0

    async def _process(group: dict) -> None:
        nonlocal succeeded
        async with sem:
            user_content = group["user_msg"].get("content", "")
            if not user_content:
                return
            # Pick the highest-scoring original candidate as the "original"
            # reference for the refiner.
            if not group["candidates"]:
                return
            best = max(group["candidates"], key=lambda c: c["score"])
            original = best["content"]
            context_str = "\n".join(
                f"{m['role']}: {m['content']}"
                for m in group["context"][-8:]
            )
            client = _make_client_for(_cfg["provider"])
            async with client:
                refined = await _refine_response(
                    client, context_str, user_content, original, caller,
                )
            if refined and len(refined) >= 10:
                out.setdefault(group["session_id"], {})[user_content] = refined
                succeeded += 1

    tasks = [_process(g) for g in candidate_groups]
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Refine responses: %d/%d groups refined via %s",
                succeeded, len(candidate_groups), _cfg["provider"])
    return out


def run_augment(sft_samples: list[dict], augment_cfg: dict) -> list[dict]:
    """Synchronous wrapper for question-rewrite augmentation."""
    global _cfg
    _cfg = augment_cfg
    return asyncio.run(augment(sft_samples))


def run_refine(candidate_groups: list[dict], augment_cfg: dict) -> dict[str, dict[str, str]]:
    """Synchronous wrapper for response refinement (DPO chosen injection)."""
    global _cfg
    _cfg = augment_cfg
    return asyncio.run(refine_responses(candidate_groups))
