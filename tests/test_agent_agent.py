"""Tests for agent/v1/ — Phase 1: skeleton + mock LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent import (
    AgentResult,
    AnthropicClient,
    Context,
    DefaultAgent,
    DirectExecutor,
    Executor,
    ModelHub,
    OpenAIClient,
    ReActExecutor,
)


# ── helpers ──────────────────────────────────────────────────────


def _patch_hub(monkeypatch):
    """用 MockClient 替换 hub._build_client，避免真实 API / LangChain 调用。"""
    from agent import hub as hub_module

    def _fake_build(cfg):
        return MockClient(api_key=cfg.get("api_key", ""), base_url=cfg.get("base_url", ""))

    monkeypatch.setattr(hub_module, "_build_client", _fake_build)


class MockClient:
    """Mock LLM Client 返回固定响应，不调真实 API。

    实现 ``LLMClient`` 协议（``chat()`` / ``chat_stream()``），
    返回 ``AIMessage`` 而非 ``ChatResult``。
    """

    def __init__(self, api_key: str = "", base_url: str = ""):
        self.api_key = api_key
        self.base_url = base_url
        self.last_messages: list[dict] = []
        # 如果非空，chat() 会返回包含 tool_calls 的 AIMessage
        self.tool_calls_to_return: list[dict] | None = None

    async def chat(self, messages, tools=None, **kwargs) -> AIMessage:
        self.last_messages = messages
        if self.tool_calls_to_return:
            return AIMessage(
                content="",
                tool_calls=self.tool_calls_to_return,
            )
        return AIMessage(content="mock response for gpt-4o")

    async def chat_stream(self, messages, **kwargs) -> AsyncIterator[AIMessageChunk]:
        self.last_messages = messages
        for token in ["mock ", "response"]:
            yield AIMessageChunk(content=token)


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


# ── Test Client 构造 ──────────────────────────────────────────


class TestClientConstruction:
    def test_openai_client_imports(self):
        """OpenAIClient 可构造（仅验证导入路径和签名）。"""
        client = OpenAIClient(api_key="sk-xxx", model="gpt-4o")
        assert client.model_name == "gpt-4o"

    def test_anthropic_client_imports(self):
        client = AnthropicClient(api_key="sk-xxx", model="claude-sonnet-5")
        assert client.model == "claude-sonnet-5"


# ── Test ModelHub ────────────────────────────────────────────────


@pytest.fixture
def two_model_hub(monkeypatch):
    """用 MockClient 替换 _build_client，避免真实 API 调用。"""
    _patch_hub(monkeypatch)

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
    _patch_hub(monkeypatch)

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
    _patch_hub(monkeypatch)

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
        _patch_hub(monkeypatch)

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



# ═══════════════════════════════════════════════════════════════════
# Test ReActExecutor
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_hub_with_tools(monkeypatch):
    """用 MockClient 的 ModelHub，支持工具调用。"""
    _patch_hub(monkeypatch)

    return ModelHub(config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
    })


class TestReActExecutor:
    @pytest.mark.asyncio
    async def test_run_without_tools(self, mock_hub_with_tools):
        """无工具时直接返回文本。"""
        executor = ReActExecutor()
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        result = await executor.run(ctx=ctx, brain=mock_hub_with_tools)
        assert result == "mock response for gpt-4o"
        assert ctx.step_count == 1

    @pytest.mark.asyncio
    async def test_run_with_tools(self, mock_hub_with_tools):
        """有工具调用时，应执行工具并继续循环。"""
        from agent.tools import Registry, FunctionTool

        registry = Registry()
        registry.add_tool(FunctionTool(
            name="test_tool",
            description="A test tool",
            fn=lambda: "tool result",
            input_schema={},
        ))

        # 设置 MockClient 第一阶段返回工具调用，第二阶段返回文本
        client = mock_hub_with_tools.get_default()
        client.tool_calls_to_return = [
            {"name": "test_tool", "args": {}, "id": "call_1", "type": "tool_call"},
        ]

        executor = ReActExecutor(max_steps=3)
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        result = await executor.run(ctx=ctx, brain=mock_hub_with_tools, tools=registry)

        # 应执行工具并最终返回文本
        assert result
        assert ctx.step_count > 1

    @pytest.mark.asyncio
    async def test_run_with_tools_max_steps(self, mock_hub_with_tools):
        """超过 max_steps 应终止。"""
        from agent.tools import Registry, FunctionTool

        registry = Registry()
        registry.add_tool(FunctionTool(
            name="loop_tool",
            description="Always loops",
            fn=lambda: "still going",
            input_schema={},
        ))

        client = mock_hub_with_tools.get_default()
        client.tool_calls_to_return = [
            {"name": "loop_tool", "args": {}, "id": "call_1", "type": "tool_call"},
        ]

        executor = ReActExecutor(max_steps=2)
        ctx = Context(user_input="loop", system_prompt="You are a bot.")

        result = await executor.run(ctx=ctx, brain=mock_hub_with_tools, tools=registry)

        # 达到 max_steps，应有结果（非空）
        assert result
        assert ctx.step_count == 2

    @pytest.mark.asyncio
    async def test_run_stream(self, mock_hub_with_tools):
        """流式版本应最终返回结果。"""

        executor = ReActExecutor()
        ctx = Context(user_input="hi", system_prompt="You are a bot.")
        tokens = [t async for t in executor.run_stream(ctx=ctx, brain=mock_hub_with_tools)]
        # 注意：模拟流式拆分会在每个 token 后加空格
        assert "mock" in "".join(tokens)

    @pytest.mark.asyncio
    async def test_run_calls_memory_hooks(self, mock_hub_with_tools):
        """验证 ReActExecutor 也调用了 memory hook。"""
        from agent.memory import ShortTermMemory
        from unittest.mock import AsyncMock

        memory = AsyncMock(spec=ShortTermMemory)
        executor = ReActExecutor()
        ctx = Context(user_input="hello", system_prompt="You are a bot.")

        await executor.run(ctx=ctx, brain=mock_hub_with_tools, memory=memory)

        memory.on_before_execute.assert_awaited_once_with(ctx)
        memory.on_after_execute.assert_awaited_once_with(ctx)

    @pytest.mark.asyncio
    async def test_build_messages_with_memory_context(self):
        """验证 _build_initial_messages 包含 memory_context。"""

        ctx = Context(
            user_input="hello",
            system_prompt="You are a bot.",
        )
        ctx.memory_context = "user likes python"

        msgs = ReActExecutor._build_initial_messages(ctx)
        dicts = ReActExecutor._messages_to_dicts(msgs)
        assert len(dicts) == 3  # system + memory + user
        assert dicts[0]["role"] == "system"
        assert dicts[1]["role"] == "system"
        assert "相关记忆" in dicts[1]["content"]
        assert dicts[2]["role"] == "user"

    @pytest.mark.asyncio
    async def test_build_messages_with_history(self):
        """验证 _messages_to_dicts 能正确转换 tool 消息。"""
        from agent.models import Message

        msgs = [
            Message(role="system", content="You are a bot."),
            Message(role="user", content="hello"),
            Message(role="assistant", content=None, tool_calls=[
                {"name": "test", "args": {}, "id": "call_1"},
            ]),
            Message(role="tool", tool_call_id="call_1", content="ok", name="test"),
        ]

        dicts = ReActExecutor._messages_to_dicts(msgs)
        assert len(dicts) == 4
        assert dicts[0] == {"role": "system", "content": "You are a bot."}
        assert dicts[1] == {"role": "user", "content": "hello"}
        # tool_calls 应被转为 API 格式
        assert dicts[2]["role"] == "assistant"
        assert dicts[2]["content"] == ""  # DeepSeek 兼容：content 不能为 None
        assert len(dicts[2]["tool_calls"]) == 1
        assert dicts[2]["tool_calls"][0]["function"]["name"] == "test"
        assert dicts[3] == {"role": "tool", "tool_call_id": "call_1", "content": "ok"}