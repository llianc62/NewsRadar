from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatResult:
    """LLM chat 调用的结构化返回。

    替代纯 str 返回，同时携带文本内容和工具调用信息。
    当 LLM 决定调用工具时，content 可能为空，tool_calls 非空。
    """
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)


class BaseClient(ABC):
    """所有 LLM Client 的基类。

    构造只收连接级参数，model 在每次调用时传入。
    同一个 Client 实例可切换不同模型。
    """

    def __init__(self, api_key: str, base_url: str = ""):
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        top_p: float = 1.0,
        **kwargs: Any,
    ) -> ChatResult:
        """非流式调用，返回结构化结果（文本 + 工具调用）。"""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        top_p: float = 1.0,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """流式调用，逐 token 返回。"""
        ...
