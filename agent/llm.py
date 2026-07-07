from __future__ import annotations
from langchain_core.language_models import BaseChatModel
from agent.types import LlmConfig


def build_llm(cfg: LlmConfig) -> BaseChatModel:
    common = {
        "model": cfg.model,
        "api_key": cfg.api_key,
        "temperature": cfg.temperature,
    }
    if cfg.base_url:
        common["base_url"] = cfg.base_url

    if cfg.protocol == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(**common)
    elif cfg.protocol == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(**common)
    else:
        raise ValueError(f"Unsupported LLM protocol: {cfg.protocol!r}")
