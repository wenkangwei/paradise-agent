"""OpenAI-compatible Transport — handles /v1/chat/completions endpoints.

Works with any provider that speaks the OpenAI chat completions protocol:
OpenAI itself, z.ai, Ollama in OpenAI-compat mode, LM Studio, etc.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
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

# ── Local-host detection (skip proxy for localhost) ────────────────────────

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})

_FINISH_REASON_MAP: dict[str, str] = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "length": "length",
    "content_filter": "content_filter",
}


def _is_local_url(url: str) -> bool:
    """Return True when *url* points to a local address."""
    host = urllib.parse.urlparse(url).hostname or ""
    return host in _LOCAL_HOSTS


def _build_headers(api_key: str) -> dict[str, str]:
    """Build request headers; omit Authorization when no key is given."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _build_url(api_url: str) -> str:
    """Normalise *api_url* to a ``/chat/completions`` endpoint."""
    return f"{api_url.rstrip('/')}/chat/completions"


# ── Transport implementation ──────────────────────────────────────────────


class OpenAICompatTransport(ProviderTransport):
    """Transport for OpenAI-compatible ``/v1/chat/completions`` APIs."""

    # -- ProviderTransport ABC ------------------------------------------------

    @property
    def api_mode(self) -> str:  # noqa: D401
        """``"openai_compat"``."""
        return "openai_compat"

    # -- Conversion helpers ---------------------------------------------------

    def convert_messages(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Prepend a system message and return the full message list."""
        converted: list[dict[str, Any]] = []
        if system_prompt:
            converted.append({"role": "system", "content": system_prompt})
        converted.extend(messages)
        return converted

    def convert_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Pass tools through unchanged — OpenAI format is the native format."""
        return list(tools)

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stream: bool = False,
        **params: Any,
    ) -> Dict[str, Any]:
        """Build the complete request payload dict.

        Calls :meth:`convert_messages` and :meth:`convert_tools` internally
        so that callers only need this single entry point.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": self.convert_messages(messages, system_prompt=system_prompt),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = self.convert_tools(tools)
            payload["tool_choice"] = "auto"
        # Merge any extra provider-specific params.
        payload.update(params)
        return payload

    def normalize_response(
        self,
        response: Any,
        stream: bool = False,
        **kwargs: Any,
    ) -> NormalizedResponse:
        """Normalise an OpenAI-format response.

        *response* is either:
        - A parsed JSON dict (non-streaming call), or
        - A pre-assembled dict produced by :meth:`stream_chat` where
          ``stream=True`` signals that content/tool_calls were collected
          from SSE deltas.

        The ``stream`` flag tells us which shape to expect.
        """
        if stream:
            return self._normalize_stream_buffer(response)
        return self._normalize_non_stream(response)

    # -- Non-streaming normalisation -----------------------------------------

    @staticmethod
    def _normalize_non_stream(data: dict[str, Any]) -> NormalizedResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        raw_finish = choice.get("finish_reason")
        finish_reason = map_finish_reason(raw_finish, _FINISH_REASON_MAP)

        content: str | None = msg.get("content") or None
        tool_calls: list[ToolCall] | None = None
        raw_tcs = msg.get("tool_calls")
        if raw_tcs:
            tool_calls = [
                build_tool_call(
                    id=tc.get("id"),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", ""),
                )
                for tc in raw_tcs
            ]

        usage_data = data.get("usage")
        usage: Usage | None = None
        if usage_data:
            usage = Usage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            )

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    # -- Streaming-buffer normalisation ---------------------------------------

    @staticmethod
    def _normalize_stream_buffer(buf: dict[str, Any]) -> NormalizedResponse:
        """Normalise the buffer dict accumulated by :meth:`stream_chat`.

        *buf* has keys ``content``, ``tool_calls``, ``finish_reason``.
        """
        content: str | None = buf.get("content") or None
        finish_reason = map_finish_reason(buf.get("finish_reason"), _FINISH_REASON_MAP)

        raw_tcs = buf.get("tool_calls")
        tool_calls: list[ToolCall] | None = None
        if raw_tcs:
            tool_calls = [
                build_tool_call(
                    id=tc.get("id"),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=tc.get("function", {}).get("arguments", ""),
                )
                for tc in raw_tcs
            ]

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    # -- map_finish_reason override -------------------------------------------

    def map_finish_reason(self, raw_reason: str) -> str:
        return map_finish_reason(raw_reason, _FINISH_REASON_MAP)

    # ── High-level convenience methods ──────────────────────────────────────
    # These wrap httpx calls so callers don't need to handle transport
    # plumbing directly.  They are *not* part of the ProviderTransport ABC.

    async def stream_chat(
        self,
        api_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: list[dict] | None = None,
    ) -> AsyncGenerator[str | dict, None]:
        """Stream chat completion over SSE.

        Yields ``str`` content chunks and ``dict`` tool call objects (once
        each tool call is fully assembled).
        """
        url = _build_url(api_url)
        headers = _build_headers(api_key)
        payload = self.build_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        logger.info(
            "[openai_compat] stream model=%s tools=%d prompt=%d msgs=%d",
            model,
            len(tools) if tools else 0,
            len(system_prompt),
            len(messages),
        )

        trust_env = not _is_local_url(url)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=10.0),
                trust_env=trust_env,
            ) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=headers,
                ) as resp:
                    resp.raise_for_status()

                    tool_call_buffers: dict[int, dict] = {}

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            # Flush any remaining assembled tool calls.
                            for _idx, tc in sorted(tool_call_buffers.items()):
                                yield tc
                            break
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        choice = (data.get("choices") or [{}])[0]
                        delta = choice.get("delta", {})

                        # ── Content ────────────────────────────────────
                        content = delta.get("content", "")
                        if content:
                            yield content

                        # ── Tool calls (streaming assembly) ────────────
                        tc_list = delta.get("tool_calls")
                        if tc_list:
                            for tc_delta in tc_list:
                                idx = tc_delta.get("index", 0)
                                if idx not in tool_call_buffers:
                                    tool_call_buffers[idx] = {
                                        "id": tc_delta.get("id", ""),
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                buf = tool_call_buffers[idx]
                                if tc_delta.get("id"):
                                    buf["id"] = tc_delta["id"]
                                fn = tc_delta.get("function", {})
                                if fn.get("name"):
                                    buf["function"]["name"] = fn["name"]
                                if fn.get("arguments"):
                                    buf["function"]["arguments"] += fn["arguments"]

                        # ── Flush on finish_reason=tool_calls ──────────
                        finish = choice.get("finish_reason")
                        if finish == "tool_calls":
                            for _idx, tc in sorted(tool_call_buffers.items()):
                                yield tc
                            tool_call_buffers.clear()
        except httpx.HTTPStatusError as exc:
            raise TransportError(
                f"OpenAI-compat stream failed: {exc.response.status_code} "
                f"{exc.response.text[:300]}",
                provider="openai_compat",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise TransportError(
                f"OpenAI-compat stream request error: {exc}",
                provider="openai_compat",
            ) from exc

    async def chat(
        self,
        api_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: list[dict] | None = None,
    ) -> NormalizedResponse:
        """Non-streaming chat completion. Returns :class:`NormalizedResponse`."""
        url = _build_url(api_url)
        headers = _build_headers(api_key)
        payload = self.build_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        logger.info(
            "[openai_compat] chat model=%s tools=%d prompt=%d msgs=%d",
            model,
            len(tools) if tools else 0,
            len(system_prompt),
            len(messages),
        )

        trust_env = not _is_local_url(url)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0),
                trust_env=trust_env,
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise TransportError(
                f"OpenAI-compat chat failed: {exc.response.status_code} "
                f"{exc.response.text[:300]}",
                provider="openai_compat",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise TransportError(
                f"OpenAI-compat chat request error: {exc}",
                provider="openai_compat",
            ) from exc

        return self.normalize_response(data, stream=False)


# ── Self-registration ────────────────────────────────────────────────────
from paradise.transports import register_transport
register_transport("openai_compat", OpenAICompatTransport)
