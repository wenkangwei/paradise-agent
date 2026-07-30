"""Thinker — parse LLM output into thinking + response sections.

The LLM is prompted to output in this format:
  <thinking>
  Internal reasoning, memory recall, strategy...
  </thinking>

  <response>
  The actual reply shown to the user.
  </response>

The stream parser detects these tags in real-time and yields
typed events. Also handles non-standard variants like:
  - bare "thinking:" / "response:" prefixes
  - unclosed tags
  - <think/> tags (some models use shorter names)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# Tags to detect and strip from output
_OPEN_TAGS = ("thinking", "think", "response", "reasoning", "thought")
_CLOSE_TAGS = tuple(f"/{t}" for t in _OPEN_TAGS)
_ALL_TAGS = _OPEN_TAGS + _CLOSE_TAGS
_MAX_TAG_LEN = max(len(f"<{t}>") for t in _ALL_TAGS)

# Regex for bare prefix patterns like "thinking: " or "response: "
_BARE_PREFIX_RE = re.compile(
    r'^(?:thinking|think|response|reasoning|thought|assistant)\s*[:：]\s*',
    re.IGNORECASE,
)


class EventType(str, Enum):
    THINKING = "thinking"
    CONTENT = "content"
    PLAN = "plan"


@dataclass
class ParsedEvent:
    type: EventType
    content: str


def _strip_tags(text: str) -> str:
    """Remove all known XML-style tags from text."""
    for tag in _ALL_TAGS:
        text = text.replace(f"<{tag}>", "")
    return text


def _clean_content(text: str) -> str:
    """Clean content before emitting: strip residual tags and bare prefixes."""
    # Remove bare prefixes at line starts
    text = re.sub(
        r'(?m)^(?:thinking|think|response|reasoning|thought|assistant)\s*[:：]\s*',
        '', text,
    )
    # Remove any remaining XML tags
    text = re.sub(r'</?(?:thinking|think|response|reasoning|thought|REASONING_SCRATCHPAD)\b[^>]*>', '', text, flags=re.IGNORECASE)
    return text


class Thinker:
    """Streaming parser that splits LLM output into thinking/response."""

    def __init__(self):
        self._buffer = ""
        self._in_thinking = False
        self._in_response = False
        _TAG_LEN = max(len("</thinking>"), len("</response>"), len("<thinking>"), len("<response>"))
        self._started = False

    def feed(self, chunk: str) -> list[ParsedEvent]:
        """Feed a streaming chunk. Returns parsed events."""
        self._buffer += chunk
        events: list[ParsedEvent] = []

        while self._buffer:
            # Detect opening <thinking> or <response> tag
            if not self._started and not self._in_thinking and not self._in_response:
                # Check for <response> tag first (skip it, go straight to content mode)
                resp_match = re.search(r'<(?:response)\b[^>]*>', self._buffer)
                think_match = re.search(r'<(?:thinking|think|reasoning|thought)\b[^>]*>', self._buffer)

                # Determine which tag comes first
                use_resp = resp_match and (not think_match or resp_match.start() <= think_match.start())
                use_think = think_match and (not resp_match or think_match.start() < resp_match.start())

                if use_resp:
                    # <response> found — skip to content mode
                    before = self._buffer[:resp_match.start()].strip()
                    self._buffer = self._buffer[resp_match.end():].lstrip("\n")
                    self._in_response = True
                    self._started = True
                    if before:
                        events.append(ParsedEvent(EventType.CONTENT, before))
                    continue

                if use_think:
                    # <thinking> found
                    before = self._buffer[:think_match.start()].strip()
                    self._buffer = self._buffer[think_match.end():]
                    self._in_thinking = True
                    self._started = True
                    if before:
                        events.append(ParsedEvent(EventType.CONTENT, before))
                    continue

                # No tag found yet
                if len(self._buffer) > 200 or "<" not in self._buffer:
                    self._in_response = True
                    self._started = True
                    text = self._buffer
                    self._buffer = ""
                    text = _clean_content(text)
                    if text.strip():
                        events.append(ParsedEvent(EventType.CONTENT, text))
                else:
                    self._try_emit_safe("content", events)
                break

                # Found thinking tag
                before = self._buffer[:think_match.start()].strip()
                self._buffer = self._buffer[think_match.end():]
                self._in_thinking = True
                self._started = True
                if before:
                    events.append(ParsedEvent(EventType.CONTENT, before))
                continue

            # Inside <thinking> — look for closing tag
            if self._in_thinking:
                close_match = re.search(r'</(?:thinking|think|reasoning|thought)\b[^>]*>', self._buffer)
                if not close_match:
                    self._emit_with_tail(EventType.THINKING, "</thinking>", events)
                    break

                text = self._buffer[:close_match.start()]
                self._buffer = self._buffer[close_match.end():]
                self._in_thinking = False
                self._buffer = self._buffer.lstrip("\n")
                self._in_response = True
                if text.strip():
                    events.append(ParsedEvent(EventType.THINKING, text))
                continue

            # Inside <response> — look for closing tag
            if self._in_response:
                # Consume <response> opening tag if present
                resp_open = re.search(r'<(?:response)\b[^>]*>', self._buffer)
                if resp_open and resp_open.start() < 5:
                    self._buffer = self._buffer[resp_open.end():]
                    self._buffer = self._buffer.lstrip("\n")
                    continue

                close_match = re.search(r'</(?:response)\b[^>]*>', self._buffer)
                if not close_match:
                    self._emit_with_tail(EventType.CONTENT, "</response>", events)
                    break

                text = self._buffer[:close_match.start()]
                self._buffer = self._buffer[close_match.end():]
                self._in_response = False
                if text.strip():
                    text = _clean_content(text)
                    events.append(ParsedEvent(EventType.CONTENT, text))
                continue

            break

        return events

    def _emit_with_tail(self, event_type: EventType, tag: str, events: list[ParsedEvent]) -> None:
        """Emit buffer content except the tail that might contain a partial tag."""
        tag_len = len(tag)
        if len(self._buffer) <= tag_len:
            return

        safe_end = len(self._buffer) - tag_len
        text = self._buffer[:safe_end]
        self._buffer = self._buffer[safe_end:]
        if text.strip():
            text = _clean_content(text) if event_type == EventType.CONTENT else text
            events.append(ParsedEvent(event_type, text))

    def _try_emit_safe(self, fallback_type: str, events: list[ParsedEvent]) -> None:
        """For unstarted mode: emit content that definitely isn't part of a tag."""
        if len(self._buffer) <= _MAX_TAG_LEN:
            return
        safe_end = len(self._buffer) - _MAX_TAG_LEN
        text = self._buffer[:safe_end]
        self._buffer = self._buffer[safe_end:]
        text = _clean_content(text)
        if text.strip():
            events.append(ParsedEvent(EventType.CONTENT, text))

    def flush(self) -> list[ParsedEvent]:
        """Flush remaining buffer content."""
        events: list[ParsedEvent] = []
        remaining = _strip_tags(self._buffer)
        remaining = _clean_content(remaining)
        remaining = remaining.strip()
        if remaining:
            event_type = EventType.THINKING if self._in_thinking else EventType.CONTENT
            events.append(ParsedEvent(event_type, remaining))
        self._buffer = ""
        return events


def build_thinking_prompt_suffix() -> str:
    """Append to system prompt to enable thinking/response separation."""
    return (
        "\n\n[输出格式要求]\n"
        "你必须按以下格式输出，不要输出任何其他内容：\n"
        "<thinking>\n"
        "在这里进行内心思考：分析用户意图、回忆相关记忆、制定回答策略。"
        "这部分用户默认看不到（可展开查看），所以可以完全自由地思考。\n"
        "</thinking>\n\n"
        "<response>\n"
        "这里是对用户的直接回答，展示你的个性和情感。\n"
        "</response>"
    )
