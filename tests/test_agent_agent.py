"""Tests for agent/v1/ — Phase 1: skeleton + mock LLM."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent import (
    AgentResult,
    AnthropicClient,
    Context,
    DefaultAgent,
    DirectExecutor,
    Executor,
    MemoryModule,
    ModelHub,
    NullMemory,
    OpenAIClient,
    ReActExecutor,
)
from agent.data import MemoryBlock, Message


# ── helpers ──────────────────────────────────────────────────────


def _patch_hub(monkeypatch):
    """用 MockClient 替换 hub._build_client，避免真实 API / LangChain 调用。"""
    from agent import model_hub as hub_module

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


def make_ai(content="", tool_calls=None):
    """Create an AIMessage-like object for executor _loop tests.

    Normalizes tool_calls to langchain ToolCall format (name/args/id/type).
    """
    tcs = []
    for tc in (tool_calls or []):
        tcs.append({
            "name": tc["name"],
            "args": tc.get("args", {}),
            "id": tc.get("id", ""),
            "type": "tool_call",
        })
    return AIMessage(content=content, tool_calls=tcs)


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
    async def test_build_messages_with_history(self):
        """验证 _messages_to_dicts 能正确转换 tool 消息。"""
        from agent.data import Message

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


# ── Test ReActExecutor _prepare + _build_llm_messages (Task 7) ────


@pytest.fixture
def mock_brain():
    """Mock ModelHub for executor tests.

    Self-referential: brain.get() returns brain itself, so brain.chat
    is the client chat method. Tests set chat.return_value / chat.side_effect.
    """
    brain = MagicMock()
    brain.get.return_value = brain
    brain.get_model_version.return_value = "test-model-v1"
    brain.chat = AsyncMock(return_value=make_ai(content="", tool_calls=[]))
    return brain


@pytest.fixture
def mock_tools():
    """Mock tool registry. execute() returns a string; get_schemas() returns None."""
    tools = MagicMock()
    tools.execute = AsyncMock(return_value="result text")
    tools.get_schemas.return_value = None
    return tools


@pytest.mark.asyncio
async def test_prepare_loads_memory_and_assembles_messages(mock_brain):
    memory = NullMemory()
    ex = ReActExecutor(brain=mock_brain, memory=memory)
    ctx = Context(user_input="hi", session_id="s1", system_prompt="sys")
    await ex._prepare(ctx)
    assert len(ctx.messages) == 1
    assert ctx.messages[0].role == "user"
    assert ctx.messages[0].content == "hi"


def test_build_llm_messages_order():
    ex = ReActExecutor(brain=None, memory=NullMemory())
    ctx = Context(system_prompt="S", user_input="U")
    ctx.memories = [
        MemoryBlock(title="知识库", content="K", source="knowledge", order=20),
        MemoryBlock(title="相关记忆", content="M", source="memory", order=10),
    ]
    ctx.history_messages = [Message(role="user", content="old")]
    ctx.messages = [Message(role="user", content="U")]
    msgs = ex._build_llm_messages(ctx)
    # system_prompt -> memories(order 10, 20) -> history -> messages
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "S"
    assert "M" in msgs[1]["content"]   # memory order=10 先
    assert "K" in msgs[2]["content"]   # knowledge order=20 后
    assert msgs[3]["content"] == "old"  # history
    assert msgs[4]["content"] == "U"    # current messages


# ── Test ReActExecutor _loop + _execute_tool (Task 8) ─────────────


@pytest.mark.asyncio
async def test_loop_single_text_response(mock_brain):
    mock_brain.chat.return_value = make_ai(content="answer", tool_calls=[])
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory())
    ctx = Context(user_input="hi")
    await ex._prepare(ctx)
    await ex._loop(ctx, stream=False)
    assert ctx.final_output == "answer"
    assert any(m.role == "assistant" for m in ctx.messages)


@pytest.mark.asyncio
async def test_loop_tool_then_answer(mock_brain, mock_tools):
    mock_brain.chat.side_effect = [
        make_ai(content="", tool_calls=[{"name": "search_news", "args": {}, "id": "c1"}]),
        make_ai(content="final", tool_calls=[]),
    ]
    mock_tools.execute.return_value = "result text"
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), tools=mock_tools)
    ctx = Context(user_input="hi")
    await ex._prepare(ctx)
    await ex._loop(ctx, stream=False)
    assert ctx.final_output == "final"
    tool_msgs = [m for m in ctx.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_result.success is True
    assert tool_msgs[0].tool_result.result == "result text"


@pytest.mark.asyncio
async def test_loop_tool_failure_continues(mock_brain, mock_tools):
    mock_brain.chat.side_effect = [
        make_ai(content="", tool_calls=[{"name": "bad", "args": {}, "id": "c1"}]),
        make_ai(content="recovered", tool_calls=[]),
    ]
    mock_tools.execute.side_effect = RuntimeError("boom")
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), tools=mock_tools, tool_max_retries=0)
    ctx = Context(user_input="hi")
    await ex._prepare(ctx)
    await ex._loop(ctx, stream=False)
    assert ctx.final_output == "recovered"
    tool_msgs = [m for m in ctx.messages if m.role == "tool"]
    assert tool_msgs[0].tool_result.success is False
    assert "boom" in tool_msgs[0].tool_result.error


# ── Test ReActExecutor _finalize + run/run_stream (Task 9) ────────


def make_chat_stream(tokens):
    """Create a mock chat_stream async-generator factory.

    ``client.chat_stream(messages=...)`` returns an async iterator yielding
    ``AIMessageChunk`` with the given token contents.
    """
    async def _stream(*args, **kwargs):
        for tok in tokens:
            yield AIMessageChunk(content=tok)
    return _stream


@pytest.fixture
def mock_memory():
    """Mock MemoryModule -- load/save are awaitable AsyncMocks."""
    memory = MagicMock(spec=MemoryModule)
    memory.load = AsyncMock(return_value=None)
    memory.save = AsyncMock(return_value=None)
    return memory


@pytest.mark.asyncio
async def test_run_normal(mock_brain):
    mock_brain.chat.return_value = make_ai(content="answer", tool_calls=[])
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory())
    ctx = Context(user_input="hi")
    output = await ex.run(ctx)
    assert output == "answer"


@pytest.mark.asyncio
async def test_run_llm_failure_returns_error_text(mock_brain):
    mock_brain.chat.side_effect = RuntimeError("api down")
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory(), llm_max_retries=0)
    ctx = Context(user_input="hi")
    output = await ex.run(ctx)
    assert "Executor 错误" in output
    assert "api down" in output


@pytest.mark.asyncio
async def test_run_calls_memory_save(mock_brain, mock_memory):
    mock_brain.chat.return_value = make_ai(content="a", tool_calls=[])
    ex = ReActExecutor(brain=mock_brain, memory=mock_memory)
    ctx = Context(user_input="hi", session_id="s1")
    await ex.run(ctx)
    mock_memory.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_stream_yields_tokens(mock_brain):
    mock_brain.chat.return_value = make_ai(content="hello world", tool_calls=[])
    mock_brain.chat_stream = make_chat_stream(["hello ", "world"])
    ex = ReActExecutor(brain=mock_brain, memory=NullMemory())
    ctx = Context(user_input="hi")
    tokens = [t async for t in ex.run_stream(ctx)]
    assert "".join(tokens).startswith("hello")