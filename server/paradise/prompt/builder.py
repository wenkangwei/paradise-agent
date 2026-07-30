"""PromptBuilder — assembles 10-layer system prompts for Paradise agents.

Layers:
  1. Soul (character identity)
  2. User info (owner name/personality)
  3. Channel context (participants, @mention rules)
  4. Message history (with sender names)
  5. Long-term memory (cross-session summaries)
  6. Short-term memory (current session compression)
  7. Lifelong memory (persistent facts)
  8. Emotion state (mood, hunger, energy modifier)
  9. Tools/skills description
 10. Output format (thinking/response tags + current time)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paradise.core.channel import Channel

from paradise.prompt.templates import FORMAT_SUFFIX, TOOL_FORMAT_SUFFIX, HEARTBEAT_SYSTEM_PROMPT


class PromptBuilder:
    """Builds 10-layer system prompts with channel awareness."""

    def build(
        self,
        agent_name: str = "",
        soul_md: str = "",
        user_name: str = "",
        user_personality: str = "",
        user_description: str = "",
        channel: Channel | None = None,
        memory_md: str = "",
        longterm_summary: str = "",
        session_summary: str = "",
        mood_modifier: str = "",
        tools_desc: str = "",
        enable_tools: bool = False,
    ) -> str:
        parts = []

        # Layer 1: Soul
        if soul_md:
            parts.append(soul_md)
        elif agent_name:
            parts.append(f"你是{agent_name}，一个AI宠物角色。")

        # Layer 2: User info
        if user_name:
            user_part = f"你的主人叫{user_name}。"
            if user_personality:
                user_part += f"\n主人的性格: {user_personality}。"
            if user_description:
                user_part += f"\n主人简介: {user_description}。"
            parts.append(user_part)

        # Layer 3: Channel context
        if channel:
            channel_type_name = "广场" if channel.type == "plaza" else "私聊"
            channel_text = f"你在「{channel_type_name}」频道中。"
            channel_text += "\n" + channel.get_participants_text()
            if channel.type == "plaza":
                channel_text += (
                    "\n\n这是多角色频道！注意查看其他成员的发言，积极用 @名字 互动。"
                    " 例如: @大黄 你说得对！ \n"
                    "重要：你应该主动与其他成员交流，回应他们的话，"
                    "让对话自然地持续下去。不要只是自说自话。"
                )
            else:
                channel_text += (
                    "\n\n你可以用 @名字 的方式回应特定成员。"
                    " 例如: @大黄 你说得对！ 如果要回应所有人就直接说即可。"
                )
            parts.append(channel_text)

        # Layer 4: Message history
        if channel:
            history_text = channel.get_history_text(last_n=20)
            if history_text:
                parts.append(history_text)

        # Layer 5: Long-term memory
        if longterm_summary:
            parts.append(longterm_summary)

        # Layer 6: Short-term memory
        if session_summary:
            parts.append(session_summary)

        # Layer 7: Lifelong memory
        if memory_md:
            parts.append(f"你的长期记忆:\n{memory_md}")

        # Layer 8: Emotion state
        if mood_modifier:
            parts.append(f"[当前状态] {mood_modifier}")

        # Layer 9: Tools/skills
        if tools_desc:
            parts.append(f"你可以使用以下工具:\n{tools_desc}")

        # Layer 10: Output format + current time
        parts.append(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        if enable_tools:
            parts.append(TOOL_FORMAT_SUFFIX)
        else:
            parts.append(FORMAT_SUFFIX)

        return "\n\n".join(parts)

    def build_heartbeat(
        self,
        agent_name: str = "",
        soul_md: str = "",
        heartbeat_md: str = "",
        agents_md: str = "",
        emotion_state: dict | None = None,
        recent_context: str = "",
        trigger_reason: str = "",
    ) -> tuple[str, str]:
        """Build heartbeat prompt. Returns (system_prompt, user_prompt)."""
        system = HEARTBEAT_SYSTEM_PROMPT

        parts = []
        # Character context
        if soul_md:
            parts.append(f"## 你的角色\n{soul_md[:500]}")
        elif agent_name:
            parts.append(f"## 你的角色\n你是{agent_name}。")

        # State
        if emotion_state:
            parts.append(
                f"## 当前状态\n"
                f"心情: {emotion_state.get('mood', 'happy')}\n"
                f"能量: {emotion_state.get('energy', 1.0):.1f}\n"
                f"饥饿: {emotion_state.get('hunger', 0.0):.1f}\n"
                f"好感: {emotion_state.get('affection', 0.5):.1f}\n"
                f"无聊: {emotion_state.get('boredom', 0.0):.1f}"
            )

        # Heartbeat rules
        if heartbeat_md:
            parts.append(f"## 心跳规则\n{heartbeat_md}")

        # Partners
        if agents_md:
            parts.append(f"## 伙伴信息\n{agents_md[:500]}")

        # Recent dialog
        if recent_context:
            parts.append(f"## 近期对话\n{recent_context}")

        # Trigger reason
        if trigger_reason:
            parts.append(f"## 触发原因\n{trigger_reason}")

        user_prompt = "\n\n".join(parts)
        return system, user_prompt


# Singleton
prompt_builder = PromptBuilder()
