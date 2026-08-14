"""SubgraphPattern — unified contract for all agent execution modes.

A "pattern" is a named, self-contained execution graph that the supervisor
can delegate to via the Handoff Protocol. Patterns come in two categories:

  * basic       — single-agent inner loops (chat / tool_react / rag / plan_execute)
  * multi_agent — coordination topologies (debate / map-reduce / team / ...)

Every pattern — whether fully implemented or stubbed — implements this ABC.
That lets the SubgraphRegistry treat all 11 modes uniformly, and the
mode_judge prompt builder to enumerate them generically.

Phase 2.6 — contract only; concrete patterns land in Phase 2.10-2.11.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from paradise.core.registry import SubgraphRegistry


class SubgraphPattern(ABC):
    """Base contract for every agent execution pattern.

    Subclasses MUST set the class attributes (`name`, `description`, etc.)
    and implement `build()`. The default `availability()` returns True —
    stubs override it to return False so the registry hides them from
    mode_judge.

    Class attributes (not instance fields) so patterns can be defined as
    lightweight singletons without __init__ boilerplate.
    """

    # ── Class-level metadata (set by subclass) ─────────────────────────
    name: ClassVar[str] = ""
    """Registry key. Must be unique across all registered patterns.
    Convention: snake_case identifier (e.g. "tool_react", "map_reduce")."""

    description: ClassVar[str] = ""
    """Human-readable purpose. Fed verbatim into mode_judge's prompt, so
    keep it short, specific, and bias-free (avoid words like "smart" or
    "advanced" — describe the trigger condition instead)."""

    category: ClassVar[str] = "basic"
    """One of "basic" or "multi_agent". Used for grouping in dashboards
    and for default cost/turn budgets."""

    cost_budget_usd: ClassVar[float] = 0.05
    """Per-invocation cost cap. Subgraph SHOULD check accumulated cost
    and exit with HANDOFF_COST_EXCEEDED when exceeded. Supervisor
    enforces this as a hard ceiling regardless of subgraph compliance."""

    max_turns: ClassVar[int] = 5
    """Inner-loop iteration cap. Subgraph exits HANDOFF_MAX_TURNS when
    reached. Supervisor treats as soft failure."""

    # ── Hooks ──────────────────────────────────────────────────────────

    @abstractmethod
    def build(self, registry: "SubgraphRegistry") -> Any:
        """Construct and return the compiled LangGraph (or compatible
        runnable) for this pattern.

        Args:
            registry: read-only access to sibling patterns. Multi-agent
                patterns use this to fetch inner-agent subgraphs (e.g.,
                a debate pattern fetches two `chat` subgraphs as debaters).
                Basic patterns typically ignore this argument.

        Returns:
            Compiled graph. Must accept an initial state containing a
            `handoff: HandoffRequest` field, and emit a final state with
            `handoff_response: HandoffResponse`.

        Implementations should be idempotent: building twice with the
        same registry must yield equivalent graphs.
        """
        raise NotImplementedError

    def availability(self) -> bool:
        """Whether this pattern can be selected by mode_judge.

        Returns:
            True if the pattern is fully implemented and ready.
            False for stubs — registry hides them from mode_judge, and
            supervisor falls back to "chat" if forced.

        Default: True. Override in stub patterns.
        """
        return True

    def describe(self) -> str:
        """One-line description used by registry.build_mode_prompt().
        Defaults to f"{self.name}: {self.description}". Stubs append
        "（接口预留，未启用）" via override.
        """
        return f"{self.name}: {self.description}"

    # Convenience for registry / tests
    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} name={self.name!r} "
            f"category={self.category!r} available={self.availability()}>"
        )


__all__ = ["SubgraphPattern"]
