"""ChannelAdapter ABC — unified interface for all messaging platforms.

Reference: hermes-agent gateway/platforms/base.py BasePlatformAdapter.
Simplified to Paradise's needs: connect/disconnect/send/normalize.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"
    STICKER = "sticker"
    COMMAND = "command"


@dataclass
class InboundMessage:
    """Cross-platform normalized inbound message.

    Corresponds to hermes MessageEvent.
    """
    user_id: str
    content: str
    platform: str               # "sse", "feishu", "webhook"
    chat_id: str                # Session key (platform-side)
    chat_type: str = "dm"       # "dm", "group", "channel"
    message_type: str = "text"
    media_urls: list[str] = field(default_factory=list)
    reply_to: str | None = None
    user_name: str = ""
    raw_event: Any = None


@dataclass
class SendResult:
    """Result of an outbound send.

    Corresponds to hermes SendResult.
    """
    success: bool
    message_id: str = ""
    error: str = ""
    retryable: bool = False


class ChannelAdapter(ABC):
    """Base class for all messaging platform adapters.

    Reference: hermes-agent BasePlatformAdapter, simplified to:
    connect / disconnect / send / normalize four core methods.
    """

    name: str = ""  # "sse", "feishu", "webhook"

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._connected = False

    # ── Lifecycle ────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to the platform. Returns True if successful."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the platform."""

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Sending ──────────────────────────────────────────────────

    @abstractmethod
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> SendResult:
        """Send a message to the platform."""

    async def send_image(self, chat_id: str, image_path: str,
                         caption: str = "") -> SendResult:
        """Optional: send an image."""
        return SendResult(success=False, error="not supported")

    async def send_typing(self, chat_id: str) -> None:
        """Optional: show typing indicator."""
        pass

    # ── Inbound normalization ────────────────────────────────────

    @abstractmethod
    def normalize_event(self, raw_event: Any) -> InboundMessage | None:
        """Convert a platform-specific event to InboundMessage.

        Returns None if the event should be ignored.
        """

    # ── Webhook handler (for platforms that need it) ─────────────

    async def handle_webhook(self, request: Any) -> Any:
        """Optional: handle incoming webhook request.

        Returns a response object for the HTTP response.
        Default returns 404.
        """
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "webhook not supported"}, status_code=404)

    # ── Helpers ──────────────────────────────────────────────────

    def _mark_connected(self) -> None:
        self._connected = True

    def _mark_disconnected(self) -> None:
        self._connected = False
