"""Feishu/Lark Bot channel adapter for Paradise.

Simplified from hermes-agent gateway/platforms/feishu.py (~5000 lines).
Supports:
  - WebSocket long connection (default) via lark-oapi SDK
  - Webhook HTTP endpoint (FastAPI integration)
  - Text and rich-text (post) inbound/outbound
  - Image/file/audio/media file_key extraction
  - Per-chat async serialization lock
  - 24-hour dedup cache
  - Adaptive message batching (debounce rapid text bursts)
  - Self-registration with graceful fallback when lark_oapi is absent

Reference: hermes-agent gateway/platforms/feishu.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable

from paradise.adapters.base import (
    ChannelAdapter,
    InboundMessage,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency: lark-oapi SDK
# ---------------------------------------------------------------------------

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )
    from lark_oapi.core import AccessTokenType
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    from lark_oapi.ws import Client as FeishuWSClient

    FEISHU_AVAILABLE = True
except ImportError:
    lark = None  # type: ignore[assignment]
    CreateMessageRequest = None  # type: ignore[assignment]
    CreateMessageRequestBody = None  # type: ignore[assignment]
    ReplyMessageRequest = None  # type: ignore[assignment]
    ReplyMessageRequestBody = None  # type: ignore[assignment]
    AccessTokenType = None  # type: ignore[assignment]
    EventDispatcherHandler = None  # type: ignore[assignment]
    FeishuWSClient = None  # type: ignore[assignment]
    FEISHU_DOMAIN = None  # type: ignore[assignment]
    LARK_DOMAIN = None  # type: ignore[assignment]
    FEISHU_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MARKDOWN_HINT_RE = re.compile(
    r"(^#{1,6}\s)|(^\s*[-*]\s)|(^\s*\d+\.\s)|(^\s*---+\s*$)"
    r"|(```)|(`[^`\n]+`)|(\*\*[^*\n].+?\*\*)|(~~[^~\n].+?~~)"
    r"|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)]+\))|(^>\s)",
    re.MULTILINE,
)
_MARKDOWN_TABLE_RE = re.compile(r"^\|.*\|\n\|[-|: ]+\|", re.MULTILINE)
_MARKDOWN_FENCE_OPEN_RE = re.compile(r"^```([^\n`]*)\s*$")
_MARKDOWN_FENCE_CLOSE_RE = re.compile(r"^```\s*$")
_MENTION_RE = re.compile(r"@_user_\d+")
_MENTION_ALL_RE = re.compile(r"@_all")
_WHITESPACE_RE = re.compile(r"\s+")
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")

_MAX_SEND_ATTEMPTS = 3
_DEFAULT_BATCH_DELAY_SECONDS = 0.6
_DEFAULT_BATCH_SPLIT_DELAY_SECONDS = 2.0
_DEFAULT_BATCH_MAX_MESSAGES = 8
_DEFAULT_BATCH_MAX_CHARS = 4000
_SPLIT_THRESHOLD = 4000
_DEFAULT_DEDUP_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_DEFAULT_DEDUP_CACHE_SIZE = 2048
_DEFAULT_WEBHOOK_HOST = "127.0.0.1"
_DEFAULT_WEBHOOK_PORT = 8765
_DEFAULT_WEBHOOK_PATH = "/feishu/webhook"
_CONNECT_ATTEMPTS = 3
_REPLY_FALLBACK_CODES = frozenset({230011, 231003})

FALLBACK_POST_TEXT = "[Rich text message]"
FALLBACK_IMAGE_TEXT = "[Image]"
FALLBACK_ATTACHMENT_TEXT = "[Attachment]"


# ---------------------------------------------------------------------------
# Public utility
# ---------------------------------------------------------------------------


def check_feishu_requirements() -> bool:
    """Return True if lark_oapi is installed and FEISHU_APP_ID/SECRET are set."""
    if not FEISHU_AVAILABLE:
        return False
    return bool(os.getenv("FEISHU_APP_ID", "").strip()) and bool(
        os.getenv("FEISHU_APP_SECRET", "").strip()
    )


# ---------------------------------------------------------------------------
# Markdown / post payload helpers
# ---------------------------------------------------------------------------


def _build_markdown_post_payload(content: str) -> str:
    """Wrap content into Feishu post-type JSON payload with md tag elements."""
    rows = _build_markdown_post_rows(content)
    return json.dumps(
        {"zh_cn": {"content": rows}},
        ensure_ascii=False,
    )


def _build_markdown_post_rows(content: str) -> list[list[dict[str, str]]]:
    """Split content at fenced code block boundaries for correct rendering."""
    if not content:
        return [[{"tag": "md", "text": ""}]]
    if "```" not in content:
        return [[{"tag": "md", "text": content}]]

    rows: list[list[dict[str, str]]] = []
    current: list[str] = []
    in_code_block = False

    def _flush() -> None:
        nonlocal current
        if not current:
            return
        segment = "\n".join(current)
        if segment.strip():
            rows.append([{"tag": "md", "text": segment}])
        current = []

    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        is_fence = bool(
            _MARKDOWN_FENCE_CLOSE_RE.match(stripped)
            if in_code_block
            else _MARKDOWN_FENCE_OPEN_RE.match(stripped)
        )
        if is_fence:
            if not in_code_block:
                _flush()
            current.append(raw_line)
            in_code_block = not in_code_block
            if not in_code_block:
                _flush()
            continue
        current.append(raw_line)

    _flush()
    return rows or [[{"tag": "md", "text": content}]]


def _build_outbound_payload(content: str) -> tuple[str, str]:
    """Decide Feishu msg_type and JSON payload for outbound content.

    Returns (msg_type, json_payload).
    - "post" with md tag elements when content has markdown hints (but not tables).
    - "text" otherwise (plain text, including markdown tables).
    """
    if _MARKDOWN_TABLE_RE.search(content):
        return "text", json.dumps({"text": content}, ensure_ascii=False)
    if _MARKDOWN_HINT_RE.search(content):
        return "post", _build_markdown_post_payload(content)
    return "text", json.dumps({"text": content}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Inbound normalization helpers
# ---------------------------------------------------------------------------


def _load_json_payload(raw: str) -> dict[str, Any]:
    """Parse raw JSON content string from Feishu message."""
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"text": raw}
    return parsed if isinstance(parsed, dict) else {"content": parsed}


def _normalize_text(text: str, mentions: Any = None) -> str:
    """Replace @_user_N mention placeholders with @name and clean whitespace."""
    mentions_map = _build_mentions_map(mentions)

    def _sub(match: re.Match[str]) -> str:
        key = match.group(0)
        ref = mentions_map.get(key)
        if ref is None:
            return " "
        name = ref.get("name") or ref.get("open_id") or "user"
        return f"@{name}"

    cleaned = _MENTION_RE.sub(_sub, text or "")
    cleaned = _MENTION_ALL_RE.sub("@all", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "\n".join(
        _WHITESPACE_RE.sub(" ", line).strip() for line in cleaned.split("\n")
    )
    cleaned = "\n".join(line for line in cleaned.split("\n") if line)
    cleaned = _MULTISPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


def _build_mentions_map(mentions: Any) -> dict[str, dict[str, str]]:
    """Convert SDK mention objects to a dict keyed by @_user_N placeholder."""
    result: dict[str, dict[str, str]] = {}
    for mention in mentions or []:
        key = str(getattr(mention, "key", "") or "")
        if not key:
            continue
        if key == "@_all":
            result[key] = {"name": "all", "is_all": "true"}
            continue
        mention_id = getattr(mention, "id", None)
        if isinstance(mention_id, str):
            open_id = mention_id
        elif mention_id is not None:
            open_id = str(getattr(mention_id, "open_id", "") or "")
        else:
            open_id = ""
        name = str(getattr(mention, "name", "") or "").strip()
        result[key] = {"name": name, "open_id": open_id}
    return result


def _extract_post_text(payload: dict[str, Any], mentions: Any = None) -> str:
    """Extract plain text from a Feishu post (rich text) payload.

    Handles the locale wrapper (zh_cn / en_us) and iterates content rows.
    """
    resolved = _resolve_post_payload(payload)
    if not resolved:
        return FALLBACK_POST_TEXT

    parts: list[str] = []
    title = _normalize_text(str(resolved.get("title", "")).strip())
    if title:
        parts.append(title)

    for row in resolved.get("content", []) or []:
        if not isinstance(row, list):
            continue
        row_parts: list[str] = []
        for element in row:
            if isinstance(element, str):
                row_parts.append(element)
            elif isinstance(element, dict):
                tag = str(element.get("tag", "")).strip().lower()
                if tag == "text":
                    row_parts.append(str(element.get("text", "") or ""))
                elif tag == "a":
                    href = str(element.get("href", "") or "").strip()
                    label = str(element.get("text", href) or "").strip()
                    row_parts.append(f"[{label}]({href})" if href else label)
                elif tag == "at":
                    placeholder = str(element.get("user_id", "")).strip()
                    if placeholder == "@_all":
                        row_parts.append("@all")
                    else:
                        user_name = str(element.get("user_name", "") or "user")
                        row_parts.append(f"@{user_name}")
        row_text = _normalize_text("".join(row_parts), mentions)
        if row_text:
            parts.append(row_text)

    return "\n".join(parts).strip() or FALLBACK_POST_TEXT


def _resolve_post_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve locale-wrapped post payload to {title, content}."""
    if "content" in payload and isinstance(payload.get("content"), list):
        return {"title": str(payload.get("title", "") or ""), "content": payload["content"]}
    wrapped = payload.get("post")
    if isinstance(wrapped, dict):
        for key in ("zh_cn", "en_us"):
            candidate = wrapped.get(key)
            if isinstance(candidate, dict) and isinstance(candidate.get("content"), list):
                return {"title": str(candidate.get("title", "") or ""), "content": candidate["content"]}
        for value in wrapped.values():
            if isinstance(value, dict) and isinstance(value.get("content"), list):
                return {"title": str(value.get("title", "") or ""), "content": value["content"]}
    for key in ("zh_cn", "en_us"):
        candidate = payload.get(key)
        if isinstance(candidate, dict) and isinstance(candidate.get("content"), list):
            return {"title": str(candidate.get("title", "") or ""), "content": candidate["content"]}
    return None


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


