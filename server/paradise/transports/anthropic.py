"""Anthropic Messages API transport.

Implements :class:`ProviderTransport` for the Anthropic Messages API
(``api_mode = "anthropic_messages"``).  Handles the key differences
between OpenAI and Anthropic formats:

* System prompt is a top-level ``system`` field, not a message.
* Tools use ``input_schema`` instead of ``parameters``.
* Streaming emits ``content_block_delta`` / ``tool_use`` SSE events.

High-level convenience methods
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* :meth:`stream_chat` — streaming text, based on the original
  ``stream_anthropic`` in ``core.llm_router``.
* :meth:`chat` — non-streaming, returns :class:`NormalizedResponse`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from paradise.exceptions import TransportError
from paradise.transports.base import ProviderTransport
from paradise.transports.types import (
    NormalizedResponse,
    ToolCall,
    Usage,
    build_tool_call,
    map_finish_reason,
)

logger = logging.getLogger(__name__)

# Anthropic stop-reason -> normalised finish reason
_FINISH_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}

# Defaults
_API_BASE_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_TIMEOUT = 120.0


class AnthropicTransport(ProviderTransport):
    """Transport for the Anthropic Messages API."""

    # ------------------------------------------------------------------
    # ProviderTransport ABC
    # ------------------------------------------------------------------

    @property
    def api_mode(self) -> str:
        return "anthropic_messages"

    def convert_messages(
        self,
        messages: List[Dict[str, Any]],
        **kwargs,
    ) -> tuple[str | None, List[Dict[str, Any]]]:
        """Separate the system prompt and filter to user/assistant turns.

        Returns ``(system_str, messages_list)``.  ``system_str`` may be
        ``None`` when no system prompt is supplied.
        """
        system_prompt: str | None = kwargs.get("system_prompt")
        filtered = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        return system_prompt, filtered

    def convert_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert OpenAI-format tool definitions to Anthropic format.

        OpenAI uses ``function.parameters``; Anthropic uses
        ``input_schema`` at the top level of each tool definition.
        """
        anthropic_tools: list[dict[str, Any]] = []
        for tool in tools:
            func = tool.get("function", tool)
            anthropic_tools.append(
                {
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return anthropic_tools

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """Build the full kwargs dict for an Anthropic Messages API call."""
        system_prompt: str | None = params.get("system_prompt")
        temperature: float = params.get("temperature", 0.7)
        max_tokens: int = params.get("max_tokens", 2048)
        stream: bool = params.get("stream", False)

        _, converted_messages = self.convert_messages(
            messages, system_prompt=system_prompt
        )

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": converted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

        if system_prompt:
            kwargs["system"] = system_prompt

        if tools:
            kwargs["tools"] = self.convert_tools(
                tools
            )

        return kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalise an Anthropic Messages response to :class:`NormalizedResponse`.

        *response* is expected to be a decoded JSON dict from the Anthropic
        Messages API.
        """
        # -- Content -------------------------------------------------------
        content: str | None = None
        tool_calls: list[ToolCall] | None = None

        blocks = response.get("content", [])
        text_parts: list[str] = []
        tc_list: list[ToolCall] = []

        for block in blocks:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tc_list.append(
                    build_tool_call(
                        id=block.get("id"),
                        name=block.get("name", ""),
                        arguments=block.get("input", {}),
                    )
                )

        content = "".join(text_parts) if text_parts else None
        tool_calls = tc_list if tc_list else None

        # -- Finish reason -------------------------------------------------
        raw_reason = response.get("stop_reason")
        finish_reason = map_finish_reason(raw_reason, _FINISH_REASON_MAP)

        # -- Usage ---------------------------------------------------------
        usage_raw = response.get("usage", {})
        usage = Usage(
            prompt_tokens=usage_raw.get("input_tokens", 0),
            completion_tokens=usage_raw.get("output_tokens", 0),
            total_tokens=(
                usage_raw.get("input_tokens", 0) + usage_raw.get("output_tokens", 0)
            ),
            cached_tokens=usage_raw.get("cache_read_input_tokens", 0),
        )

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    # ------------------------------------------------------------------
    # Optional ProviderTransport overrides
    # ------------------------------------------------------------------

    def validate_response(self, response: Any) -> bool:
        """Check that the response dict has the expected Anthropic structure."""
        if not isinstance(response, dict):
            return False
        return "content" in response and isinstance(response.get("content"), list)

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """Extract Anthropic prompt-cache token counts if present."""
        usage = response.get("usage", {})
        cached = usage.get("cache_read_input_tokens", 0)
        created = usage.get("cache_creation_input_tokens", 0)
        if cached or created:
            return {"cached_tokens": cached, "creation_tokens": created}
        return None

    def map_finish_reason(self, raw_reason: str) -> str:
        return map_finish_reason(raw_reason, _FINISH_REASON_MAP)

    # ------------------------------------------------------------------
    # High-level convenience methods
    # ------------------------------------------------------------------

    async def stream_chat(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncGenerator[str, None]:
        """Stream chat completion via the Anthropic Messages API.

        Yields content text chunks extracted from ``content_block_delta``
        SSE events.

        Based on the original ``stream_anthropic`` from
        ``core.llm_router.LLMRouter``.
        """
        url = _API_BASE_URL
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": _API_VERSION,
        }

        _, converted_messages = self.convert_messages(
            messages, system_prompt=system_prompt
        )

        payload = {
            "model": model,
            "system": system_prompt,
            "messages": converted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        logger.info(
            "[Anthropic] stream model=%s prompt=%d msgs=%d",
            model,
            len(system_prompt),
            len(messages),
        )

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        try:
                            data = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

                        event_type = data.get("type")

                        if event_type == "content_block_delta":
                            text = data.get("delta", {}).get("text", "")
                            if text:
                                yield text
        except httpx.HTTPStatusError as exc:
            raise TransportError(
                f"Anthropic API error: {exc.response.status_code} {exc.response.text[:300]}",
                provider="anthropic",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Anthropic transport error: {exc}",
                provider="anthropic",
            ) from exc

    async def chat(
        self,
        api_key: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> NormalizedResponse:
        """Non-streaming chat via the Anthropic Messages API.

        Returns a :class:`NormalizedResponse`.
        """
        url = _API_BASE_URL
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": _API_VERSION,
        }

        kwargs = self.build_kwargs(
            model=model,
            messages=messages,
            tools=None,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )

        logger.info(
            "[Anthropic] chat model=%s prompt=%d msgs=%d",
            model,
            len(system_prompt),
            len(messages),
        )

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(url, json=kwargs, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            return self.normalize_response(data)

        except httpx.HTTPStatusError as exc:
            raise TransportError(
                f"Anthropic API error: {exc.response.status_code} {exc.response.text[:300]}",
                provider="anthropic",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Anthropic transport error: {exc}",
                provider="anthropic",
            ) from exc


# ── Self-registration ────────────────────────────────────────────────────
from paradise.transports import register_transport
register_transport("anthropic_messages", AnthropicTransport)
