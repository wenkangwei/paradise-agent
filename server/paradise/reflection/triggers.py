"""Trigger policy for reflection — when to trigger which reflection type."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class TriggerPolicy:
    post_turn: bool = True
    idle_minutes: float = 10.0
    idle_max_context_turns: int = 50

    def should_post_turn(self) -> bool:
        return self.post_turn

    def should_idle(self, idle_minutes: float) -> bool:
        return idle_minutes >= self.idle_minutes
