"""Context Compactor — token-aware conversation summarization.

When the conversation context approaches 50% of the model's context window,
older messages are grouped into chunks and summarized via LLM. Each summary
acts as a "collapsed folder" — the LLM sees the summary and can expand it
on demand via the context_expand tool.

Architecture:
  1. estimate_tokens() — fast char-based token estimation
  2. should_compact() — check if tokens exceed 50% threshold
  3. compact() — group older messages, call LLM to summarize each chunk
  4. context_expand tool — registered in paradise registry, retrieves original
     messages by summary_id for on-demand expansion
  5. SummaryIndex — in-memory store mapping summary_id → original messages

Usage:
    from context_compactor import ContextCompactor, estimate_tokens

    compactor = ContextCompactor(model_name="qwen2.5:7b")
    if compactor.should_compact(messages):
        compacted_messages = await compactor.compact(messages, conv_id)
        # compacted_messages replaces original messages in the LLM request
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Token estimation ─────────────────────────────────────────────────

# Conservative estimates: 1 char ≈ 0.5 tokens for Chinese, 0.3 tokens for English
# Actual ratio depends on the model's tokenizer, but this avoids underestimation.
_CHINESE_CHAR_RATIO = 0.5
_ENGLISH_CHAR_RATIO = 0.3

# Model context window sizes (tokens)
_MODEL_WINDOWS: dict[str, int] = {
    "qwen2.5:0.5b": 32768,
    "qwen2.5:3b": 32768,
    "qwen2.5:7b": 32768,
    "qwen2.5:14b": 32768,
    "qwen2.5:32b": 32768,
    "qwen2.5:72b": 32768,
    "qwen2.5vl:7b": 32768,
    "llava:7b": 4096,
    "llava:13b": 4096,
    "bakllava:7b": 4096,
    "llama3.2:3b": 32768,
    "qwen3:8b": 32768,
}

_DEFAULT_WINDOW = 8192  # fallback for unknown models
_COMPACT_THRESHOLD = 0.5  # compact at 50% window
_COMPACT_CHUNK_TURNS = 3  # group ~3 round-trip turns per summary chunk
_SUMMARY_MAX_TOKENS = 150  # max tokens for each summary
_ESTIMATED_SYSTEM_PROMPT_TOKENS = 300  # reserve for system prompt + tools


def estimate_tokens(text: str) -> int:
    """Fast token estimation based on character count.

    Uses different ratios for CJK characters (higher token density)
    vs ASCII characters (lower token density).
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u303f')
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    other = len(text) - cjk - ascii_chars
    return int(cjk * _CHINESE_CHAR_RATIO + ascii_chars * _ENGLISH_CHAR_RATIO + other * 0.4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens for a list of messages.

    Accounts for message overhead tokens (~4 tokens per message for role markers).
    """
    total = 0
    for msg in messages:
        total += 4  # role/format overhead
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens(part.get("text", ""))
                    if part.get("type") == "image_url":
                        total += 200  # rough estimate for image token cost
        # Count tool_calls and tool_call_id overhead
        if msg.get("tool_calls"):
            total += 50  # rough overhead per tool call
    return total


def get_model_window(model_name: str) -> int:
    """Get context window size for a model."""
    # Try exact match first
    if model_name in _MODEL_WINDOWS:
        return _MODEL_WINDOWS[model_name]
    # Try prefix match (e.g., "qwen2.5:7b-instruct" → "qwen2.5:7b")
    for key, size in _MODEL_WINDOWS.items():
        if model_name.startswith(key.split(":")[0]) and key.split(":")[1] in model_name:
            return size
    # Try by parameter size pattern
    import re
    match = re.search(r'(\d+)b', model_name.lower())
    if match:
        size_b = int(match.group(1))
        if size_b <= 3:
            return 32768
        elif size_b <= 7:
            return 32768
        else:
            return 32768
    return _DEFAULT_WINDOW


# ── Summary Index ────────────────────────────────────────────────────

class SummaryIndex:
    """Index: summary_id → original messages + metadata.

    Phase 5: summaries are persisted as per-conv_id JSON files under
    ``data/summaries/{conv_id}.json`` so they survive process restarts.
    On first access to a conv_id not yet in memory, the index lazy-loads
    its JSON file from disk. Writes happen on every ``store()`` call.
    """

    def __init__(self):
        self._entries: dict[str, dict] = {}
        self._conv_indices: dict[str, list[str]] = {}  # conv_id → [summary_ids]
        self._persist_dir = Path(
            os.getenv("PARADISE_DATA_DIR", "data")
        ) / "summaries"
        self._persist_dir.mkdir(parents=True, exist_ok=True)

    def store(self, conv_id: str, chunk_id: str, original_messages: list[dict],
              summary: str, start_index: int, end_index: int) -> str:
        """Store a summary entry. Returns the summary_id."""
        summary_id = f"{conv_id}_{chunk_id}"
        self._entries[summary_id] = {
            "conv_id": conv_id,
            "chunk_id": chunk_id,
            "messages": original_messages,
            "summary": summary,
            "start_index": start_index,
            "end_index": end_index,
            "created_at": time.time(),
        }
        if conv_id not in self._conv_indices:
            self._conv_indices[conv_id] = []
        self._conv_indices[conv_id].append(summary_id)
        self._persist_conv(conv_id)
        return summary_id

    def get(self, summary_id: str) -> dict | None:
        """Retrieve original messages for a summary."""
        return self._entries.get(summary_id)

    def get_summaries_for_conv(self, conv_id: str) -> list[dict]:
        """Get all summaries for a conversation, oldest first.

        Lazy-loads from disk on first access for a conv_id not yet
        seen in this process.
        """
        if conv_id not in self._conv_indices:
            self._load_conv(conv_id)
        ids = self._conv_indices.get(conv_id, [])
        return [self._entries[sid] for sid in ids if sid in self._entries]

    def remove_conv(self, conv_id: str) -> None:
        """Remove all entries for a conversation."""
        ids = self._conv_indices.pop(conv_id, [])
        for sid in ids:
            self._entries.pop(sid, None)
        # Also remove the persisted JSON file
        fpath = self._persist_dir / f"{conv_id}.json"
        try:
            fpath.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("SummaryIndex remove file failed %s: %s", conv_id, e)

    def render_context(self, conv_id: str, limit: int = 10) -> str:
        """Render all summaries as a compact context block for the system prompt."""
        summaries = self.get_summaries_for_conv(conv_id)
        if not summaries:
            return ""
        lines = ["[对话历史摘要 — 可按需展开]"]
        for i, entry in enumerate(summaries[-limit:]):
            sid = f"{entry['conv_id']}_{entry['chunk_id']}"
            lines.append(f"<summary id=\"{sid}\">{entry['summary']}</summary>")
        return "\n".join(lines)

    # ── Phase 5: disk persistence ──────────────────────────────────

    def _persist_conv(self, conv_id: str) -> None:
        """Write this conversation's summaries to a JSON file."""
        entries = self.get_summaries_for_conv(conv_id)
        fpath = self._persist_dir / f"{conv_id}.json"
        try:
            fpath.write_text(
                json.dumps(entries, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("SummaryIndex persist failed %s: %s", conv_id, e)

    def _load_conv(self, conv_id: str) -> None:
        """Load summaries for a conversation from disk (if file exists)."""
        fpath = self._persist_dir / f"{conv_id}.json"
        if not fpath.exists():
            return
        try:
            entries = json.loads(fpath.read_text(encoding="utf-8"))
            for entry in entries:
                sid = f"{entry['conv_id']}_{entry['chunk_id']}"
                if sid not in self._entries:
                    self._entries[sid] = entry
                    self._conv_indices.setdefault(conv_id, []).append(sid)
        except Exception as e:
            logger.warning("SummaryIndex load failed %s: %s", conv_id, e)


# Global singleton
summary_index = SummaryIndex()


# ── Context Compactor ────────────────────────────────────────────────

class ContextCompactor:
    """Manages context window compaction via LLM summarization."""

    def __init__(self, model_name: str = "qwen2.5:7b",
                 llm_call: Callable | None = None,
                 threshold: float = _COMPACT_THRESHOLD):
        self.model_name = model_name
        self._window = get_model_window(model_name)
        self._threshold = threshold
        self._llm_call = llm_call  # async callable: (system_prompt, user_prompt) → str
        self._max_tokens = int(self._window * self._threshold)

    @property
    def window_size(self) -> int:
        return self._window

    @property
    def max_tokens_before_compact(self) -> int:
        """Token count at which compaction triggers."""
        return self._max_tokens

    def should_compact(self, messages: list[dict], extra_tokens: int = 0) -> bool:
        """Check if messages + system prompt would exceed the threshold."""
        total = (estimate_messages_tokens(messages)
                 + _ESTIMATED_SYSTEM_PROMPT_TOKENS
                 + extra_tokens)
        return total >= self._max_tokens

    def current_usage_ratio(self, messages: list[dict]) -> float:
        """Return current token usage as fraction of window (0.0 - 1.0)."""
        total = estimate_messages_tokens(messages) + _ESTIMATED_SYSTEM_PROMPT_TOKENS
        return min(total / self._window, 1.0)

    async def compact(self, messages: list[dict], conv_id: str) -> list[dict]:
        """Compact older messages into summaries.

        Strategy:
        1. Keep the most recent ~30% of messages intact
        2. Group the older 70% into chunks of ~3 turns each
        3. Summarize each chunk via LLM
        4. Store originals in summary_index, replace with summaries

        Returns a new messages list with summaries prepended.
        """
        if len(messages) < 4:
            return messages  # Too few messages to compact

        # Split: keep recent 30% intact, compact older 70%
        split_point = max(int(len(messages) * 0.3), 4)
        older = messages[:-split_point] if len(messages) > split_point else messages[:len(messages)//2]
        recent = messages[-split_point:] if len(messages) > split_point else messages[len(messages)//2:]

        if len(older) < 2:
            return messages

        # Group older messages into chunks
        chunk_size = _COMPACT_CHUNK_TURNS * 2  # ~3 user+assistant pairs = 6 messages
        chunks = []
        for i in range(0, len(older), chunk_size):
            chunk = older[i:i + chunk_size]
            if len(chunk) >= 1:
                chunks.append(chunk)

        # Summarize each chunk
        summaries = []
        for i, chunk in enumerate(chunks):
            summary = await self._summarize_chunk(chunk, i + 1, len(chunks))
            if summary:
                chunk_id = f"chunk_{i + 1}_{hashlib.md5(json.dumps(chunk, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]}"
                start_idx = i * chunk_size
                end_idx = start_idx + len(chunk) - 1
                summary_index.store(conv_id, chunk_id, chunk, summary, start_idx, end_idx)
                summaries.append({
                    "chunk_id": chunk_id,
                    "summary": summary,
                    "message_count": len(chunk),
                })

        # Build compacted messages list
        if not summaries:
            return messages  # Summarization failed, pass through

        compacted = []
        for s in summaries:
            compacted.append({
                "role": "system",
                "content": (
                    f"[对话摘要 {s['chunk_id']} — {s['message_count']}条消息]\n"
                    f"{s['summary']}\n"
                    f"[如需完整对话内容，调用 context_expand 工具展开此摘要]"
                ),
            })
        compacted.extend(recent)

        logger.info(
            "Compacted %d messages → %d summaries + %d recent (%.0f%% reduction)",
            len(older), len(summaries), len(recent),
            (1 - len(compacted) / len(messages)) * 100,
        )
        return compacted

    async def _summarize_chunk(self, messages: list[dict], chunk_num: int,
                                total_chunks: int) -> str | None:
        """Summarize a chunk of messages via LLM.

        Uses the injected llm_call or a direct httpx call to Ollama.
        """
        if not self._llm_call:
            return await self._summarize_via_ollama(messages, chunk_num, total_chunks)

        # Build the conversation text
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = " ".join(text_parts)
            if content:
                prefix = "用户" if role == "user" else "AI" if role == "assistant" else role
                lines.append(f"{prefix}: {content[:300]}")

        if not lines:
            return None

        conversation = "\n".join(lines)
        system = "你是一个对话摘要工具。用2-3句话（中文）简洁概括这段对话的关键信息。只输出摘要，不要加任何前缀或格式。"
        user = f"请概括这段对话（第{chunk_num}/{total_chunks}段）：\n\n{conversation}"

        try:
            result = await self._llm_call(system, user)
            if result:
                result = result.strip()
                if len(result) > 300:
                    result = result[:300] + "..."
                return result
        except Exception as e:
            logger.warning("Chunk %d/%d summarization failed: %s", chunk_num, total_chunks, e)

        # Fallback: use the first and last message as a rough summary
        first = lines[0] if lines else ""
        last = lines[-1] if len(lines) > 1 else ""
        return f"对话片段：{first[:100]} ... {last[:100]}"

    async def _summarize_via_ollama(self, messages: list[dict],
                                      chunk_num: int, total_chunks: int) -> str | None:
        """Summarize via direct Ollama API call (fallback when no llm_call injected)."""
        import httpx

        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = " ".join(text_parts)
            if content:
                prefix = "用户" if role == "user" else "AI" if role == "assistant" else role
                lines.append(f"{prefix}: {content[:300]}")

        if not lines:
            return None

        conversation = "\n".join(lines)
        ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        model = os.getenv("SUMMARY_MODEL", "qwen2.5:3b")

        payload = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": "用2-3句话（中文）简洁概括这段对话的关键信息。只输出摘要。"},
                {"role": "user", "content": f"概括对话（第{chunk_num}/{total_chunks}段）：\n{conversation}"},
            ],
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
            logger.warning("Ollama summarization failed: %s", e)
            # Ultra-fallback: just use first/last
            first = lines[0][:100] if lines else ""
            last = lines[-1][:100] if len(lines) > 1 else ""
            return f"{first} ... {last}" if first else None


# Global compactor instance (initialized with model at runtime)
_compactor: ContextCompactor | None = None


def get_compactor(model_name: str = "qwen2.5:7b",
                   llm_call: Callable | None = None) -> ContextCompactor:
    """Get or create the global ContextCompactor instance."""
    global _compactor
    if _compactor is None or _compactor.model_name != model_name:
        _compactor = ContextCompactor(model_name=model_name, llm_call=llm_call)
    return _compactor


# ── context_expand Tool (registered in paradise registry) ────────────

def _handle_context_expand(args: dict[str, Any]) -> str:
    """Expand a conversation summary to reveal original messages.

    Registered as tool "context_expand" in toolset "context".
    """
    summary_id = args.get("summary_id", "")
    if not summary_id:
        return json.dumps({"error": "summary_id is required"}, ensure_ascii=False)

    entry = summary_index.get(summary_id)
    if not entry:
        return json.dumps({
            "error": f"summary not found: {summary_id}",
            "available_summaries": list(summary_index._conv_indices.get(
                summary_id.rsplit("_", 1)[0] if "_chunk_" in summary_id else summary_id, []
            )),
        }, ensure_ascii=False)

    # Render original messages
    lines = [f"展开摘要: {summary_id}\n"]
    for msg in entry["messages"]:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(text_parts)
        prefix = "用户" if role == "user" else "AI" if role == "assistant" else role
        lines.append(f"{prefix}: {content}")

    return "\n".join(lines)


CONTEXT_EXPAND_SCHEMA = {
    "name": "context_expand",
    "description": (
        "Expand a previously summarized conversation segment to see the full "
        "original messages. Use this when you need more context from a summary "
        "to answer the user's question accurately. "
        "summary_id can be found in the <summary id=\"...\"> tags in the context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary_id": {
                "type": "string",
                "description": "The summary ID to expand, e.g., 'conv_abc_chunk_1'",
            },
        },
        "required": ["summary_id"],
    },
}

# Tool will be registered when this module is imported by agent_handler
from paradise.tools.registry import registry

registry.register(
    name="context_expand",
    toolset="context",
    schema=CONTEXT_EXPAND_SCHEMA,
    handler=_handle_context_expand,
    is_async=False,
    description="Expand a conversation summary to view original messages",
    emoji="📂",
    max_result_size_chars=10000,
)
