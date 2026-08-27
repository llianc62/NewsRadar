"""LLM Client 基类——提供共享的 chat() / chat_stream() 实现。

继承 LangChain ``BaseChatModel`` 作为基类，``bind_tools`` / ``ainvoke`` / ``astream``
来自 ``BaseChatModel``，由 MRO 解析到具体实现（``ChatOpenAI`` / ``ChatAnthropic`` /
``ChatDeepSeek``）。直接返回 ``AIMessage`` / ``AIMessageChunk``，不额外包装。

不再使用 ``ChatResult`` 中间格式。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk


class BaseClient(BaseChatModel):
    """LLM Client 基类。

    与 LangChain 具体类（``ChatOpenAI`` / ``ChatAnthropic`` / ``ChatDeepSeek``）多继承使用：

        class OpenAIClient(BaseClient, ChatOpenAI): ...

    ``bind_tools`` / ``ainvoke`` / ``astream`` 来自 ``BaseChatModel``，由 MRO 解析到具体实现。

    ``BaseClient`` 不定义 ``__init__``，MRO 自动路由到 LangChain 类的构造器。
    """

    async def chat(
        self,
        messages: list,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        """非流式调用，直接返回 ``AIMessage``。

        Args:
            messages: OpenAI 格式的消息 dict 列表（或 LangChain 消息对象列表）。
            tools: 可选工具 schema 列表（OpenAI format）。

        Returns:
            AIMessage: 包含 content、tool_calls、response_metadata、usage_metadata 等。
        """
        bound = self.bind_tools(tools) if tools else self
        return await bound.ainvoke(messages)

    def chat_stream(
        self,
        messages: list,
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        """流式调用，逐 chunk 返回 ``AIMessageChunk``。

        Args:
            messages: OpenAI 格式的消息 dict 列表（或 LangChain 消息对象列表）。
            tools: 可选工具 schema 列表（OpenAI format）。**流式同样必须绑定
                tools**：DeepSeek 等模型在请求不带 tools 时会把工具调用意图
                写成 DSML 等标记纯文本，而非结构化 ``tool_calls``。

        调用方需自行从 ``chunk.content`` 提取文本增量；工具调用通过聚合
        chunk（``AIMessageChunk`` 支持 ``+`` 合并）后从 ``agg.tool_calls``
        还原。
        """
        bound = self.bind_tools(tools) if tools else self
        return bound.astream(messages)