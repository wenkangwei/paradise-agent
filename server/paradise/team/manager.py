"""TeamManager and AgentTeam — multi-agent coordination for Paradise.

Combines hermes-style delegate model with a global team registry.
Teams share a Channel for inter-agent communication and can broadcast,
delegate tasks, or vote on decisions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter

from paradise.core.agent import ParadiseAgent
from paradise.core.channel import Channel, ChannelMessage
from paradise.core.context import LoopContext

logger = logging.getLogger(__name__)


class AgentTeam:
    """A named group of agents that can coordinate on tasks.

    Each team owns a shared Channel that all members use for
    inter-agent messaging.  Delegation creates an independent
    LoopContext so the sub-agent runs with its own state.
    """

    def __init__(self, team_id: str) -> None:
        self.team_id = team_id
        self.members: dict[str, ParadiseAgent] = {}
        self.channel: Channel = Channel(
            channel_type="plaza",
            participants=[],
        )
        self.shared_context: dict = {}
        self.created_at: float = time.monotonic()

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def add_member(self, agent: ParadiseAgent) -> None:
        """Register an agent as a team member."""
        if agent.agent_id in self.members:
            logger.warning("[Team:%s] %s already a member", self.team_id, agent.agent_id)
            return
        self.members[agent.agent_id] = agent
        # Also register as a channel participant
        self.channel.participants.append({
            "id": agent.agent_id,
            "name": agent.agent_id,
            "role": "agent",
        })
        logger.info("[Team:%s] added member %s", self.team_id, agent.agent_id)

    def remove_member(self, agent_id: str) -> None:
        """Remove a member by agent_id."""
        self.members.pop(agent_id, None)
        self.channel.participants = [
            p for p in self.channel.participants if p["id"] != agent_id
        ]
        logger.info("[Team:%s] removed member %s", self.team_id, agent_id)

    def get_member(self, agent_id: str) -> ParadiseAgent | None:
        """Look up a member by agent_id."""
        return self.members.get(agent_id)

    def list_members(self) -> list[str]:
        """Return list of member agent IDs."""
        return list(self.members.keys())

    # ------------------------------------------------------------------
    # Communication
    # ------------------------------------------------------------------

    async def broadcast(
        self,
        message: str,
        from_agent_id: str | None = None,
        exclude: set[str] | None = None,
    ) -> dict[str, str]:
        """Send a message to all team members (except excluded).

        Returns a dict mapping agent_id -> response text.
        """
        exclude = exclude or set()
        if from_agent_id:
            exclude.add(from_agent_id)

        # Record the broadcast in the shared channel
        sender_name = from_agent_id or "team"
        self.channel.add_message(sender_name, message, sender_name=sender_name)

        tasks: dict[str, asyncio.Task[str]] = {}
        for aid, agent in self.members.items():
            if aid in exclude:
                continue
            tasks[aid] = asyncio.create_task(
                self._query_agent(agent, message, from_agent_id or "team"),
            )

        results: dict[str, str] = {}
        for aid, task in tasks.items():
            try:
                results[aid] = await task
            except Exception as exc:
                logger.error("[Team:%s] broadcast to %s failed: %s", self.team_id, aid, exc)
                results[aid] = f"[error] {exc}"

        return results

    async def delegate(
        self,
        parent_id: str,
        target_id: str,
        task: str,
        max_iterations: int = 50,
    ) -> str:
        """Parent agent delegates a task to a specific team member.

        Creates an independent LoopContext for the sub-agent so it runs
        with its own session and channel scope.
        """
        target = self.members.get(target_id)
        if target is None:
            raise ValueError(f"Agent {target_id} is not a member of team {self.team_id}")

        logger.info(
            "[Team:%s] delegate: %s -> %s | task=%s",
            self.team_id, parent_id, target_id, task[:60],
        )

        # Build an independent context for the sub-agent
        ctx = LoopContext(
            agent_id=target_id,
            agent_name=target_id,
            session_id=f"delegate-{parent_id}-{target_id}-{int(time.time())}",
            user_message=task,
            channel=self.channel,
            enable_tools=True,
        )

        # Collect the full response text from the sub-agent
        chunks: list[str] = []
        async for event in target.handle_message(ctx):
            if event.get("type") == "content":
                chunks.append(event["content"])
            elif event.get("type") == "error":
                logger.error("[Team:%s] delegate error: %s", self.team_id, event["content"])
                break

        result = "".join(chunks)
        self.shared_context[f"delegate:{parent_id}:{target_id}"] = result
        return result

    async def vote(
        self,
        question: str,
        options: list[str],
        quorum: float = 0.5,
    ) -> str:
        """Multi-agent voting.  Each agent gives an opinion, majority wins.

        *quorum* is the minimum fraction of members that must respond.
        If quorum is not met the first option is returned as a fallback.
        """
        prompt = (
            f"[投票] {question}\n"
            f"可选选项: {', '.join(options)}\n"
            f"请只回复一个选项名称，不要解释。"
        )

        responses = await self.broadcast(prompt)

        if len(responses) < len(self.members) * quorum:
            logger.warning("[Team:%s] vote quorum not met: %d/%d",
                           self.team_id, len(responses), len(self.members))
            return options[0] if options else ""

        # Tally votes — normalize to match option list
        tally = Counter()
        for text in responses.values():
            pick = text.strip()
            # Fuzzy match: accept partial match against known options
            for opt in options:
                if opt in pick or pick in opt:
                    pick = opt
                    break
            tally[pick] += 1

        winner = tally.most_common(1)[0][0]
        logger.info("[Team:%s] vote result: %s (tally=%s)", self.team_id, winner, tally)
        return winner

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _query_agent(
        self, agent: ParadiseAgent, message: str, from_id: str,
    ) -> str:
        """Send a single message to one agent and return the full response."""
        ctx = LoopContext(
            agent_id=agent.agent_id,
            agent_name=agent.agent_id,
            session_id=f"team-{self.team_id}-{int(time.time())}",
            user_message=message,
            channel=self.channel,
            enable_tools=False,
        )
        chunks: list[str] = []
        async for event in agent.handle_message(ctx):
            if event.get("type") == "content":
                chunks.append(event["content"])
        return "".join(chunks)


class TeamManager:
    """Global manager for all agent teams.

    Usage::

        tm = TeamManager()
        team = tm.create_team("squad-1")
        team.add_member(agent_a)
        team.add_member(agent_b)
        await team.broadcast("Hello team!")
    """

    def __init__(self) -> None:
        self._teams: dict[str, AgentTeam] = {}
        self._default_team_id: str | None = None

    def create_team(self, team_id: str, agent_ids: list[str] | None = None) -> AgentTeam:
        """Create a new team.  Optionally pre-register agent IDs.

        Note: agent_ids are stored as names only; actual ParadiseAgent
        instances must be added via ``team.add_member()`` later.
        """
        if team_id in self._teams:
            raise ValueError(f"Team {team_id} already exists")
        team = AgentTeam(team_id)
        self._teams[team_id] = team
        logger.info("[TeamManager] created team %s", team_id)
        return team

    def get_team(self, team_id: str) -> AgentTeam | None:
        """Look up a team by ID."""
        return self._teams.get(team_id)

    def get_or_create(self, team_id: str) -> AgentTeam:
        """Return an existing team or create a new one."""
        if team_id not in self._teams:
            self._teams[team_id] = AgentTeam(team_id)
            logger.info("[TeamManager] auto-created team %s", team_id)
        return self._teams[team_id]

    def dissolve_team(self, team_id: str) -> None:
        """Remove a team and clear its shared state."""
        team = self._teams.pop(team_id, None)
        if team:
            team.members.clear()
            team.shared_context.clear()
            if self._default_team_id == team_id:
                self._default_team_id = None
            logger.info("[TeamManager] dissolved team %s", team_id)

    def list_teams(self) -> list[str]:
        """Return all registered team IDs."""
        return list(self._teams.keys())

    def get_default_team(self) -> AgentTeam | None:
        """Return the default team, if one is set."""
        if self._default_team_id is None:
            return None
        return self._teams.get(self._default_team_id)

    def set_default_team(self, team_id: str) -> None:
        """Set the default team.  Raises KeyError if team does not exist."""
        if team_id not in self._teams:
            raise KeyError(f"Team {team_id} not found")
        self._default_team_id = team_id
        logger.info("[TeamManager] default team set to %s", team_id)
