"""Ollama native API transport — /api/chat with NDJSON streaming.

Implements ProviderTransport for the Ollama native protocol.  Every request
uses ``trust_env=False`` to avoid proxying local Ollama instances.

Source logic refactored from core.llm_router.stream_ollama / chat_ollama.
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
)

logger = logging.getLogger(__name__)


class OllamaNativeTransport(ProviderTransport):
    """Transport for Ollama native API (/api/chat, NDJSON streaming)."""

    # ── ProviderTransport ABC ─────────────────────────────────────

    @property
    def api_mode(self) -> str:
        return "ollama_native"

    def convert_messages(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Prepend system message (if provided) to the message list.

        Ollama uses the same message format as OpenAI, so no other conversion
        is needed.
        """
        if system_prompt:
            return [{"role": "system", "content": system_prompt}] + list(messages)
        return list(messages)

    def convert_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Pass tools through unchanged — Ollama uses the same format."""
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stream: bool = False,
        **params,
    ) -> Dict[str, Any]:
        """Build the complete payload for Ollama /api/chat."""
        ollama_messages = self.convert_messages(messages, system_prompt=system_prompt)

        payload: Dict[str, Any] = {
            "model": model,
            "messages": ollama_messages,
            "stream": stream,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "repeat_penalty": 1.15,
                "top_p": 0.9,
            },
        }

        if tools:
            payload["tools"] = self.convert_tools(tools)
            # Note: Ollama does not support tool_choice parameter

        return payload

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalize an Ollama /api/chat (non-streaming) response.

        Expected *response* shape (already ``dict`` from ``resp.json()``)::

            {
              "message": {
                "role": "assistant",
                "content": "...",
                "tool_calls": [{"function": {"name": "...", "arguments": {...}}}]
              }
            }
        """
        if isinstance(response, dict):
            data = response
        else:
            raise TransportError(
                f"Unexpected Ollama response type: {type(response).__name__}",
                provider="ollama_native",
            )

        msg = data.get("message", {})
        content = msg.get("content", "") or ""
        raw_tool_calls = msg.get("tool_calls")

        tool_calls: list[ToolCall] | None = None
        if raw_tool_calls:
            tool_calls = []
            for idx, tc in enumerate(raw_tool_calls):
                func = tc.get("function", {})
                tool_calls.append(
                    build_tool_call(
                        id=tc.get("id"),
                        name=func.get("name", ""),
                        arguments=func.get("arguments", {}),
                    )
                )

        # Determine finish reason
        if tool_calls:
            finish_reason = "tool_calls"
        elif data.get("done_reason") == "length":
            finish_reason = "length"
        else:
            finish_reason = "stop"

        # Usage stats (Ollama includes eval_count / prompt_eval_count)
        usage: Usage | None = None
        eval_count = data.get("eval_count")
        prompt_eval_count = data.get("prompt_eval_count")
        if eval_count is not None or prompt_eval_count is not None:
            usage = Usage(
                prompt_tokens=prompt_eval_count or 0,
                completion_tokens=eval_count or 0,
                total_tokens=(prompt_eval_count or 0) + (eval_count or 0),
            )

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    # ── High-level convenience methods ────────────────────────────

    async def stream_chat(
        self,
        api_url: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        """Stream chat via Ollama native API (/api/chat, NDJSON).

        Based on llm_router.stream_ollama.  Always uses trust_env=False.
        """
        url = f"{api_url.rstrip('/')}/api/chat"
        ollama_messages = self.convert_messages(messages, system_prompt=system_prompt)

        payload = {
            "model": model,
            "messages": ollama_messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": 512,
                "repeat_penalty": 1.15,
                "top_p": 0.9,
            },
        }

        logger.info(
            "[OllamaNative] stream_chat model=%s prompt=%d msgs=%d",
            model,
            len(system_prompt),
            len(messages),
        )

        try:
            async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            if data.get("done"):
                                break
                            content = data.get("message", {}).get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
        except httpx.HTTPStatusError as exc:
            raise TransportError(
                f"Ollama stream HTTP {exc.response.status_code}: {exc.message}",
                provider="ollama_native",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Ollama stream request failed: {exc}",
                provider="ollama_native",
            ) from exc

    async def chat(
        self,
        api_url: str,
        model: str,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: list[dict] | None = None,
    ) -> NormalizedResponse:
        """Non-streaming chat via Ollama native API.

        Based on llm_router.chat_ollama.  Always uses trust_env=False.
        Returns a NormalizedResponse with content and tool_calls.
        """
        url = f"{api_url.rstrip('/')}/api/chat"
        ollama_messages = self.convert_messages(messages, system_prompt=system_prompt)

        payload = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "repeat_penalty": 1.15,
                "top_p": 0.9,
            },
        }
        if tools:
            payload["tools"] = self.convert_tools(tools)

        logger.info(
            "[OllamaNative] chat model=%s tools=%d prompt=%d msgs=%d",
            model,
            len(tools) if tools else 0,
            len(system_prompt),
            len(messages),
        )

        try:
            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise TransportError(
                f"Ollama chat HTTP {exc.response.status_code}: {exc.message}",
                provider="ollama_native",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Ollama chat request failed: {exc}",
                provider="ollama_native",
            ) from exc

        return self.normalize_response(data)


# ── Self-registration ────────────────────────────────────────────────────
from paradise.transports import register_transport
register_transport("ollama_native", OllamaNativeTransport)
