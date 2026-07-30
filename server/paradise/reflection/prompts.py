"""Reflection prompts — three-tier prompt templates for self-reflection."""

from __future__ import annotations

from paradise.emotion.engine import EmotionState


def build_post_turn_prompt(
    agent_name: str,
    user_message: str,
    agent_response: str,
    emotion_state: EmotionState,
) -> str:
    """Build lightweight post-turn reflection prompt."""
    return (
        f"你是{agent_name}的记忆助手。回顾刚才的对话：\n\n"
        f"用户说了: {user_message[:500]}\n\n"
        f"你回复了: {agent_response[:500]}\n\n"
        f"当前情绪: 心情={emotion_state.mood}, 能量={emotion_state.energy:.1f}, "
        f"饥饿={emotion_state.hunger:.1f}, 好感={emotion_state.affection:.1f}, "
        f"无聊={emotion_state.boredom:.1f}\n\n"
        f"请严格用以下JSON格式回答，不要输出其他内容：\n"
        f"{{\n"
        f'  "key_facts": ["用户喜欢间谍过家家", "用户养了一只叫小白的橘猫"],\n'
        f'  "emotion_delta": {{"hunger": 0.0, "energy": 0.0, "affection": 0.0, "boredom": 0.0}},\n'
        f'  "user_impression": "对用户的一句话理解"\n'
        f"}}\n\n"
        f"规则：\n"
        f"1. key_facts 必须是字符串数组，每个元素是一句完整的事实陈述\n"
        f"2. 尽量从对话中提取关于用户的喜好、经历、特点、身份等信息\n"
        f"3. 即使信息很少也要提取，不要返回空数组\n"
        f"4. emotion_delta 每个值范围 -0.1 到 0.1\n"
    )


def build_idle_prompt(
    agent_name: str,
    idle_minutes: float,
    recent_conversations: str,
    current_memory: str,
    emotion_state: EmotionState,
) -> str:
    """Build idle-time reflection prompt."""
    return (
        f"你是{agent_name}。你已经空闲了{idle_minutes:.0f}分钟。\n\n"
        f"回顾你最近的对话:\n{recent_conversations[:2000]}\n\n"
        f"当前记忆:\n{current_memory[:1000]}\n\n"
        f"当前情绪状态: 心情={emotion_state.mood}, 能量={emotion_state.energy:.1f}, "
        f"饥饿={emotion_state.hunger:.1f}, 好感={emotion_state.affection:.1f}\n\n"
        f"请严格用以下JSON格式回答，不要输出其他内容：\n"
        f"{{\n"
        f'  "memory_updates": "需要更新到长期记忆的内容",\n'
        f'  "emotion_adjustment": {{"energy": 0.0, "affection": 0.0, "boredom": 0.0}},\n'
        f'  "insights": "对自身状态的觉察",\n'
        f'  "next_actions": "建议的自主行为"\n'
        f"}}"
    )


def build_scheduled_prompt(
    agent_name: str,
    all_conversations: str,
    recent_reflections: str,
) -> str:
    """Build deep scheduled reflection prompt."""
    return (
        f"你是{agent_name}。这是一次深度自省。\n\n"
        f"过去7天的反思记录:\n{recent_reflections[:2000]}\n\n"
        f"对话记忆:\n{all_conversations[:3000]}\n\n"
        f"请严格用以下JSON格式回答，不要输出其他内容：\n"
        f"{{\n"
        f'  "personality_growth": "角色成长与变化",\n'
        f'  "relationship_summary": "与用户的关系变化",\n'
        f'  "memory_compression": "可压缩的旧记忆摘要",\n'
        f'  "goal_adjustment": "目标/愿望的调整",\n'
        f'  "emotion_baseline": {{"energy": 0.5, "affection": 0.5}}\n'
        f"}}"
    )
