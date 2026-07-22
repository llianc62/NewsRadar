from __future__ import annotations

from langchain_openai import ChatOpenAI

from .base_client import BaseClient

__all__ = ["OpenAIClient"]


class OpenAIClient(BaseClient, ChatOpenAI):
    """OpenAI 兼容 API 的 LLM Client。

    多继承 ``BaseClient`` + ``ChatOpenAI``，获得共享的 ``chat()`` / ``chat_stream()``
    实现（直接返回 ``AIMessage``，不额外包装）。

    支持 OpenAI 官方 API 及所有兼容端点（DeepSeek、Ollama、OpenRouter 等）。
    """

    def __init__(self, api_key: str = "", base_url: str = "", model: str = "", **kwargs):
        super().__init__(
            api_key=api_key,
            base_url=base_url or None,
            model=model,
            **kwargs,
        )