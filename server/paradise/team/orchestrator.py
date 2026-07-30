"""Orchestrator -- multi-agent coordination strategies.

Provides parallel, sequential, fan-out-fan-in, and round-robin
orchestration patterns for ParadiseAgent teams.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

from paradise.core.agent import ParadiseAgent
from paradise.core.context import LoopContext

logger = logging.getLogger(__name__)


def _build_agent_context(
    agent: ParadiseAgent,
    user_message: str,
    session_id: str,
) -> LoopContext:
    """Build a LoopContext for an agent in an orchestrated flow."""
    return LoopContext(
        agent_id=agent.agent_id,
        agent_name=agent.agent_id,
        session_id=session_id,
        user_message=user_message,
        enable_tools=False,
    )


async def _collect_response(agent: ParadiseAgent, ctx: LoopContext) -> str:
    """Run a single agent and collect its full text response."""
    chunks: list[str] = []
    async for event in agent.handle_message(ctx):
        if event.get("type") == "content":
            chunks.append(event["content"])
        elif event.get("type") == "error":
            logger.error("[Orchestrator] agent %s error: %s", agent.agent_id, event["content"])
    return "".join(chunks)


class Orchestrator:
    """Orchestration strategies for multi-agent teams."""

    @staticmethod
    async def parallel(
        agents: list[ParadiseAgent],
        prompt: str,
        agent_name: str = "team",
        session_id: str = "",
        max_concurrent: int = 3,
    ) -> list[dict]:
        """Run all agents in parallel on the same prompt.

        Limited to *max_concurrent* simultaneous runs via a semaphore.
        Returns ``[{"agent_id": str, "response": str, "error": str | None}]``.
        """

        async def _run(agent: ParadiseAgent) -> dict:
            ctx = _build_agent_context(agent, prompt, session_id)
            ctx.agent_name = agent_name
            try:
                text = await _collect_response(agent, ctx)
                return {"agent_id": agent.agent_id, "response": text, "error": None}
            except Exception as exc:  # noqa: BLE001
                logger.exception("[Orchestrator] parallel error for %s", agent.agent_id)
                return {"agent_id": agent.agent_id, "response": "", "error": str(exc)}

        sem = asyncio.Semaphore(max_concurrent)

        async def _guarded(agent: ParadiseAgent) -> dict:
            async with sem:
                return await _run(agent)

        return list(await asyncio.gather(*[_guarded(a) for a in agents]))

    @staticmethod
    async def sequential(
        agents: list[ParadiseAgent],
        prompt: str,
        agent_name: str = "team",
        session_id: str = "",
    ) -> list[dict]:
        """Run agents sequentially, each receives the previous output.

        The first agent gets the original *prompt*.
        Subsequent agents receive ``"[前一位agent的回复]\\n\\n{original_prompt}"``.
        Returns ``[{"agent_id": str, "response": str}]``.
        """
        results: list[dict] = []
        current_prompt = prompt

        for agent in agents:
            ctx = _build_agent_context(agent, current_prompt, session_id)
            ctx.agent_name = agent_name
            try:
                text = await _collect_response(agent, ctx)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[Orchestrator] sequential error for %s", agent.agent_id)
                text = f"[error] {exc}"
            results.append({"agent_id": agent.agent_id, "response": text})
            current_prompt = f"[前一位agent的回复]\n{text}\n\n{prompt}"

        return results

    @staticmethod
    async def fan_out_fan_in(
        agents: list[ParadiseAgent],
        prompt: str,
        aggregator: ParadiseAgent,
        agent_name: str = "team",
        session_id: str = "",
    ) -> str:
        """Fan-out / fan-in (Mixture-of-Agents style).

        Fan-out: all agents process *prompt* in parallel.
        Fan-in: *aggregator* synthesises every result into one response.

        Returns the aggregator's final response text.
        """
        # --- Fan-out ---
        parallel_results = await Orchestrator.parallel(
            agents, prompt, agent_name=agent_name, session_id=session_id,
        )

        # Collect successful responses
        responses: list[str] = []
        for r in parallel_results:
            if r["response"]:
                responses.append(r["response"])
            elif r["error"]:
                logger.warning("[Orchestrator] fan-out: %s failed: %s", r["agent_id"], r["error"])

        # --- Fan-in ---
        numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(responses))
        agg_prompt = (
            "以下是多位agent对同一问题的回复，请综合各方观点，"
            "去重、修正错误，输出一份高质量的统一回复。\n\n"
            f"{numbered}\n\n原始问题：{prompt}"
        )

        ctx = _build_agent_context(aggregator, agg_prompt, session_id)
        ctx.agent_name = agent_name
        return await _collect_response(aggregator, ctx)

    @staticmethod
    async def round_robin(
        agents: list[ParadiseAgent],
        prompt: str,
        rounds: int = 2,
        agent_name: str = "team",
        session_id: str = "",
    ) -> list[dict]:
        """Plaza-style round robin: agents take turns responding.

        Each agent sees all previous responses in the conversation history.
        Returns ``[{"round": int, "agent_id": str, "response": str}]``.
        """
        history: list[str] = []
        results: list[dict] = []

        for rnd in range(rounds):
            for agent in agents:
                # Build context that includes prior turns
                if history:
                    prior = "\n".join(history)
                    current_prompt = f"[已有讨论]\n{prior}\n\n[当前问题]{prompt}"
                else:
                    current_prompt = prompt

                ctx = _build_agent_context(agent, current_prompt, session_id)
                ctx.agent_name = agent_name
                try:
                    text = await _collect_response(agent, ctx)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[Orchestrator] round_robin error for %s", agent.agent_id)
                    text = f"[error] {exc}"

                results.append({
                    "round": rnd + 1,
                    "agent_id": agent.agent_id,
                    "response": text,
                })
                history.append(f"{agent.agent_id}: {text}")

        return results
