"""Tests for agent/llm.py."""
import pytest
from agent.types import LlmConfig
from agent.llm import build_llm


class TestBuildLlm:
    def test_raises_on_unknown_protocol(self):
        cfg = LlmConfig(protocol="unknown", model="x", api_key="sk-xxx")
        with pytest.raises(ValueError, match="Unsupported LLM protocol"):
            build_llm(cfg)

    def test_build_openai_returns_chat_openai(self):
        cfg = LlmConfig(protocol="openai", model="gpt-4", api_key="sk-xxx")
        llm = build_llm(cfg)
        from langchain_openai import ChatOpenAI
        assert isinstance(llm, ChatOpenAI)

    def test_build_anthropic_returns_chat_anthropic(self):
        cfg = LlmConfig(protocol="anthropic", model="claude-sonnet-5", api_key="sk-xxx")
        llm = build_llm(cfg)
        from langchain_anthropic import ChatAnthropic
        assert isinstance(llm, ChatAnthropic)

    def test_base_url_passed_when_set(self):
        cfg = LlmConfig(protocol="openai", model="gpt-4", api_key="sk-xxx", base_url="https://custom.api")
        llm = build_llm(cfg)
        # ChatOpenAI stores base_url — check it's set
        assert llm.openai_api_base == "https://custom.api"
