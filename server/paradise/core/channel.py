"""Channel — message channel for plaza (broadcast) and private (1v1) modes.

Agents in a channel can see all messages and know who sent them.
Supports @mention parsing for agent-to-agent interaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ChannelMessage:
    """A single message in a channel."""
    sender_id: str       # "user" | agent_id
    sender_name: str     # "主人" | "小橘"
    content: str
    mentions: list[str] = field(default_factory=list)  # @提及的名字列表
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat(timespec="seconds")


class Channel:
    """Message channel — manages participants and message history."""

    def __init__(self, channel_type: str = "plaza",
                 participants: list[dict] | None = None):
        """
        Args:
            channel_type: "plaza" or "private"
            participants: list of dicts:
                [{"id": "user", "name": "主人", "role": "user"},
                 {"id": "xiaoju", "name": "小橘", "avatar": "🐱",
                  "role": "agent", "soul_md": "你是小橘..."}]
        """
        self.type = channel_type
        self.participants: list[dict] = participants or []
        self.messages: list[ChannelMessage] = []

    def add_message(self, sender_id: str, content: str, sender_name: str = "") -> ChannelMessage:
        """Add a message to the channel. Auto-detects @mentions."""
        if not sender_name:
            sender_name = self._get_name(sender_id)
        mentions = self._extract_mentions(content)
        msg = ChannelMessage(
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            mentions=mentions,
        )
        self.messages.append(msg)
        return msg

    def get_participants_text(self) -> str:
        """Human-readable participant list for prompt injection."""
        lines = []
        for p in self.participants:
            role = "用户" if p.get("role") == "user" else "AI宠物"
            avatar = p.get("avatar", "")
            name = p["name"]
            soul = p.get("soul_md", "")
            # Extract first sentence of soul as brief description
            desc = ""
            if soul:
                first_sentence = re.split(r'[。！？\n]', soul)[0].strip()
                if first_sentence and len(first_sentence) < 40:
                    desc = f"，{first_sentence}"
            lines.append(f"- {name}{avatar}（{role}{desc}）")
        return "频道成员:\n" + "\n".join(lines)

    def get_history(self, last_n: int = 20) -> list[dict]:
        """Return recent messages with sender names for prompt injection."""
        recent = self.messages[-last_n:]
        return [
            {"role": "assistant" if m.sender_id != "user" else "user",
             "name": m.sender_name,
             "content": m.content,
             "sender_id": m.sender_id}
            for m in recent
        ]

    def get_history_text(self, last_n: int = 20) -> str:
        """Return formatted message history text for prompt."""
        history = self.get_history(last_n)
        if not history:
            return ""
        lines = []
        for msg in history:
            lines.append(f"{msg['name']}: {msg['content']}")
        return (
            "[以下是频道中其他人的发言记录，仅供参考，不要重复]\n"
            + "\n".join(lines)
            + "\n[以上是历史记录。请直接回复，不要重复上面的内容]"
        )

    def should_trigger(self, agent_id: str, message: ChannelMessage) -> bool:
        """Check if a message should trigger a specific agent.

        Rules:
        - User messages trigger all active agents
        - Agent messages trigger other agents if they are @mentioned or in plaza mode
        - An agent never triggers itself
        """
        if message.sender_id == agent_id:
            return False
        if message.sender_id == "user":
            return True
        # Other agent's message: trigger if @mentioned or in plaza mode
        agent_name = self._get_name(agent_id)
        if agent_name in message.mentions:
            return True
        if self.type == "plaza":
            return True
        return False

    def _get_name(self, participant_id: str) -> str:
        for p in self.participants:
            if p["id"] == participant_id:
                return p["name"]
        return participant_id

    @staticmethod
    def _extract_mentions(content: str) -> list[str]:
        """Extract @mentions from message content. Supports @名字 format."""
        pattern = r'@(\S+?)(?:\s|$|[，。！？,.!?])'
        return re.findall(pattern, content)
