"""Tests for agent/v1/ — Phase 1: skeleton + mock LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from agent import (
    AgentResult,
    AnthropicClient,
    BaseClient,
    ChatResult,
    Context,
    DefaultAgent,
    DirectExecutor,
    Executor,
    ModelHub,
    OpenAIClient,
)


# ── helpers ──────────────────────────────────────────────────────


class MockClient(BaseClient):
    """Mock LLM Client 返回固定响应，不调真实 API。"""

    def __init__(self, api_key: str, base_url: str = ""):
        self.api_key = api_key
        self.base_url = base_url
        self.last_model: str = ""
        self.last_messages: list[dict] = []
        # 如果非空，chat() 会返回包含 tool_calls 的 ChatResult
        self.tool_calls_to_return: list[dict] | None = None

    async def chat(self, model, messages, temperature=0.7, top_p=1.0, **kwargs) -> ChatResult:
        self.last_model = model
        self.last_messages = messages
        if self.tool_calls_to_return:
            return ChatResult(content="", tool_calls=self.tool_calls_to_return)
        return ChatResult(content=f"mock response for {model}")

    async def chat_stream(self, model, messages, temperature=0.7, top_p=1.0, **kwargs) -> AsyncIterator[str]:
        self.last_model = model
        self.last_messages = messages
        for token in ["mock ", "response"]:
            yield token


# ── Test Context ─────────────────────────────────────────────────


class TestContext:
    def test_defaults(self):
        ctx = Context(user_input="hello")
        assert ctx.user_input == "hello"
        assert ctx.session_id == ""
        assert ctx.system_prompt == ""
        assert ctx.model_name == "default"
        assert ctx.assistant_output == ""
        assert ctx.step_count == 0
        assert ctx.model_used == ""
        assert ctx.total_tokens == 0

    def test_custom_values(self):
        ctx = Context(
            user_input="hi",
            session_id="sess-1",
            system_prompt="You are a helper.",
            model_name="cheap",
        )
        assert ctx.model_name == "cheap"
        assert ctx.system_prompt == "You are a helper."


# ── Test AgentResult ─────────────────────────────────────────────


class TestAgentResult:
    def test_creates_with_content(self):
        r = AgentResult(content="hello world")
        assert r.content == "hello world"
        assert r.model_used == ""
        assert r.total_tokens == 0
        assert r.tool_calls == []
        assert r.tool_results == []
        assert r.step_count == 0

    def test_with_metadata(self):
        r = AgentResult(content="hi", model_used="gpt-4o", total_tokens=100)
        assert r.model_used == "gpt-4o"
        assert r.total_tokens == 100

    def test_with_tool_calls(self):
        r = AgentResult(
            content="done",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}],
            tool_results=["北京 今日天气: Sunny 25°C"],
            step_count=2,
        )
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0]["function"]["name"] == "get_weather"
        assert "Sunny" in r.tool_results[0]
        assert r.step_count == 2


# ── Test BaseClient ABC ──────────────────────────────────────────


class TestBaseClient:
    def test_abstract_class_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            BaseClient(api_key="sk-xxx")  # type: ignore

    def test_concrete_subclass_works(self):
        client = MockClient(api_key="sk-xxx")
        assert client.api_key == "sk-xxx"

    def test_openai_client_imports(self):
        """OpenAIClient 可构造（仅验证导入路径和签名）。"""
        client = OpenAIClient(api_key="sk-xxx")
        assert isinstance(client, BaseClient)

    def test_anthropic_client_imports(self):
        client = AnthropicClient(api_key="sk-xxx")
        assert isinstance(client, BaseClient)


# ── Test ModelHub ────────────────────────────────────────────────


@pytest.fixture
def two_model_hub(monkeypatch):
    """用 MockClient 替换 _PROVIDER_MAP，避免真实 API 调用。"""
    from agent import hub as hub_module

    monkeypatch.setitem(hub_module._PROVIDER_MAP, "openai", MockClient)
    monkeypatch.setitem(hub_module._PROVIDER_MAP, "anthropic", MockClient)

    return ModelHub(config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
        "cheap": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-xxx"},
        "claude": {"protocol": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-xxx"},
    })


class TestModelHub:
    def test_get_default_returns_client(self, two_model_hub):
        client = two_model_hub.get_default()
        assert isinstance(client, MockClient)

    def test_get_returns_client(self, two_model_hub):
        client = two_model_hub.get("cheap")
        assert isinstance(client, MockClient)

    def test_get_lazy_creation(self, two_model_hub):
        """Client 在第一次 get 时才创建。"""
        assert len(two_model_hub._clients) == 0
        two_model_hub.get("default")
        assert len(two_model_hub._clients) == 1

    def test_get_reuses_client(self, two_model_hub):
        c1 = two_model_hub.get("default")
        c2 = two_model_hub.get("default")
        assert c1 is c2

    def test_get_unknown_raises(self, two_model_hub):
        with pytest.raises(KeyError, match="unknown_model"):
            two_model_hub.get("unknown_model")

    def test_get_default_without_default_config_raises(self):
        hub = ModelHub(config={
            "cheap": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-xxx"},
        })
        with pytest.raises(KeyError, match="default"):
            hub.get_default()

    def test_unknown_protocol_raises(self, monkeypatch):
        from agent import hub as hub_module

        monkeypatch.setitem(hub_module._PROVIDER_MAP, "openai", MockClient)

        hub = ModelHub(config={
            "default": {"protocol": "nonexistent", "model": "x", "api_key": "sk-xxx"},
        })
        with pytest.raises(ValueError, match="Unsupported protocol.*nonexistent"):
            hub.get_default()


# ── Test DirectExecutor ──────────────────────────────────────────


@pytest.fixture
def executor():
    return DirectExecutor()


@pytest.fixture
def mock_hub(monkeypatch):
    from agent import hub as hub_module

    monkeypatch.setitem(hub_module._PROVIDER_MAP, "openai", MockClient)

    return ModelHub(config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
    })


class TestDirectExecutor:
    @pytest.mark.asyncio
    async def test_run_returns_response(self, executor, mock_hub):
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        result = await executor.run(ctx, mock_hub)
        assert result == "mock response for gpt-4o"
        assert ctx.assistant_output == "mock response for gpt-4o"

    @pytest.mark.asyncio
    async def test_run_builds_messages_correctly(self, executor, mock_hub):
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        await executor.run(ctx, mock_hub)
        client = mock_hub._clients["default"]
        assert client.last_messages == [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "hello"},
        ]

    @pytest.mark.asyncio
    async def test_run_without_system_prompt(self, executor, mock_hub):
        ctx = Context(user_input="hello")
        await executor.run(ctx, mock_hub)
        client = mock_hub._clients["default"]
        assert client.last_messages == [
            {"role": "user", "content": "hello"},
        ]

    @pytest.mark.asyncio
    async def test_run_stream_yields_tokens(self, executor, mock_hub):
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        tokens = [t async for t in executor.run_stream(ctx, mock_hub)]
        assert tokens == ["mock ", "response"]
        assert ctx.assistant_output == "mock response"

    @pytest.mark.asyncio
    async def test_run_stream_without_system_prompt(self, executor, mock_hub):
        ctx = Context(user_input="hello")
        tokens = [t async for t in executor.run_stream(ctx, mock_hub)]
        assert tokens == ["mock ", "response"]
        client = mock_hub._clients["default"]
        assert client.last_messages == [
            {"role": "user", "content": "hello"},
        ]


# ── Test DefaultAgent ────────────────────────────────────────────


@pytest.fixture
def agent(monkeypatch):
    """返回一个使用 MockClient 的 DefaultAgent。"""
    from agent import hub as hub_module

    monkeypatch.setitem(hub_module._PROVIDER_MAP, "openai", MockClient)

    return DefaultAgent(
        config={
            "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
        },
        system_prompt="You are a helper.",
    )


class TestDefaultAgent:
    @pytest.mark.asyncio
    async def test_chat_returns_result(self, agent):
        result = await agent.chat("hello")
        assert isinstance(result, AgentResult)
        assert result.content == "mock response for gpt-4o"

    @pytest.mark.asyncio
    async def test_chat_with_custom_model_name(self, agent):
        with pytest.raises(KeyError):
            await agent.chat("hello", model_name="nonexistent")

    @pytest.mark.asyncio
    async def test_chat_stream_yields_tokens(self, agent):
        tokens = [t async for t in agent.chat_stream("hello")]
        assert tokens == ["mock ", "response"]

    @pytest.mark.asyncio
    async def test_chat_stream_returns_string_when_joined(self, agent):
        tokens = [t async for t in agent.chat_stream("hello")]
        assert "".join(tokens) == "mock response"

    def test_parse_config(self):
        """_parse_config 已移除，config 直接赋值到 _configs。"""
        hub = ModelHub(config={
            "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
        })
        assert hub._config["default"]["model"] == "gpt-4o"

    def test_parse_config_empty(self):
        hub = ModelHub(config={})
        assert hub._config == {}

    def test_default_executor(self, monkeypatch):
        """不传 executor 时自动使用 DirectExecutor。"""
        from agent import hub as hub_module

        monkeypatch.setitem(hub_module._PROVIDER_MAP, "openai", MockClient)

        agent = DefaultAgent(
            config={
                "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
            },
        )
        assert isinstance(agent.executor, DirectExecutor)

    def test_agent_requires_config(self):
        """config 是必传参数。"""
        with pytest.raises(TypeError):
            DefaultAgent()  # type: ignore


# ── Test Executor ABC ────────────────────────────────────────────


class TestExecutorABC:
    def test_abstract_class_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            Executor()  # type: ignore
