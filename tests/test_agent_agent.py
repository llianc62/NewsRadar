"""Tests for agent/agent.py."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from agent.types import LlmConfig
from agent.agent import Agent


class FakeChunk:
    def __init__(self, content):
        self.content = content


class AstreamMock(AsyncMock):
    """AsyncMock that returns an async generator iterating over ``return_value``."""

    async def _gen(self):
        for item in self.return_value:
            yield item

    def __call__(self, *args, **kwargs):
        return self._gen()


class TestAgent:
    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock()
        llm.astream = AstreamMock()
        llm.ainvoke = AsyncMock()
        return llm

    @pytest.fixture
    def agent(self, mock_llm):
        with patch("agent.agent.build_llm", return_value=mock_llm):
            cfg = LlmConfig(protocol="openai", model="gpt-4", api_key="sk-xxx")
            return Agent(cfg)

    @pytest.mark.asyncio
    async def test_chat_stream_yields_tokens(self, agent, mock_llm):
        mock_llm.astream.return_value = [
            FakeChunk("Hello"),
            FakeChunk(" world"),
            FakeChunk(""),
            FakeChunk("!"),
        ]
        tokens = []
        async for token in agent.chat_stream("Hi"):
            tokens.append(token)
        assert tokens == ["Hello", " world", "!"]

    @pytest.mark.asyncio
    async def test_chat_stream_skips_empty_content(self, agent, mock_llm):
        mock_llm.astream.return_value = [
            FakeChunk(""),
            FakeChunk(None),
            FakeChunk("ok"),
        ]
        tokens = []
        async for token in agent.chat_stream("Hi"):
            tokens.append(token)
        assert tokens == ["ok"]

    @pytest.mark.asyncio
    async def test_chat_returns_full_response(self, agent, mock_llm):
        response = MagicMock()
        response.content = "Full response"
        mock_llm.ainvoke.return_value = response

        result = await agent.chat("Hello")
        assert result == "Full response"
        mock_llm.ainvoke.assert_called_once_with("Hello")
