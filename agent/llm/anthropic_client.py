from __future__ import annotations

from langchain_anthropic import ChatAnthropic

from .base_client import BaseClient

__all__ = ["AnthropicClient"]


class AnthropicClient(BaseClient, ChatAnthropic):
    """Anthropic API 的 LLM Client。

    多继承 ``BaseClient`` + ``ChatAnthropic``，获得共享的 ``chat()`` / ``chat_stream()``
    实现。

    消息格式转换（OpenAI <-> Anthropic content blocks）、tool schema 转换
    （``parameters`` <-> ``input_schema``）全部由 LangChain 接管。

    直接返回 ``AIMessage``，不额外包装。
    """

    def __init__(self, api_key: str = "", base_url: str = "", model: str = "", **kwargs):
        super().__init__(
            api_key=api_key,
            base_url=base_url or None,
            model=model,
            **kwargs,
        )