from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from .base_client import BaseLLMClient


class OpenAIClient(BaseLLMClient):
    """OpenAI Chat Completions 协议客户端（含所有 OpenAI-compatible 端点）。

    未来扩展点：reasoning_effort 门控、Responses API、归一化等都在这里加。
    """

    def get_llm(self) -> BaseChatModel:
        kwargs = {
            "model": self.cfg.model,
            "api_key": self.cfg.api_key,
            "temperature": self.cfg.temperature,
        }
        if self.cfg.base_url:
            kwargs["base_url"] = self.cfg.base_url
        return ChatOpenAI(**kwargs)
