from __future__ import annotations
from abc import ABC, abstractmethod

from langchain_core.language_models import BaseChatModel

from agent.types import LlmConfig


class BaseLLMClient(ABC):
    """构造期抽象：把 LlmConfig 配成可用的 langchain 模型。

    每协议一个子类。vendor 专属 kwargs 的门控（如 Anthropic 的 thinking、
    OpenAI 的 reasoning_effort）放在具体子类的 get_llm() 里。归一化、model
    校验等待未来需要时再加。

    get_llm() 产出 BaseChatModel 后即退场；调用方继续用 langchain 接口
    （invoke / astream / with_structured_output / bind_tools）。
    """

    def __init__(self, cfg: LlmConfig):
        self.cfg = cfg

    @abstractmethod
    def get_llm(self) -> BaseChatModel:
        """Return a configured langchain chat model."""
