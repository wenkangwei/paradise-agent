"""SubgraphRegistry — static registry of agent execution patterns.

The registry is populated once at startup (see paradise.core.patterns.register_all)
and treated read-only thereafter. It serves three consumers:

  1. Supervisor.handoff_node  → registry.get(mode).compiled
  2. mode_judge LLM           → registry.build_mode_prompt()
  3. Observability dashboards → registry.list_modes() for grouping

Design choices:

  * Static, not dynamic. Pattern plugins at runtime are an anti-pattern
    for a single-machine deployment — they complicate observability and
    make cost budgets non-deterministic. If a new pattern is needed,
    ship it via a code change + redeploy.

  * Lazy compilation. SubgraphSpec stores the pattern object, not a
    pre-compiled graph. compile() is invoked on first get() (memoized).
    This avoids building 11 graphs at startup when only a handful will
    be used in practice, and it lets multi-agent patterns resolve their
    inner-agent references through the registry at compile time.

  * Thread-safe enough for asyncio. The compile cache uses a plain dict
    guarded by a lock; under LangGraph's async model this is sufficient
    (no true parallelism in a single Python process for CPU work).

Phase 2.6 — registry mechanics only; pattern registration lives in
paradise.core.patterns.__init__.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from paradise.core.patterns.base import SubgraphPattern


@dataclass
class SubgraphSpec:
    """Resolved metadata for a registered pattern.

    `compiled` is populated lazily on first access via the registry's
    compile cache; at registration time it is None.

    Attributes:
        name:           Registry key (matches pattern.name).
        pattern:        The SubgraphPattern instance.
        state_schema:   TypedDict (or dict-like) used as the subgraph's
                        state. Stays None until compile() is called.
        compiled:       Compiled graph. None until first get().
        description:    Forwarded from pattern for prompt building.
        category:       "basic" | "multi_agent" — for dashboards.
    """
    name: str
    pattern: "SubgraphPattern"
    state_schema: type | None = None
    compiled: Any = None
    description: str = ""
    category: str = "basic"


class SubgraphRegistry:
    """In-memory registry of agent execution patterns.

    Lifecycle:
        registry = SubgraphRegistry()
        register_all(registry)        # populate from paradise.core.patterns
        registry.get("chat").compiled  # lazy compile on first access

    Thread-safety: registration is meant to happen single-threaded at
    startup. Compile cache is guarded for the rare case where two
    coroutines hit get() concurrently for the same pattern.
    """

    def __init__(self) -> None:
        self._patterns: dict[str, SubgraphSpec] = {}
        self._compile_lock = threading.Lock()

    # ── Registration ───────────────────────────────────────────────────

    def register(self, pattern: "SubgraphPattern") -> None:
        """Add a pattern to the registry.

        Raises:
            ValueError: if a pattern with the same name is already
                registered. Duplicate registration is almost always a bug
                (e.g., two modules both calling register(StubPattern("debate",...))).
        """
        name = pattern.name
        if not name:
            raise ValueError(
                f"Cannot register pattern with empty name: {pattern!r}"
            )
        if name in self._patterns:
            raise ValueError(
                f"Pattern {name!r} already registered "
                f"(existing: {self._patterns[name].pattern!r}, "
                f"new: {pattern!r})"
            )
        self._patterns[name] = SubgraphSpec(
            name=name,
            pattern=pattern,
            description=pattern.describe(),
            category=pattern.category,
        )

    # ── Lookup ─────────────────────────────────────────────────────────

    def get(self, name: str) -> SubgraphSpec | None:
        """Return the spec for `name`, or None if not registered.

        On first access for a given name, triggers lazy compile of the
        pattern's graph — but ONLY if the pattern is available. Stub
        patterns (availability()=False) are never compiled: they'd hit
        a fallback that may not exist yet (e.g., during Phase 2.7 when
        `chat` itself is a stub, compiling it would recurse infinitely).
        Callers should treat spec.compiled is None as "pattern not
        runnable, supervisor must fall back".
        """
        spec = self._patterns.get(name)
        if spec is None:
            return None
        if spec.compiled is None and spec.pattern.availability():
            self._compile_locked(spec)
        return spec

    def list_modes(self, include_unavailable: bool = False) -> list[str]:
        """Return pattern names.

        Args:
            include_unavailable: When False (default), excludes stub
                patterns (availability()=False). This is what mode_judge
                should see — it cannot select a stub. When True, returns
                all registered names — used by dashboards and admin tools.
        """
        return [
            name
            for name, spec in self._patterns.items()
            if include_unavailable or spec.pattern.availability()
        ]

    def build_mode_prompt(self) -> str:
        """Build the prompt fragment listing available modes for mode_judge.

        Format:
            Available modes:
            - chat: single-shot LLM reply for chitchat / simple Q&A
            - tool_react: ReAct loop for tool-using requests
            ...

        Unavailable patterns are omitted so the LLM cannot select a stub.
        """
        lines = ["Available modes:"]
        for name in self.list_modes(include_unavailable=False):
            spec = self._patterns[name]
            # Use the spec's stored description (computed at registration)
            # rather than re-calling pattern.describe(), to keep prompts
            # stable even if someone mutates pattern state later.
            lines.append(f"- {spec.description}")
        return "\n".join(lines)

    # ── Iteration / introspection ─────────────────────────────────────

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._patterns

    def __len__(self) -> int:
        return len(self._patterns)

    def __iter__(self) -> Iterator[SubgraphSpec]:
        return iter(self._patterns.values())

    # ── Internal: lazy compile ────────────────────────────────────────

    def _compile_locked(self, spec: SubgraphSpec) -> None:
        """Compile a pattern's graph under a lock (memoized).

        Failures are non-fatal: if a pattern's build() raises, we leave
        spec.compiled = None and let the caller decide. The supervisor
        treats compiled=None as "fall back to chat" rather than crashing.
        """
        # Double-checked locking: another coroutine may have compiled
        # while we waited for the lock.
        if spec.compiled is not None:
            return
        with self._compile_lock:
            if spec.compiled is not None:
                return
            try:
                spec.compiled = spec.pattern.build(self)
            except Exception:
                # Logged at supervisor level; here we just leave compiled=None
                # so a fallback can be attempted. Don't re-raise — the
                # registry is meant to be resilient.
                spec.compiled = None


__all__ = ["SubgraphSpec", "SubgraphRegistry"]
