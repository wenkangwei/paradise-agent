"""DelegationTool -- delegate_task for Paradise multi-agent delegation.

Reference: hermes-agent tools/delegate_tool.py pattern.

Parent agent calls this tool to spawn a child agent for a specific sub-task.
The child runs with an isolated LoopContext (no parent history leaked) and
returns a structured DelegationResult.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from paradise.core.agent import ParadiseAgent
from paradise.core.context import LoopContext
from paradise.team.manager import AgentTeam

logger = logging.getLogger(__name__)

# --- LLM tool-calling schema ---

DELEGATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "将任务委托给另一个agent执行。适合将复杂任务分解为子任务。"
            " 子agent拥有独立的上下文，不会看到父agent的历史对话。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "要委托的任务描述（需自包含，子agent不了解父对话历史）",
                },
                "target_agent_id": {
                    "type": "string",
                    "description": "目标agent ID（可选，不指定则自动创建临时子agent）",
                },
                "role": {
                    "type": "string",
                    "enum": ["leaf", "orchestrator"],
                    "description": (
                        "子agent角色。leaf(默认)=不可继续委托；"
                        "orchestrator=可继续委托给更下级agent"
                    ),
                },
            },
            "required": ["task_description"],
        },
    },
}


# --- Result dataclass ---

@dataclass
class DelegationResult:
    """Result of a delegation call."""

    task_id: str
    agent_id: str
    response: str
    success: bool
    error: str = ""
    iterations_used: int = 0
    duration_seconds: float = 0.0


# --- Delegation tool ---

class DelegationTool:
    """delegate_task -- parent agent delegates a sub-task to another agent.

    1. Selects/creates a child agent (or picks from team members)
    2. Builds a focused system prompt for the sub-task
    3. Runs an independent agent loop with limited iterations
    4. Returns the structured result to the parent agent
    """

    def __init__(self, parent_agent: ParadiseAgent, team: AgentTeam | None = None):
        self.parent = parent_agent
        self.team = team

    async def delegate_task(
        self,
        task_description: str,
        target_agent_id: str | None = None,
        max_iterations: int = 50,
        role: str = "leaf",
        system_prompt_override: str | None = None,
    ) -> DelegationResult:
        """Execute a delegated task.

        1. Resolve target agent (team member or self)
        2. Build independent LoopContext (no parent history leaked)
        3. Run agent loop, collecting response
        4. Return structured result
        """
        task_id = f"del-{uuid.uuid4().hex[:8]}"
        start = time.monotonic()
        role = role or "leaf"

        # Resolve target agent
        agent = self._resolve_agent(target_agent_id)
        if agent is None:
            return DelegationResult(
                task_id=task_id,
                agent_id=target_agent_id or "",
                response="",
                success=False,
                error=f"Agent {target_agent_id} not found in team",
            )

        # Build independent context
        session_id = f"delegate-{self.parent.agent_id}-{agent.agent_id}-{int(time.time())}"
        ctx = LoopContext(
            agent_id=agent.agent_id,
            agent_name=agent.agent_id,
            session_id=session_id,
            user_message=task_description,
            channel=self.team.channel if self.team else None,
            soul_md=system_prompt_override or self._build_delegation_prompt(
                task_description, role,
            ),
            enable_tools=True,
        )

        # Run agent loop, collecting response
        iterations = 0
        chunks: list[str] = []
        try:
            async for event in agent.handle_message(ctx):
                iterations += 1
                if iterations > max_iterations:
                    logger.warning(
                        "[Delegation] max_iterations (%d) reached for %s",
                        max_iterations, task_id,
                    )
                    break
                if event.get("type") == "content":
                    chunks.append(event["content"])
                elif event.get("type") == "error":
                    logger.error(
                        "[Delegation] error from %s: %s",
                        agent.agent_id, event["content"],
                    )
        except Exception as exc:
            logger.exception("[Delegation] failed for task %s", task_id)
            return DelegationResult(
                task_id=task_id,
                agent_id=agent.agent_id,
                response="",
                success=False,
                error=str(exc),
                iterations_used=iterations,
                duration_seconds=round(time.monotonic() - start, 2),
            )

        response = "".join(chunks)
        return DelegationResult(
            task_id=task_id,
            agent_id=agent.agent_id,
            response=response,
            success=bool(response),
            error="" if response else "No response produced",
            iterations_used=iterations,
            duration_seconds=round(time.monotonic() - start, 2),
        )

    def _resolve_agent(self, target_agent_id: str | None) -> ParadiseAgent | None:
        """Find or default to an agent for delegation."""
        if target_agent_id and self.team:
            return self.team.get_member(target_agent_id)
        if target_agent_id is None:
            return self.parent
        return None

    def _build_delegation_prompt(self, task: str, role: str) -> str:
        """Build focused system prompt for the delegated task."""
        parts = [
            "你是一个专注的子agent，正在执行一个被委托的特定任务。",
            "",
            f"你的任务:\n{task}",
            "",
            "完成这个任务后，请提供清晰的总结，包括：",
            "- 你做了什么",
            "- 你发现或完成了什么",
            "- 遇到的任何问题",
            "",
            "简洁但完整 -- 你的回复将作为摘要返回给父agent。",
        ]
        if role == "orchestrator":
            parts.extend([
                "",
                "## 编排者角色",
                "你可以使用 delegate_task 工具将工作进一步委托给其他agent。",
                "请谨慎使用此能力：仅在子任务确实可以并行化时才委托。",
            ])
        return "\n".join(parts)