def _get_header(headers: Any, key: str) -> str:
    """Extract a header value from dict-like or object-like headers."""
    if isinstance(headers, dict):
        return str(headers.get(key, "") or "")
    get_fn = getattr(headers, "get", None)
    if callable(get_fn):
        return str(get_fn(key, "") or "")
    return str(getattr(headers, key, "") or "")


def _verify_webhook_signature(
    headers: Any,
    body_bytes: bytes,
    encrypt_key: str,
) -> bool:
    """Verify Feishu webhook signature: SHA256(timestamp + nonce + encrypt_key + body)."""
    timestamp = _get_header(headers, "x-lark-request-timestamp")
    nonce = _get_header(headers, "x-lark-request-nonce")
    signature = _get_header(headers, "x-lark-signature")
    if not timestamp or not nonce or not signature:
        return False
    try:
        body_str = body_bytes.decode("utf-8", errors="replace")
        content = f"{timestamp}{nonce}{encrypt_key}{body_str}"
        computed = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return hmac.compare_digest(computed, signature)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session key helper
# ---------------------------------------------------------------------------


def _build_session_key(chat_id: str, thread_id: str | None = None) -> str:
    """Build a session key: (platform, chat_id, thread_id)."""
    parts = ["feishu", chat_id]
    if thread_id:
        parts.append(thread_id)
    return "|".join(parts)


