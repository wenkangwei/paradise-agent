"""C++ kernel intent strategy (L4 — acceleration tier, stub).

PLACEHOLDER. The real implementation will load a pybind11 .so exposing a
function like:

    int classify(const std::string& message, double* confidence) noexcept;

compiled from `cpp/intent_classifier.cpp` (TODO W3.5). The kernel will use:
  - Aho-Corasick DFA over compiled rule patterns (~5µs / 1k patterns)
  - Pre-computed embedding dot-product (if a small static cache is loaded)

Until the .so exists, this strategy is inert: `is_available()` returns False
and `classify()` returns None so the cascade skips it gracefully. The
production wiring already treats None as "fall through" — no code change
will be needed once the kernel lands.

Build contract (future)
-----------------------
$ cd cpp && mkdir build && cd build
$ cmake -DCMAKE_BUILD_TYPE=Release ..
$ make -j
# produces paradise_intent_ext.so → install to paradise/core/intent/
"""
from __future__ import annotations

import logging
import importlib.util
from pathlib import Path
from typing import Optional

from paradise.core.intent.base import Intent, IntentResult, IntentStrategy

logger = logging.getLogger(__name__)

_SO_NAME = "paradise_intent_ext.so"
_SO_SEARCH_PATHS = (
    Path(__file__).parent,
    Path(__file__).parent.parent.parent.parent / "cpp" / "build",
)


def _find_kernel() -> Optional[object]:
    """Locate and load the C++ extension if present. None otherwise."""
    for search_dir in _SO_SEARCH_PATHS:
        candidate = search_dir / _SO_NAME
        if candidate.exists():
            try:
                spec = importlib.util.spec_from_file_location(
                    "paradise_intent_ext", candidate
                )
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                logger.info("C++ intent kernel loaded from %s", candidate)
                return module
            except Exception as exc:
                logger.warning(
                    "C++ intent kernel present but failed to load (%s); "
                    "falling back to L1-L3 cascade", exc
                )
                return None
    return None


class CppStrategy(IntentStrategy):
    """L4 — pybind11 kernel placeholder.

    Contract (when .so is built)
    ----------------------------
    `kernel.classify(message: str) -> tuple[int, double]` where the int is the
    zero-based index into `Intent._member_names_` and the double is confidence
    in [0, 1]. Returning confidence < 0 means "no opinion" (cascade continues).

    Until then, this class is a no-op so the orchestrator wires it
    unconditionally and gets transparent fallback.
    """

    name = "cpp"

    def __init__(self) -> None:
        self._kernel = _find_kernel()
        if self._kernel is None:
            logger.info("C++ intent kernel not built — L4 disabled (stub)")

    def is_available(self) -> bool:
        """True iff the compiled kernel is loaded and ready."""
        return self._kernel is not None and hasattr(self._kernel, "classify")

    async def classify(self, message: str) -> Optional[IntentResult]:
        if not message or not message.strip():
            return None
        if self._kernel is None:
            return None
        # Forward call to the synchronous kernel. Wrap in run_in_executor at
        # the call site if it ever blocks; current kernel design is <1ms.
        try:
            idx, conf = self._kernel.classify(message)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.debug("C++ kernel raised (%s); skipping", exc)
            return None
        if conf < 0:
            # Kernel's "no opinion" signal — let the cascade continue.
            return None
        try:
            members = list(Intent)
            intent = members[int(idx)]
        except (IndexError, ValueError):
            logger.warning("C++ kernel returned bad intent idx=%r", idx)
            return None
        return IntentResult(
            intent=intent,
            confidence=float(conf),
            source=self.name,
            latency_ms=0.0,  # kernel self-reports if needed via meta
            meta={"kernel": _SO_NAME},
        )

    async def warmup(self) -> None:
        """Pre-load DFA tables / embeddings if the kernel exposes it."""
        if self._kernel is None:
            return
        warm = getattr(self._kernel, "warmup", None)
        if callable(warm):
            try:
                warm()
                logger.info("C++ intent kernel warmed up")
            except Exception as exc:
                logger.warning("C++ warmup failed (%s); kernel still usable", exc)
