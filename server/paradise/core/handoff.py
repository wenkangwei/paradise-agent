"""Handoff Protocol — supervisor ↔ subgraph data contract.

The supervisor does NOT read a subgraph's internal state. Communication is
strictly via these two dataclasses:

    HandoffRequest  → sent from supervisor to subgraph entry node
    HandoffResponse ← emitted by subgraph terminal node, read by supervisor

Why a strict contract:
  * Future replacement of any subgraph (LangGraph → raw asyncio loop, or
    rewrite into another framework) is possible without touching supervisor.
  * Subgraph internal state schema can evolve freely; only this payload
    must remain stable.
  * Simplifies ATIF export — the trace recorder only needs to walk
    handoff_chain, not understand subgraph internals.

Phase 2.6 — pure data classes, zero business logic, zero deps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Status strings — kept as constants rather than an Enum so they survive
# JSON serialization through LangGraph checkpointer / ATIF export without
# custom encoder boilerplate.
HANDOFF_OK = "ok"
HANDOFF_COST_EXCEEDED = "cost_exceeded"
HANDOFF_MAX_TURNS = "max_turns"
HANDOFF_DEPTH_CAPPED = "depth_capped"
HANDOFF_ERROR = "error"
HANDOFF_CANCELLED = "cancelled"


@dataclass
class HandoffRequest:
    """Payload sent from supervisor → subgraph.

    Intentionally minimal: only what a subgraph needs to do its job, NOT
    the supervisor's full AgentState. Add fields here only when a real
    cross-cutting need arises, otherwise push into `context` dict.

    Attributes:
        session_id:    Stable session identifier (same as supervisor session).
        user_id:       User identity for tier/quota lookups.
        trace_id:      OTel-style trace ID — joins all spans in the turn.
        user_tier:     "free" | "pro" | ... — affects cost/turn caps.
        message:       The user's verbatim message for this turn.
        context:       Minimal handoff context. Reserved keys:
                       - "facts": list[str]            persistent facts
                       - "last_output": str           prior turn's output
                       - "artifacts": dict[str, Any]  structured carry-over
                       Subgraphs may read/add keys but MUST NOT remove
                       keys they didn't add.
        depth:         Nesting depth of this invocation. Supervisor is 0;
                       direct subgraphs are 1; nested subgraphs are 2.
                       Enforced ≤ MAX_DEPTH at supervisor.handoff_node.
        parent_trace_id: Trace ID of the parent invocation if depth > 0;
                       None when supervisor hands off to a top-level subgraph.
        deadline_ms:   Soft wall-clock budget for this invocation. Subgraphs
                       SHOULD check this between turns and exit HANDOFF_MAX_TURNS
                       (or a dedicated budget-exceeded status) when exceeded.
                       0 means "use default from SubgraphSpec".
    """
    session_id: str
    user_id: str
    trace_id: str
    user_tier: str = "free"
    message: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    depth: int = 1
    parent_trace_id: str | None = None
    deadline_ms: int = 0


@dataclass
class HandoffResponse:
    """Payload emitted by subgraph terminal node → supervisor.

    Attributes:
        session_id, trace_id: echo of request for correlation.
        output:                Primary text output shown to user.
        artifacts:             Structured results. Reserved keys:
                               - "tool_results": list[dict]
                               - "plan_steps":   list[dict]
                               - "retrieved":    list[dict]   (rag)
                               Subgraph-specific keys OK under namespaces.
        cost_incurred_usd:     Actual cost burned by this subgraph invocation
                               (sum of LLM + tool costs). Used by supervisor
                               to accumulate per-turn cost.
        turns_used:            Inner turns consumed (ReAct iterations,
                               reflection rounds, etc.). Used for budget
                               enforcement and metrics.
        status:                One of HANDOFF_* constants. Supervisor treats
                               any non-ok status as soft failure (may still
                               show output to user, but logs/metrics reflect
                               the issue).
        error:                 Optional human-readable error string when
                               status != ok. Stack traces should NOT go here
                               — log them inside the subgraph instead.
    """
    session_id: str
    trace_id: str
    output: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    cost_incurred_usd: float = 0.0
    turns_used: int = 0
    status: str = HANDOFF_OK
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when status == HANDOFF_OK (the common happy path)."""
        return self.status == HANDOFF_OK


class HandoffError(Exception):
    """Raised by subgraphs when a structured failure cannot be represented
    as a HandoffResponse (e.g., entry node contract violation).

    Supervisor catches this and synthesises a HANDOFF_ERROR response,
    so end users see graceful degradation rather than 5xx.

    Subgraph authors SHOULD prefer returning a HandoffResponse with
    status=HANDOFF_ERROR; reserve raising HandoffError for unrecoverable
    contract violations (e.g., missing required input field).
    """


__all__ = [
    "HandoffRequest",
    "HandoffResponse",
    "HandoffError",
    "HANDOFF_OK",
    "HANDOFF_COST_EXCEEDED",
    "HANDOFF_MAX_TURNS",
    "HANDOFF_DEPTH_CAPPED",
    "HANDOFF_ERROR",
    "HANDOFF_CANCELLED",
]
