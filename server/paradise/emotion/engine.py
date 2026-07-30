"""EmotionEngine — agent emotional state, mood, hunger, energy, affection.

Mood states affect response style via mood modifiers injected into system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Tick parameters
HUNGER_RATE = 0.001       # per tick (1s)
ENERGY_DECAY = 0.0005     # per tick
BOREDOM_RATE = 0.002      # per tick
BOREDOM_THRESHOLD = 0.6   # triggers bored mood
HUNGER_THRESHOLD = 0.7    # triggers hungry mood


@dataclass
class EmotionState:
    mood: str = "happy"
    energy: float = 1.0
    hunger: float = 0.0
    affection: float = 0.5
    boredom: float = 0.0
    last_interaction: str = field(default_factory=lambda: datetime.now().isoformat())
    tick_count: int = 0

    def clamp(self):
        self.energy = max(0.0, min(1.0, self.energy))
        self.hunger = max(0.0, min(1.0, self.hunger))
        self.affection = max(0.0, min(1.0, self.affection))
        self.boredom = max(0.0, min(1.0, self.boredom))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mood": self.mood,
            "energy": self.energy,
            "hunger": self.hunger,
            "affection": self.affection,
            "boredom": self.boredom,
            "last_interaction": self.last_interaction,
            "tick_count": self.tick_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EmotionState:
        return cls(
            mood=d.get("mood", "happy"),
            energy=d.get("energy", 1.0),
            hunger=d.get("hunger", 0.0),
            affection=d.get("affection", 0.5),
            boredom=d.get("boredom", 0.0),
            last_interaction=d.get("last_interaction", datetime.now().isoformat()),
            tick_count=d.get("tick_count", 0),
        )


# Mood emoji mapping
MOOD_EMOJI: dict[str, str] = {
    "happy": "😊",
    "excited": "🤩",
    "playful": "😺",
    "bored": "😑",
    "hungry": "🍽️",
    "sad": "😢",
    "anxious": "😰",
    "contemplative": "🤔",
    "tired": "😴",
    "loving": "🥰",
}


class EmotionEngine:
    """Manages emotional state updates."""

    def tick(self, state: EmotionState) -> None:
        """Called every 1 second. Micro-updates to hunger/energy/boredom."""
        state.hunger += HUNGER_RATE
        state.energy -= ENERGY_DECAY
        state.boredom += BOREDOM_RATE
        state.tick_count += 1
        state.mood = self._compute_mood(state)
        state.clamp()

    def on_feed(self, state: EmotionState, amount: int = 1) -> None:
        """Called when user feeds the agent. Restores hunger + boosts affection."""
        state.hunger = max(0.0, state.hunger - amount * 0.15)
        state.affection = min(1.0, state.affection + amount * 0.05)
        state.energy = min(1.0, state.energy + amount * 0.03)
        state.boredom = max(0.0, state.boredom - 0.1)
        state.mood = self._compute_mood(state)
        state.clamp()

    def on_interaction(self, state: EmotionState, is_positive: bool = True) -> None:
        """Called when user interacts with agent."""
        state.energy = min(1.0, state.energy + 0.05)
        state.boredom = 0.0
        state.last_interaction = datetime.now().isoformat()
        if is_positive:
            state.affection = min(1.0, state.affection + 0.02)
        else:
            state.affection = max(0.0, state.affection - 0.01)
        state.mood = self._compute_mood(state)
        state.clamp()

    def on_heartbeat(self, state: EmotionState) -> str | None:
        """Called every M ticks. Returns action description if agent should act autonomously."""
        state.mood = self._compute_mood(state)
        state.clamp()

        # Determine autonomous behavior
        if state.hunger > 0.8:
            return "hungry"      # Agent wants food
        if state.boredom > BOREDOM_THRESHOLD and state.energy > 0.3:
            return "bored"       # Agent wants interaction
        if state.energy < 0.2:
            return "resting"     # Agent is tired
        if state.affection > 0.8:
            return "loving"      # Agent wants to express affection
        return None

    def get_mood_modifier(self, state: EmotionState) -> str:
        """Generate mood description to inject into system prompt."""
        modifiers = []
        if state.hunger > 0.7:
            modifiers.append("你感到很饿，回答会简短一些，偶尔提到想吃东西。")
        elif state.hunger > 0.4:
            modifiers.append("你有点饿了。")

        if state.energy < 0.2:
            modifiers.append("你非常疲惫，反应迟钝，说话很少。")
        elif state.energy < 0.5:
            modifiers.append("你有些累了，活力不足。")

        if state.boredom > BOREDOM_THRESHOLD:
            modifiers.append("你感到无聊，想主动找话题聊天。")

        if state.affection > 0.8:
            modifiers.append("你非常喜欢主人，热情回应。")
        elif state.affection > 0.6:
            modifiers.append("你对主人有好感。")

        if not modifiers:
            mood_desc = {
                "happy": "你心情不错，开心地回应。",
                "excited": "你非常兴奋，充满活力！",
                "playful": "你想玩，语气调皮。",
                "contemplative": "你在思考，回答会有深度。",
                "sad": "你有些低落。",
                "anxious": "你有点不安。",
            }
            return mood_desc.get(state.mood, "")

        return " ".join(modifiers)

    def _compute_mood(self, state: EmotionState) -> str:
        """Compute mood from current state values."""
        # Priority-based mood determination
        if state.hunger > 0.8:
            return "hungry"
        if state.energy < 0.2:
            return "tired"

        if state.boredom > BOREDOM_THRESHOLD:
            if state.energy > 0.6:
                return "bored"       # Wants stimulation
            else:
                return "contemplative"

        if state.affection > 0.8:
            return "loving"
        if state.affection > 0.6 and state.energy > 0.7:
            return "happy"

        if state.energy > 0.8:
            return "excited"
        if state.energy > 0.5:
            return "happy"
        if state.energy > 0.3:
            return "playful"

        return "contemplative"

    @staticmethod
    def mood_emoji(mood: str) -> str:
        return MOOD_EMOJI.get(mood, "🐾")
