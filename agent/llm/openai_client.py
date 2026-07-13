from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .base_client import BaseClient, ChatResult


class OpenAIClient(BaseClient):
    """OpenAI 兼容 API 的 Client。"""

    def __init__(self, api_key: str, base_url: str = ""):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url or None)

    async def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        top_p: float = 1.0,
        **kwargs: Any,
    ) -> ChatResult:
        resp = self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            **kwargs,
        )
        msg = resp.choices[0].message
        content = msg.content or ""
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })
        return ChatResult(content=content, tool_calls=tool_calls)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        top_p: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        stream = self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            stream=True,
            **kwargs,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
