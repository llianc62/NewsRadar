"""LLM Client implementations.

每个 client 继承 ``BaseClient`` + 对应 LangChain 具体类（``ChatOpenAI`` / ``ChatAnthropic`` / ``ChatDeepSeek``），
直接返回 ``AIMessage``，不额外包装。``BaseClient`` 提供共享的 ``chat()`` 与 ``chat_stream()`` 实现。

不再使用 ``BaseClient`` 门面与 ``ChatResult`` 中间格式。
"""

from .anthropic_client import AnthropicClient
from .base_client import BaseClient
from .deepseek_client import DeepSeekClient
from .openai_client import OpenAIClient

__all__ = [
    "BaseClient",
    "OpenAIClient",
    "AnthropicClient",
    "DeepSeekClient",
]