# ---------------------------------------------------------------------------
# FeishuAdapter
# ---------------------------------------------------------------------------


class FeishuAdapter(ChannelAdapter):
    """Feishu/Lark Bot channel adapter for Paradise.

    Supports WebSocket (long connection) and Webhook (HTTP) modes.
    Configuration via environment variables, matching hermes patterns.
    """

    name = "feishu"

    # =====================================================================
    # Lifecycle — init / connect / disconnect
    # =====================================================================

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)

        # ── Credentials & mode ──
        self._app_id: str = str(
            (config or {}).get("app_id") or os.getenv("FEISHU_APP_ID", "")
        ).strip()
        self._app_secret: str = str(
            (config or {}).get("app_secret") or os.getenv("FEISHU_APP_SECRET", "")
        ).strip()
        self._domain_name: str = str(
            (config or {}).get("domain") or os.getenv("FEISHU_DOMAIN", "feishu")
        ).strip().lower()
        self._connection_mode: str = str(
            (config or {}).get("connection_mode")
            or os.getenv("FEISHU_CONNECTION_MODE", "websocket")
        ).strip().lower()
        self._encrypt_key: str = os.getenv("FEISHU_ENCRYPT_KEY", "").strip()
        self._verification_token: str = os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip()

        # ── Webhook settings ──
        self._webhook_host: str = str(
            (config or {}).get("webhook_host")
            or os.getenv("FEISHU_WEBHOOK_HOST", _DEFAULT_WEBHOOK_HOST)
        ).strip()
        self._webhook_port: int = int(
            (config or {}).get("webhook_port")
            or os.getenv("FEISHU_WEBHOOK_PORT", str(_DEFAULT_WEBHOOK_PORT))
        )
        self._webhook_path: str = str(
            (config or {}).get("webhook_path")
            or os.getenv("FEISHU_WEBHOOK_PATH", _DEFAULT_WEBHOOK_PATH)
        ).strip() or _DEFAULT_WEBHOOK_PATH

        # ── SDK clients ──
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_future: asyncio.Future | None = None
        self._ws_thread_loop: asyncio.AbstractEventLoop | None = None
        self._event_handler: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # ── Dedup cache ──
        self._seen_message_ids: dict[str, float] = {}
        self._dedup_lock = threading.Lock()

        # ── Per-chat serialization ──
        self._chat_locks: dict[str, asyncio.Lock] = {}

        # ── Message batching ──
        self._pending_batches: dict[str, InboundMessage] = {}
        self._pending_batch_tasks: dict[str, asyncio.Task] = {}
        self._pending_batch_counts: dict[str, int] = {}
        self._batch_delay: float = float(
            os.getenv("FEISHU_BATCH_DELAY", str(_DEFAULT_BATCH_DELAY_SECONDS))
        )
        self._batch_split_delay: float = _DEFAULT_BATCH_SPLIT_DELAY_SECONDS
        self._batch_max_messages: int = int(
            os.getenv("FEISHU_BATCH_MAX_MESSAGES", str(_DEFAULT_BATCH_MAX_MESSAGES))
        )
        self._batch_max_chars: int = int(
            os.getenv("FEISHU_BATCH_MAX_CHARS", str(_DEFAULT_BATCH_MAX_CHARS))
        )

        # ── Inbound callback ──
        self._on_inbound: Callable[[InboundMessage], Any] | None = None

        # ── Webhook route registration callback (set by transport layer) ──
        self._register_webhook_route: Callable[..., Any] | None = None

        self._running = False

    # ── Configuration ─────────────────────────────────────────────────

    def set_inbound_callback(self, callback: Callable[[InboundMessage], Any]) -> None:
        """Set the callback invoked for each normalized inbound message."""
        self._on_inbound = callback

    def set_webhook_route_registrar(
        self, registrar: Callable[..., Any]
    ) -> None:
        """Set the function used to register a FastAPI webhook route.

        Called as ``registrar(path, handler)`` during connect() in webhook mode.
        """
        self._register_webhook_route = registrar

    # ── connect() ─────────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Connect to Feishu/Lark in the configured mode."""
        if not FEISHU_AVAILABLE:
            logger.error("[Feishu] lark-oapi SDK not installed")
            return False
        if not self._app_id or not self._app_secret:
            logger.error(
                "[Feishu] FEISHU_APP_ID or FEISHU_APP_SECRET not configured"
            )
            return False
        if self._connection_mode not in ("websocket", "webhook"):
            logger.error(
                "[Feishu] Unsupported FEISHU_CONNECTION_MODE=%s; "
                "expected 'websocket' or 'webhook'",
                self._connection_mode,
            )
            return False

        try:
            self._loop = asyncio.get_running_loop()
            await self._connect_with_retry()
            self._running = True
            self._mark_connected()
            logger.info(
                "[Feishu] Connected in %s mode (%s)",
                self._connection_mode,
                self._domain_name,
            )
            return True
        except Exception as exc:
            logger.error("[Feishu] Failed to connect: %s", exc, exc_info=True)
            return False

    async def _connect_with_retry(self) -> None:
        """Attempt connection with exponential backoff."""
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                if self._connection_mode == "websocket":
                    await self._connect_websocket()
                else:
                    await self._connect_webhook()
                return
            except Exception as exc:
                self._running = False
                self._ws_future = None
                if attempt >= _CONNECT_ATTEMPTS - 1:
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "[Feishu] Connect attempt %d/%d failed; retry in %ds: %s",
                    attempt + 1,
                    _CONNECT_ATTEMPTS,
                    wait,
                    exc,
                )
                await asyncio.sleep(wait)

    async def _connect_websocket(self) -> None:
        """Setup lark-oapi event dispatcher + start WS client."""
        domain = FEISHU_DOMAIN if self._domain_name != "lark" else LARK_DOMAIN
        self._client = self._build_lark_client(domain)
        self._event_handler = self._build_event_handler()
        if self._event_handler is None:
            raise RuntimeError("Failed to build Feishu event handler")

        self._ws_client = FeishuWSClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=self._event_handler,
            domain=domain,
        )
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("Adapter event loop is not ready")

        self._ws_future = loop.run_in_executor(
            None, _run_ws_client, self._ws_client
        )

    async def _connect_webhook(self) -> None:
        """Register FastAPI webhook route via callback."""
        domain = FEISHU_DOMAIN if self._domain_name != "lark" else LARK_DOMAIN
        self._client = self._build_lark_client(domain)

        if self._register_webhook_route is not None:
            self._register_webhook_route(
                self._webhook_path, self.handle_webhook
            )
            logger.info(
                "[Feishu] Webhook route registered at %s",
                self._webhook_path,
            )
        else:
            logger.warning(
                "[Feishu] No webhook route registrar configured; "
                "external HTTP server required for path %s",
                self._webhook_path,
            )

    def _build_lark_client(self, domain: Any) -> Any:
        """Build a lark-oapi Client with app credentials."""
        return (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def _build_event_handler(self) -> Any:
        """Build lark-oapi EventDispatcherHandler with message callback."""
        if EventDispatcherHandler is None:
            return None
        return (
            EventDispatcherHandler.builder(
                self._encrypt_key,
                self._verification_token,
            )
            .register_p2_im_message_receive_v1(self._on_ws_message_event)
            .build()
        )

    # ── disconnect() ──────────────────────────────────────────────────

    async def disconnect(self) -> None:
        """Disconnect and clean up resources."""
        self._running = False

        # Cancel pending batch tasks
        for task in self._pending_batch_tasks.values():
            if task and not task.done():
                task.cancel()
        if self._pending_batch_tasks:
            await asyncio.gather(
                *self._pending_batch_tasks.values(), return_exceptions=True
            )
        self._pending_batch_tasks.clear()
        self._pending_batches.clear()
        self._pending_batch_counts.clear()

        # Stop websocket client
        if self._ws_client is not None:
            try:
                setattr(self._ws_client, "_auto_reconnect", False)
            except Exception:
                pass
            self._ws_client = None

        # Wait for WS thread to exit
        ws_future = self._ws_future
        if ws_future is not None:
            try:
                await asyncio.wait_for(asyncio.shield(ws_future), timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass

        # Stop WS thread loop
        ws_loop = self._ws_thread_loop
        if ws_loop is not None and not ws_loop.is_closed():
            try:
                for task in asyncio.all_tasks(ws_loop):
                    task.cancel()
                ws_loop.call_soon_threadsafe(ws_loop.stop)
            except Exception:
                pass

        self._ws_future = None
        self._ws_thread_loop = None
        self._loop = None
        self._event_handler = None
        self._client = None

        self._chat_locks.clear()
        self._mark_disconnected()
        logger.info("[Feishu] Disconnected")

    # =====================================================================
    # Outbound — send
    # =====================================================================

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs: Any,
    ) -> SendResult:
        """Send a message to Feishu.

        - If reply_to is provided, uses im.v1.message.reply API.
        - Otherwise uses im.v1.message.create API.
        - Auto-detects chat_id prefix: "ou_" = user (DM), "oc_" = group.
        - Markdown content is sent as post-type with md tag elements.
        - Retries up to 3 times with exponential backoff.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")

        msg_type, payload = _build_outbound_payload(content)
        return await self._send_with_retry(
            chat_id=chat_id,
            msg_type=msg_type,
            payload=payload,
            reply_to=reply_to,
        )

    async def _send_with_retry(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: str | None = None,
    ) -> SendResult:
        """Send with retry logic: 3 attempts, exponential backoff."""
        last_error: str = ""
        active_reply_to = reply_to

        for attempt in range(_MAX_SEND_ATTEMPTS):
            try:
                response = await self._send_raw_message(
                    chat_id=chat_id,
                    msg_type=msg_type,
                    payload=payload,
                    reply_to=active_reply_to,
                )

                if self._response_succeeded(response):
                    message_id = self._extract_field(response, "message_id") or ""
                    return SendResult(success=True, message_id=message_id)

                # Reply target withdrawn/missing -> fall back to create
                if active_reply_to:
                    code = getattr(response, "code", None)
                    if code in _REPLY_FALLBACK_CODES:
                        logger.warning(
                            "[Feishu] Reply to %s failed (code %s); "
                            "falling back to new message",
                            active_reply_to,
                            code,
                        )
                        active_reply_to = None
                        continue

                code = getattr(response, "msg", "unknown")
                last_error = str(code)

            except Exception as exc:
                last_error = str(exc)

            if attempt < _MAX_SEND_ATTEMPTS - 1:
                wait = 0.5 * (2 ** attempt)
                logger.warning(
                    "[Feishu] Send attempt %d/%d failed; retry in %.1fs: %s",
                    attempt + 1,
                    _MAX_SEND_ATTEMPTS,
                    wait,
                    last_error,
                )
                await asyncio.sleep(wait)

        return SendResult(success=False, error=last_error, retryable=True)

    async def _send_raw_message(
        self,
        *,
        chat_id: str,
        msg_type: str,
        payload: str,
        reply_to: str | None = None,
    ) -> Any:
        """Core send: reply or create depending on reply_to."""
        if reply_to:
            body = ReplyMessageRequestBody.builder() \
                .msg_type(msg_type) \
                .content(payload) \
                .uuid(str(uuid.uuid4())) \
                .build()
            request = ReplyMessageRequest.builder() \
                .message_id(reply_to) \
                .request_body(body) \
                .build()
            return await asyncio.to_thread(
                self._client.im.v1.message.reply, request
            )

        # Create new message
        body = CreateMessageRequestBody.builder() \
            .msg_type(msg_type) \
            .content(payload) \
            .receive_id(chat_id) \
            .uuid(str(uuid.uuid4())) \
            .build()

        # Detect whether chat_id is user (ou_) or group (oc_)
        if chat_id.startswith("ou_"):
            receive_id_type = "open_id"
        else:
            receive_id_type = "chat_id"

        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(body) \
            .build()
        return await asyncio.to_thread(
            self._client.im.v1.message.create, request
        )

    @staticmethod
    def _response_succeeded(response: Any) -> bool:
        """Check if lark-oapi response indicates success."""
        return bool(response and getattr(response, "success", lambda: False)())

    @staticmethod
    def _extract_field(response: Any, field_name: str) -> Any:
        """Extract a field from a successful lark-oapi response."""
        data = getattr(response, "data", None)
        return getattr(data, field_name, None) if data else None

    # =====================================================================
    # Inbound — normalize_event
    # =====================================================================

    def normalize_event(self, raw_event: Any) -> InboundMessage | None:
        """Convert a Feishu event data object to InboundMessage.

        Handles: text, post, image, file, audio, media message types.
        """
        event = getattr(raw_event, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)

        if not message or not sender:
            return None

        # ── Extract basic fields ──
        message_id = str(getattr(message, "message_id", "") or "")
        raw_type = str(getattr(message, "message_type", "") or "").strip().lower()
        raw_content = str(getattr(message, "content", "") or "")
        chat_id = str(getattr(message, "chat_id", "") or "")
        chat_type_raw = str(getattr(message, "chat_type", "p2p") or "").strip().lower()

        sender_id_obj = getattr(sender, "sender_id", None)
        user_id = ""
        if sender_id_obj is not None:
            user_id = (
                str(getattr(sender_id_obj, "open_id", "") or "")
                or str(getattr(sender_id_obj, "user_id", "") or "")
                or str(getattr(sender_id_obj, "union_id", "") or "")
            )

        chat_type = "group" if chat_type_raw == "group" else "dm"
        mentions = getattr(message, "mentions", None)
        thread_id = str(getattr(message, "root_id", "") or "") or None

        # ── Parse content by type ──
        content = ""
        media_urls: list[str] = []
        inbound_type = "text"

        if raw_type == "text":
            payload = _load_json_payload(raw_content)
            content = _normalize_text(str(payload.get("text", "") or ""), mentions)
            inbound_type = "text"

        elif raw_type == "post":
            payload = _load_json_payload(raw_content)
            content = _extract_post_text(payload, mentions)
            inbound_type = "text"

        elif raw_type == "image":
            payload = _load_json_payload(raw_content)
            image_key = str(payload.get("image_key", "") or "").strip()
            if image_key:
                media_urls.append(image_key)
            content = _normalize_text(
                str(payload.get("text", "") or str(payload.get("alt", "") or ""))
                or FALLBACK_IMAGE_TEXT,
                mentions,
            )
            inbound_type = "image"

        elif raw_type in ("file", "audio", "media"):
            payload = _load_json_payload(raw_content)
            file_key = str(payload.get("file_key", "") or "").strip()
            file_name = str(payload.get("file_name", "") or "").strip()
            if file_key:
                media_urls.append(file_key)
            content = file_name or FALLBACK_ATTACHMENT_TEXT
            inbound_type = "audio" if raw_type == "audio" else "file"

        else:
            logger.debug("[Feishu] Ignoring unsupported message type: %s", raw_type)
            return None

        if not content and not media_urls:
            return None

        session_key = _build_session_key(chat_id, thread_id)

        return InboundMessage(
            user_id=user_id,
            content=content,
            platform="feishu",
            chat_id=session_key,
            chat_type=chat_type,
            message_type=inbound_type,
            media_urls=media_urls,
            reply_to=message_id,
            raw_event=raw_event,
        )

    # =====================================================================
    # Webhook handler
    # =====================================================================

    async def handle_webhook(self, request: Any) -> Any:
        """Handle incoming Feishu webhook HTTP request.

        Flow:
          1. URL verification challenge -> return {"challenge": value}
          2. Verify signature (SHA256 timestamp+nonce+encrypt_key+body)
          3. Verify token if configured
          4. Dispatch event -> normalize_event -> callback
        """
        try:
            body_bytes: bytes = await request.body()
        except Exception:
            body_bytes = b""

        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"code": 400, "msg": "invalid json"}, status_code=400
            )

        # ── URL verification ──
        if payload.get("type") == "url_verification":
            from fastapi.responses import JSONResponse
            return JSONResponse({"challenge": payload.get("challenge", "")})

        # ── Verification token check ──
        if self._verification_token:
            header = payload.get("header") or {}
            incoming_token = str(
                header.get("token") or payload.get("token") or ""
            )
            if not incoming_token or not hmac.compare_digest(
                incoming_token, self._verification_token
            ):
                logger.warning("[Feishu] Webhook: invalid verification token")
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    {"code": 401, "msg": "invalid token"}, status_code=401
                )

        # ── Signature verification ──
        headers_dict = {}
        if hasattr(request, "headers"):
            headers_dict = dict(request.headers)
        if self._encrypt_key and not _verify_webhook_signature(
            headers_dict, body_bytes, self._encrypt_key
        ):
            logger.warning("[Feishu] Webhook: invalid signature")
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"code": 401, "msg": "invalid signature"}, status_code=401
            )

        # ── Dispatch event ──
        event_type = str((payload.get("header") or {}).get("event_type") or "")
        if event_type == "im.message.receive_v1":
            event_data = self._dict_to_namespace(payload.get("event", {}))
            await self._handle_inbound_event(event_data)

        from fastapi.responses import JSONResponse
        return JSONResponse({"code": 0, "msg": "ok"})

    @staticmethod
    def _dict_to_namespace(value: Any) -> Any:
        """Recursively convert dict to SimpleNamespace for attribute access."""
        if isinstance(value, dict):
            from types import SimpleNamespace
            return SimpleNamespace(
                **{k: FeishuAdapter._dict_to_namespace(v) for k, v in value.items()}
            )
        if isinstance(value, list):
            return [FeishuAdapter._dict_to_namespace(v) for v in value]
        return value

    # =====================================================================
    # WebSocket message handler
    # =====================================================================

    def _on_ws_message_event(self, data: Any) -> None:
        """Handle message event from lark-oapi WS event dispatcher.

        Called on a background thread by the SDK. Schedule processing
        on the adapter's event loop.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.debug("[Feishu] Dropping event: loop not ready")
            return

        message = getattr(getattr(data, "event", None), "message", None)
        if message is not None:
            message_id = str(getattr(message, "message_id", "") or "")
            if message_id and self._is_duplicate(message_id):
                logger.debug("[Feishu] Dropping duplicate: %s", message_id)
                return

        future = asyncio.run_coroutine_threadsafe(
            self._handle_inbound_event(data), loop
        )
        future.add_done_callback(self._log_background_failure)

    # =====================================================================
    # Inbound event processing pipeline
    # =====================================================================

    async def _handle_inbound_event(self, data: Any) -> None:
        """Process an inbound event: normalize -> batch or dispatch."""
        inbound = self.normalize_event(data)
        if inbound is None:
            return

        # Only batch text messages; media passes through immediately
        if inbound.message_type == "text" and not inbound.reply_to:
            await self._enqueue_batch(inbound)
        else:
            await self._dispatch_inbound(inbound)

    async def _dispatch_inbound(self, inbound: InboundMessage) -> None:
        """Dispatch a normalized inbound message via callback."""
        if self._on_inbound is not None:
            try:
                result = self._on_inbound(inbound)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.error(
                    "[Feishu] Inbound callback error: %s",
                    exc_info=True,
                )

    # =====================================================================
    # Dedup cache
    # =====================================================================

    def _is_duplicate(self, message_id: str) -> bool:
        """Check if message_id was seen within TTL. If not, record it."""
        now = time.time()
        with self._dedup_lock:
            seen_at = self._seen_message_ids.get(message_id)
            if seen_at is not None and (now - seen_at) < _DEFAULT_DEDUP_TTL_SECONDS:
                return True
            self._seen_message_ids[message_id] = now
            self._evict_dedup()

    def _evict_dedup(self) -> None:
        """Evict entries older than 24 hours from the dedup cache."""
        now = time.time()
        expired = [
            mid
            for mid, ts in self._seen_message_ids.items()
            if (now - ts) >= _DEFAULT_DEDUP_TTL_SECONDS
        ]
        for mid in expired:
            del self._seen_message_ids[mid]
        # Also enforce max size
        if len(self._seen_message_ids) > _DEFAULT_DEDUP_CACHE_SIZE:
            sorted_ids = sorted(
                self._seen_message_ids, key=self._seen_message_ids.get
            )
            for mid in sorted_ids[: len(sorted_ids) - _DEFAULT_DEDUP_CACHE_SIZE]:
                del self._seen_message_ids[mid]

    # =====================================================================
    # Per-chat serialization lock
    # =====================================================================

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create a per-chat asyncio.Lock for message serialization."""
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    # =====================================================================
    # Message batching (adaptive debounce)
    # =====================================================================

    async def _enqueue_batch(self, inbound: InboundMessage) -> None:
        """Queue an inbound text message for batching.

        If a batch is already pending for this chat, append to it.
        Otherwise, start a new batch with a flush timer.
        """
        key = inbound.chat_id
        existing = self._pending_batches.get(key)

        if existing is None:
            # First message in batch -> store and schedule flush
            self._pending_batches[key] = inbound
            self._pending_batch_counts[key] = 1
            self._schedule_batch_flush(key)
            return

        # Check compatibility (same chat context)
        if existing.reply_to != inbound.reply_to:
            await self._flush_batch_now(key)
            self._pending_batches[key] = inbound
            self._pending_batch_counts[key] = 1
            self._schedule_batch_flush(key)
            return

        # Merge into existing batch
        count = self._pending_batch_counts.get(key, 1) + 1
        combined = f"{existing.content}\n{inbound.content}"

        if count > self._batch_max_messages or len(combined) > self._batch_max_chars:
            await self._flush_batch_now(key)
            self._pending_batches[key] = inbound
            self._pending_batch_counts[key] = 1
            self._schedule_batch_flush(key)
            return

        existing.content = combined
        self._pending_batch_counts[key] = count
        self._schedule_batch_flush(key)

    def _schedule_batch_flush(self, key: str) -> None:
        """Reset the debounce timer for a pending text batch."""
        prior = self._pending_batch_tasks.get(key)
        if prior and not prior.done():
            prior.cancel()
        self._pending_batch_tasks[key] = asyncio.create_task(
            self._flush_batch(key)
        )

    async def _flush_batch(self, key: str) -> None:
        """Flush a pending text batch after the adaptive quiet period."""
        current_task = asyncio.current_task()
        try:
            pending = self._pending_batches.get(key)
            last_len = len(pending.content) if pending else 0
            # Adaptive delay: longer wait when chunk is near split threshold
            delay = (
                self._batch_split_delay
                if last_len >= _SPLIT_THRESHOLD
                else self._batch_delay
            )
            await asyncio.sleep(delay)
            await self._flush_batch_now(key)
        finally:
            if self._pending_batch_tasks.get(key) is current_task:
                self._pending_batch_tasks.pop(key, None)

    async def _flush_batch_now(self, key: str) -> None:
        """Dispatch the current batch immediately."""
        inbound = self._pending_batches.pop(key, None)
        self._pending_batch_counts.pop(key, None)
        if inbound is not None:
            logger.debug(
                "[Feishu] Flushing text batch for %s (%d chars)",
                key,
                len(inbound.content),
            )
            await self._dispatch_inbound(inbound)

    # =====================================================================
    # Utility / logging
    # =====================================================================

    @staticmethod
    def _log_background_failure(future: asyncio.Future) -> None:
        """Log exceptions from run_coroutine_threadsafe submissions."""
        try:
            future.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("[Feishu] Background task failed", exc_info=True)


# ---------------------------------------------------------------------------
# WebSocket thread runner
# ---------------------------------------------------------------------------


def _run_ws_client(ws_client: Any) -> None:
    """Run the lark-oapi WS client in its own thread-local event loop."""
    import asyncio as _asyncio

    try:
        import lark_oapi.ws.client as ws_client_module
    except ImportError:
        logger.error("[Feishu] lark_oapi.ws.client module not available")
        return

    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    ws_client_module.loop = loop

    try:
        ws_client.start()
    except Exception:
        pass
    finally:
        pending = [t for t in _asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                _asyncio.gather(*pending, return_exceptions=True)
            )
        try:
            loop.stop()
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Self-register (graceful when lark_oapi is absent)
# ---------------------------------------------------------------------------

try:
    from paradise.adapters import register_adapter

    register_adapter("feishu", FeishuAdapter)
except Exception:
    pass
