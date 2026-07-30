"""Webhook channel adapter — generic HTTP POST inbound / callback outbound.

Env vars:
    WEBHOOK_SECRET           — optional shared secret for HMAC verification
    WEBHOOK_DEFAULT_CALLBACK — fallback callback URL when none is registered
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

import httpx

from paradise.adapters.base import ChannelAdapter, InboundMessage, SendResult

logger = logging.getLogger(__name__)


def _sign(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class WebhookAdapter(ChannelAdapter):
    """Generic webhook adapter for Paradise."""

    name = "webhook"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._callbacks: dict[str, str] = {}
        self._secret: str = os.getenv("WEBHOOK_SECRET", "")
        self._default_callback: str = os.getenv("WEBHOOK_DEFAULT_CALLBACK", "")

    # ── Lifecycle ────────────────────────────────────────────────

    async def connect(self) -> bool:
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._callbacks.clear()
        self._mark_disconnected()

    # ── Sending ──────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str,
                   reply_to: str | None = None, **kwargs) -> SendResult:
        url = self._callbacks.get(chat_id, self._default_callback)
        if not url:
            return SendResult(success=False,
                              error=f"no callback URL for {chat_id!r}")

        payload = {"content": content, "chat_id": chat_id, "reply_to": reply_to}
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._secret:
            body_bytes = json.dumps(payload, ensure_ascii=False).encode()
            headers["X-Webhook-Signature"] = _sign(self._secret, body_bytes)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("webhook send failed: %s", exc)
            return SendResult(success=False, error=str(exc), retryable=True)

        ok = 200 <= resp.status_code < 300
        return SendResult(
            success=ok,
            message_id=resp.headers.get("X-Message-Id", ""),
            error="" if ok else f"HTTP {resp.status_code}",
            retryable=resp.status_code >= 500,
        )

    # ── Inbound normalization ────────────────────────────────────

    def normalize_event(self, raw_event: Any) -> InboundMessage | None:
        if not isinstance(raw_event, dict):
            return None
        user_id = raw_event.get("user_id")
        content = raw_event.get("content")
        if not user_id or not content:
            return None
        return InboundMessage(
            user_id=str(user_id),
            content=str(content),
            platform="webhook",
            chat_id=str(raw_event.get("chat_id", user_id)),
            chat_type=raw_event.get("chat_type", "dm"),
            message_type="text",
            reply_to=raw_event.get("reply_to"),
            user_name=raw_event.get("platform_user_id", ""),
            raw_event=raw_event,
        )

    # ── Callback management ──────────────────────────────────────

    def register_callback(self, chat_id: str, callback_url: str) -> None:
        self._callbacks[chat_id] = callback_url

    # ── Webhook handler ──────────────────────────────────────────

    async def handle_webhook(self, request: Any) -> Any:
        from fastapi.responses import JSONResponse

        try:
            body: dict = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        if self._secret:
            sig = request.headers.get("X-Webhook-Signature", "")
            raw = json.dumps(body, ensure_ascii=False).encode()
            if not hmac.compare_digest(sig, _sign(self._secret, raw)):
                return JSONResponse({"error": "invalid signature"}, status_code=403)

        callback_url = body.get("callback_url")
        chat_id = body.get("chat_id")
        if callback_url and chat_id:
            self.register_callback(chat_id, callback_url)

        msg = self.normalize_event(body)
        if msg is None:
            return JSONResponse(
                {"error": "missing required fields (user_id, content)"},
                status_code=400,
            )
        return msg


from paradise.adapters import register_adapter  # noqa: E402
register_adapter("webhook", WebhookAdapter)
