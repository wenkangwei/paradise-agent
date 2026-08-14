"""StubPattern — placeholder pattern that registers but doesn't execute.

Used to reserve the 7 multi-agent pattern slots in the registry without
implementing their full execution logic. The mode_judge LLM never sees
stubs (they're filtered out by `availability()=False`), and any direct
invocation transparently falls through to a real pattern — usually
`chat`. This means:

  * The 11-pattern API surface is stable from day 1 (Phase 2.7).
  * Implementing a multi-agent pattern later is "just" filling in
    `build()` — no registry / supervisor / dashboard changes needed.
  * Operators can flip a stub to available by subclassing and overriding
    `availability()`, without touching registration code.

Phase 2.6 — generic stub class only; the 7 specific stubs are instantiated
in `patterns/__init__.py::register_all()` (Phase 2.7).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from paradise.core.patterns.base import SubgraphPattern

if TYPE_CHECKING:
    from paradise.core.registry import SubgraphRegistry


class StubPattern(SubgraphPattern):
    """Generic placeholder pattern.

    Example:
        registry.register(StubPattern(
            name="debate",
            description="N debater agents + 1 judge",
            fallback="chat",
        ))

    The stub instance is stateless beyond its constructor args, so it's
    safe to share/reuse across registries.
    """

    # category default — instances may override via constructor arg.
    # Basic patterns are typically stubbed only during Phase 2.7 (before
    # 2.10-2.11 implement them); multi_agent patterns remain stubs until
    # a concrete need arises.
    category: ClassVar[str] = "multi_agent"

    def __init__(
        self,
        name: str,
        description: str,
        fallback: str = "chat",
        category: str | None = None,
        cost_budget_usd: float = 0.05,
        max_turns: int = 5,
    ) -> None:
        if not name:
            raise ValueError("StubPattern requires a non-empty name")
        # ClassVar attributes can be shadowed by instance attributes —
        # this is the standard way to parameterise a class-based pattern.
        self.name = name
        self.description = description
        self.fallback = fallback
        # Allow per-instance category so Phase 2.7 can register the 4
        # basic-mode slots as stubs (category="basic") before their real
        # implementations land in Phase 2.10-2.11. Defaults to the class
        # default ("multi_agent") when None.
        self.category = category if category is not None else type(self).category
        self.cost_budget_usd = cost_budget_usd
        self.max_turns = max_turns

    def availability(self) -> bool:
        """Stubs always report unavailable. Override in subclass to enable."""
        return False

    def build(self, registry: "SubgraphRegistry") -> Any:
        """Return the fallback pattern's compiled graph.

        Even though mode_judge won't pick an unavailable pattern, build()
        must still return *something* runnable so the registry can be
        populated eagerly at startup. We delegate to the fallback pattern,
        which means a forced invocation degrades to a working mode rather
        than crashing.

        Raises:
            KeyError: if the fallback pattern itself isn't registered.
                This is a deployment-time misconfiguration and should
                surface loudly.
        """
        fallback_spec = registry.get(self.fallback)
        if fallback_spec is None:
            raise KeyError(
                f"StubPattern({self.name!r}) fallback {self.fallback!r} "
                f"is not registered"
            )
        return fallback_spec.compiled

    def describe(self) -> str:
        """Append the unavailable marker so dashboards / prompts are honest."""
        return f"{self.name}: {self.description}（接口预留，未启用）"


__all__ = ["StubPattern"]
