"""HeartbeatDecision — dataclass for heartbeat decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import logging

logger = logging.getLogger(__name__)


class HeartbeatAction(Enum):
    SILENT = "SILENT"
    PLAZA = "PLAZA"
    MENTION = "MENTION"
    USER = "USER"


@dataclass
class HeartbeatDecision:
    action: HeartbeatAction = HeartbeatAction.SILENT
    target: str = ""
    message: str = ""


def parse_decision(raw: str) -> HeartbeatDecision:
    """Parse LLM response into a HeartbeatDecision."""
    decision = HeartbeatDecision()

    # Extract <decision> tag
    m = re.search(r"<decision>\s*(\w+)\s*</decision>", raw, re.IGNORECASE)
    if m:
        try:
            decision.action = HeartbeatAction(m.group(1).upper())
        except ValueError:
            decision.action = HeartbeatAction.SILENT

    # Extract <target> tag
    m = re.search(r"<target>\s*(.*?)\s*</target>", raw, re.IGNORECASE | re.DOTALL)
    if m:
        decision.target = m.group(1).strip()

    # Extract <message> tag
    m = re.search(r"<message>\s*(.*?)\s*</message>", raw, re.IGNORECASE | re.DOTALL)
    if m:
        decision.message = m.group(1).strip()

    return decision
