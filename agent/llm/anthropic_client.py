from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .base_client import BaseClient, ChatResult


class AnthropicClient(BaseClient):
    """Anthropic API 的 Client。"""

    def __init__(self, api_key: str, base_url: str = ""):
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key, base_url=base_url or None)

    async def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        top_p: float = 1.0,
        **kwargs: Any,
    ) -> ChatResult:
        # 拆出 system 消息（Anthropic API 单独传）
        system = None
        filtered_messages = messages
        if messages and messages[0].get("role") == "system":
            system = messages[0]["content"]
            filtered_messages = messages[1:]

        msg = self._client.messages.create(
            model=model,
            messages=filtered_messages,
            system=system,
            max_tokens=kwargs.pop("max_tokens", 4096),
            temperature=temperature,
            **kwargs,
        )
        content = msg.content[0].text if msg.content else ""
        return ChatResult(content=content)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        top_p: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        system = None
        filtered_messages = messages
        if messages and messages[0].get("role") == "system":
            system = messages[0]["content"]
            filtered_messages = messages[1:]

        with self._client.messages.stream(
            model=model,
            messages=filtered_messages,
            system=system,
            max_tokens=kwargs.pop("max_tokens", 4096),
            temperature=temperature,
            **kwargs,
        ) as stream:
            for text in stream.text_stream:
                yield text
