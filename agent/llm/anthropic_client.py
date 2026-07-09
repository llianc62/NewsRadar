from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from .base_client import BaseLLMClient


class AnthropicClient(BaseLLMClient):
    """Anthropic Messages API 协议客户端。

    未来扩展点：extended thinking 的 thinking/effort 门控、prompt caching、
    归一化（开 thinking 后 content 会变成 list[dict]）都在这里加。
    """

    def get_llm(self) -> BaseChatModel:
        kwargs = {
            "model": self.cfg.model,
            "api_key": self.cfg.api_key,
            "temperature": self.cfg.temperature,
        }
        if self.cfg.base_url:
            kwargs["base_url"] = self.cfg.base_url
        return ChatAnthropic(**kwargs)
