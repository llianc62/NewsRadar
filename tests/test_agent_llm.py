"""Tests for agent/llm/ package - factory + per-protocol clients."""
import sys

import pytest

from agent.llm import BaseLLMClient, create_llm_client
from agent.llm.anthropic_client import AnthropicClient
from agent.llm.openai_client import OpenAIClient
from agent.types import LlmConfig


class TestCreateLlmClient:
    def test_unknown_protocol_raises(self):
        cfg = LlmConfig(protocol="unknown", model="x", api_key="sk-xxx")
        with pytest.raises(ValueError, match="Unsupported LLM protocol"):
            create_llm_client(cfg)

    def test_openai_returns_openai_client(self):
        cfg = LlmConfig(protocol="openai", model="gpt-4", api_key="sk-xxx")
        client = create_llm_client(cfg)
        assert isinstance(client, OpenAIClient)
        assert isinstance(client, BaseLLMClient)

    def test_anthropic_returns_anthropic_client(self):
        cfg = LlmConfig(protocol="anthropic", model="claude-sonnet-5", api_key="sk-xxx")
        client = create_llm_client(cfg)
        assert isinstance(client, AnthropicClient)
        assert isinstance(client, BaseLLMClient)

    def test_unknown_protocol_does_not_import_vendor_sdks(self):
        """懒导入回归：未知协议不应拉起 langchain_openai / langchain_anthropic。"""
        for mod in ("langchain_openai", "langchain_anthropic"):
            sys.modules.pop(mod, None)
        cfg = LlmConfig(protocol="unknown", model="x", api_key="sk-xxx")
        with pytest.raises(ValueError):
            create_llm_client(cfg)
        assert "langchain_openai" not in sys.modules
        assert "langchain_anthropic" not in sys.modules


class TestOpenAIClient:
    def test_get_llm_returns_chat_openai(self):
        cfg = LlmConfig(protocol="openai", model="gpt-4", api_key="sk-xxx", temperature=0.3)
        llm = OpenAIClient(cfg).get_llm()
        from langchain_openai import ChatOpenAI

        assert isinstance(llm, ChatOpenAI)

    def test_base_url_passed_when_set(self):
        cfg = LlmConfig(
            protocol="openai", model="gpt-4", api_key="sk-xxx", base_url="https://custom.api"
        )
        llm = OpenAIClient(cfg).get_llm()
        assert llm.openai_api_base == "https://custom.api"


class TestAnthropicClient:
    def test_get_llm_returns_chat_anthropic(self):
        cfg = LlmConfig(protocol="anthropic", model="claude-sonnet-5", api_key="sk-xxx")
        llm = AnthropicClient(cfg).get_llm()
        from langchain_anthropic import ChatAnthropic

        assert isinstance(llm, ChatAnthropic)

    def test_base_url_passed_when_set(self):
        cfg = LlmConfig(
            protocol="anthropic",
            model="claude-sonnet-5",
            api_key="sk-xxx",
            base_url="https://custom.api",
        )
        llm = AnthropicClient(cfg).get_llm()
        assert llm.anthropic_api_url == "https://custom.api"
