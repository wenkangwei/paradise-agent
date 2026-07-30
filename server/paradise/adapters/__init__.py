"""Paradise channel adapters — unified messaging platform interface.

Registry for ChannelAdapter implementations (SSE, Feishu, Webhook, etc.).
Pattern mirrors paradise.transports: register_adapter() + auto-discover.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paradise.adapters.base import ChannelAdapter

logger = logging.getLogger(__name__)

# Global registry: name → adapter class
_ADAPTER_REGISTRY: dict[str, type[ChannelAdapter]] = {}

# Active instances: name → adapter instance
_ADAPTER_INSTANCES: dict[str, ChannelAdapter] = {}


def register_adapter(name: str, cls: type[ChannelAdapter]) -> None:
    """Register a channel adapter class under a given name."""
    _ADAPTER_REGISTRY[name] = cls
    logger.debug("Registered adapter: %s → %s", name, cls.__name__)


def get_adapter(name: str) -> ChannelAdapter | None:
    """Get or create an adapter instance by name."""
    if name in _ADAPTER_INSTANCES:
        return _ADAPTER_INSTANCES[name]
    cls = _ADAPTER_REGISTRY.get(name)
    if not cls:
        return None
    instance = cls()
    _ADAPTER_INSTANCES[name] = instance
    return instance


def available_adapters() -> list[str]:
    """Return list of registered adapter names."""
    return sorted(_ADAPTER_REGISTRY.keys())


def all_active() -> list[ChannelAdapter]:
    """Return all instantiated (active) adapter instances."""
    return list(_ADAPTER_INSTANCES.values())


# ── Auto-import concrete adapters ──────────────────────────────────

def _discover_adapters() -> None:
    """Import all adapter modules to trigger self-registration."""
    try:
        import paradise.adapters.sse  # noqa: F401
    except Exception as e:
        logger.warning("Failed to import SSE adapter: %s", e)
    try:
        import paradise.adapters.feishu  # noqa: F401
    except Exception as e:
        logger.debug("Feishu adapter not available: %s", e)
    try:
        import paradise.adapters.webhook  # noqa: F401
    except Exception as e:
        logger.warning("Failed to import Webhook adapter: %s", e)


_discover_adapters()
