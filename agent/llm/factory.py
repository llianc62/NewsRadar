from __future__ import annotations

from agent.types import LlmConfig

from .base_client import BaseLLMClient


def create_llm_client(cfg: LlmConfig) -> BaseLLMClient:
    """按 cfg.protocol 创建对应的协议 client。

    子模块懒导入：仅 import 本包（如测试收集）不会拉起 langchain_openai /
    langchain_anthropic，也不会在 API key 缺失时报错。只有真正构造某协议
    时才 import 对应包。
    """
    if cfg.protocol == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(cfg)
    if cfg.protocol == "openai":
        from .openai_client import OpenAIClient

        return OpenAIClient(cfg)
    raise ValueError(f"Unsupported LLM protocol: {cfg.protocol!r}")
