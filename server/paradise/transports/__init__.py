"""Transport registry — auto-discovers and registers transport implementations."""

from __future__ import annotations

import logging
from typing import Dict, Type

from paradise.transports.base import ProviderTransport
from paradise.config import LLMConfig
from paradise.exceptions import TransportNotFoundError

logger = logging.getLogger(__name__)

# Global registry: api_mode -> transport class
_TRANSPORTS: Dict[str, Type[ProviderTransport]] = {}


def register_transport(api_mode: str, cls: Type[ProviderTransport]) -> None:
    """Register a transport class for an api_mode."""
    _TRANSPORTS[api_mode] = cls
    logger.debug("Registered transport: %s -> %s", api_mode, cls.__name__)


def get_transport(api_mode: str) -> ProviderTransport:
    """Get a transport instance by api_mode. Raises TransportNotFoundError if not found."""
    cls = _TRANSPORTS.get(api_mode)
    if not cls:
        raise TransportNotFoundError(api_mode)
    return cls()


def resolve_transport(config: LLMConfig) -> ProviderTransport:
    """Resolve a LLMConfig to a Transport instance."""
    api_mode = config.resolved_api_mode
    return get_transport(api_mode)


def available_transports() -> list[str]:
    """Return list of registered api_mode strings."""
    return sorted(_TRANSPORTS.keys())


# ── Auto-import concrete transports ──

def _discover_transports() -> None:
    """Import all transport modules to trigger registration."""
    try:
        import paradise.transports.openai_compat  # noqa: F401
    except Exception as e:
        logger.warning("Failed to import openai_compat transport: %s", e)
    try:
        import paradise.transports.anthropic  # noqa: F401
    except Exception as e:
        logger.warning("Failed to import anthropic transport: %s", e)
    try:
        import paradise.transports.ollama_native  # noqa: F401
    except Exception as e:
        logger.warning("Failed to import ollama_native transport: %s", e)


_discover_transports()
