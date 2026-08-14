"""Phase 6 — ContextCompactionService tests.

Verifies the hybrid context-compaction model:
  1. Two-tier threshold: should_compact (0.5) vs should_drop (0.8)
  2. drop() removes oldest history, keeps recent turns
  3. compact_current() (LLM-triggered) stores {summary_id → summary + original}
  4. expand() reveals a summary's original messages
  5. context_compact + context_expand tools are registered

Run:
    python -m pytest paradise/tests/test_context_compaction_service.py -v
"""
from __future__ import annotations

import json

import pytest

from context_compactor import (
    ContextCompactionService,
    SummaryIndex,
    bind_current_conversation,
    get_current_conversation,
    get_compaction_service,
    _render_expanded_messages,
)


def _make_messages(n: int) -> list[dict]:
    """Build n alternating user/assistant messages."""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"message {i} " + "x" * 50})
    return out


async def _fake_llm(system: str, user: str) -> str:
    return "这是一段对话摘要，包含关键信息。"


# ── SummaryIndex injectability ────────────────────────────────────────

def test_summary_index_injectable_data_dir(tmp_path):
    idx = SummaryIndex(data_dir=str(tmp_path))
    assert str(tmp_path) in str(idx._persist_dir)


def test_service_holds_own_index(tmp_path):
    s1 = ContextCompactionService(data_dir=str(tmp_path / "a"))
    s2 = ContextCompactionService(data_dir=str(tmp_path / "b"))
    assert s1.index is not s2.index


# ── Two-tier threshold ────────────────────────────────────────────────

def test_should_drop_uses_drop_threshold():
    svc = ContextCompactionService(drop_threshold=0.0, llm_call=_fake_llm)
    assert svc.should_drop(_make_messages(6))


def test_should_compact_uses_compact_threshold():
    svc = ContextCompactionService(compact_threshold=0.0, llm_call=_fake_llm)
    assert svc.should_compact(_make_messages(6))


# ── drop() keeps recent turns ─────────────────────────────────────────

def test_drop_keeps_recent_messages():
    svc = ContextCompactionService(drop_threshold=0.0, llm_call=_fake_llm)
    messages = _make_messages(10)
    result = svc.drop(messages)
    # Drops oldest turns (2 msgs at a time) but keeps last 4
    assert len(result) <= len(messages)
    assert len(result) >= 4
    # Recent tail is preserved
    assert result[-1]["content"] == messages[-1]["content"]
    assert result[-2]["content"] == messages[-2]["content"]


# ── compact_current() (LLM-triggered) ─────────────────────────────────

@pytest.mark.asyncio
async def test_compact_current_stores_summary(tmp_path):
    svc = ContextCompactionService(
        data_dir=str(tmp_path), llm_call=_fake_llm,
    )
    bind_current_conversation("conv_x", _make_messages(8))
    result = await svc.compact_current()
    assert "summary_id" in result
    assert result["summary"] == "这是一段对话摘要，包含关键信息。"
    assert result["message_count"] == 8
    # Original messages retrievable via the stored summary_id
    entry = svc.index.get(result["summary_id"])
    assert entry is not None
    assert len(entry["messages"]) == 8


@pytest.mark.asyncio
async def test_compact_current_requires_bound_conversation(tmp_path):
    svc = ContextCompactionService(data_dir=str(tmp_path), llm_call=_fake_llm)
    result = await svc.compact_current()
    assert result.get("error") == "no active conversation to compact"


# ── expand() reveals originals ────────────────────────────────────────

def test_expand_returns_original_messages(tmp_path):
    idx = SummaryIndex(data_dir=str(tmp_path))
    msgs = _make_messages(4)
    sid = idx.store("conv_y", "chunk_1", msgs, "摘要", 0, 3)
    out = _render_expanded_messages(idx, sid)
    assert "展开摘要" in out
    assert "message 0" in out
    assert "message 3" in out


def test_expand_missing_summary_returns_error(tmp_path):
    idx = SummaryIndex(data_dir=str(tmp_path))
    out = _render_expanded_messages(idx, "conv_z_chunk_missing")
    parsed = json.loads(out)
    assert "error" in parsed


# ── Tool registration ─────────────────────────────────────────────────

def test_context_tools_registered():
    from paradise.tools.registry import registry
    names = registry.get_all_tool_names()
    assert "context_compact" in names
    assert "context_expand" in names


# ── TTL expiry + cleanup ──────────────────────────────────────────────

def test_ttl_prunes_expired_entries(tmp_path):
    idx = SummaryIndex(data_dir=str(tmp_path), ttl_seconds=100)
    msgs = _make_messages(4)
    idx.store("conv_t", "chunk_1", msgs, "摘要", 0, 3)
    # Force the entry's timestamp into the distant past
    for sid in idx._conv_indices["conv_t"]:
        idx._entries[sid]["created_at"] = 0
    assert idx.get_summaries_for_conv("conv_t") == []


def test_ttl_keeps_recent_entries(tmp_path):
    idx = SummaryIndex(data_dir=str(tmp_path), ttl_seconds=100)
    msgs = _make_messages(4)
    idx.store("conv_fresh", "chunk_1", msgs, "摘要", 0, 3)
    assert len(idx.get_summaries_for_conv("conv_fresh")) == 1


def test_remove_conv_deletes_file(tmp_path):
    idx = SummaryIndex(data_dir=str(tmp_path))
    msgs = _make_messages(4)
    idx.store("conv_r", "chunk_1", msgs, "摘要", 0, 3)
    fpath = idx._persist_dir / "conv_r.json"
    assert fpath.exists()
    idx.remove_conv("conv_r")
    assert not fpath.exists()
    assert idx.get_summaries_for_conv("conv_r") == []


def test_prune_all_removes_expired_files(tmp_path):
    idx = SummaryIndex(data_dir=str(tmp_path), ttl_seconds=100)
    msgs = _make_messages(4)
    idx.store("conv_p", "chunk_1", msgs, "摘要", 0, 3)
    fpath = idx._persist_dir / "conv_p.json"
    assert fpath.exists()
    # Force file mtime into the distant past
    import os
    os.utime(fpath, (0, 0))
    removed = idx.prune_all()
    assert removed >= 1
    assert not fpath.exists()
