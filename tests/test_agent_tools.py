"""Tests for agent/tools/ — ToolDef, FunctionTool, MCPClient, Registry."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import (
    BaseTool,
    ChatResult,
    Context,
    DirectExecutor,
    FunctionTool,
    MCPClient,
    MCPTool,
    ReActExecutor,
    ToolCallRecord,
    ToolDef,
    Registry,
    tool,
)
from agent.hub import ModelHub
from tests.test_agent_agent import MockClient


# ── Test ToolDef ─────────────────────────────────────────────────


class TestToolDef:
    def test_create(self):
        td = ToolDef(
            name="test_tool",
            description="A test tool",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
        )
        assert td.name == "test_tool"
        assert td.description == "A test tool"
        assert td.input_schema["required"] == ["x"]

    def test_default_schema(self):
        td = ToolDef(name="simple", description="Simple tool")
        assert td.input_schema == {}

    def test_immutable(self):
        td = ToolDef(name="immutable", description="Test")
        with pytest.raises(AttributeError):
            td.name = "changed"  # frozen dataclass


# ── Test ToolCallRecord ──────────────────────────────────────────


class TestToolCallRecord:
    def test_create(self):
        tc = ToolCallRecord(name="calc", args={"a": 1, "b": 2})
        assert tc.name == "calc"
        assert tc.args == {"a": 1, "b": 2}
        assert tc.result == ""
        assert tc.error == ""

    def test_with_result(self):
        tc = ToolCallRecord(name="calc", args={}, result="42")
        assert tc.result == "42"


# ── Test BaseTool ABC ────────────────────────────────────────────


class TestBaseTool:
    def test_abstract_class_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            BaseTool()  # type: ignore


# ── Test FunctionTool ────────────────────────────────────────────


class TestFunctionTool:
    def test_create(self):
        tool = FunctionTool(
            name="echo",
            description="Echo input back",
            fn=lambda x: x,
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
        )
        assert tool.get_def().name == "echo"
        assert tool.get_def().description == "Echo input back"

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="name must not be empty"):
            FunctionTool(name="", description="test", fn=lambda: "", input_schema={})

    def test_empty_description_raises(self):
        with pytest.raises(ValueError, match="description must not be empty"):
            FunctionTool(name="test", description="", fn=lambda: "", input_schema={})

    @pytest.mark.asyncio
    async def test_execute_sync_function(self):
        tool = FunctionTool(
            name="add",
            description="Add two numbers",
            fn=lambda a, b: a + b,
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                },
                "required": ["a", "b"],
            },
        )
        result = await tool.execute(a=3, b=4)
        assert result == "7"

    @pytest.mark.asyncio
    async def test_execute_async_function(self):
        async def async_add(a, b):
            await asyncio.sleep(0.01)
            return a + b

        tool = FunctionTool(
            name="async_add",
            description="Async add",
            fn=async_add,
            input_schema={},
        )
        result = await tool.execute(a=10, b=20)
        assert result == "30"

    @pytest.mark.asyncio
    async def test_execute_error_handling(self):
        def failing_fn():
            raise ValueError("something went wrong")

        tool = FunctionTool(
            name="failing",
            description="Always fails",
            fn=failing_fn,
            input_schema={},
        )
        result = await tool.execute()
        assert "Error executing tool" in result
        assert "something went wrong" in result

    def test_get_def_returns_tooldef(self):
        tool = FunctionTool(
            name="greet",
            description="Greet someone",
            fn=lambda name: f"Hello {name}",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        )
        td = tool.get_def()
        assert isinstance(td, ToolDef)
        assert td.name == "greet"


# ── Test MCPClient (mock stdio) ──────────────────────────────────


class TestMCPClient:
    @pytest.mark.asyncio
    async def test_create(self):
        client = MCPClient(name="test-client")
        assert client.name == "test-client"
        assert not client.is_connected

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="name must not be empty"):
            MCPClient(name="")

    def test_get_tools_returns_empty_list_before_connect(self):
        client = MCPClient(name="test")
        assert client.get_tools() == []

    def test_get_schemas_returns_empty_list_before_connect(self):
        client = MCPClient(name="test")
        assert client.get_schemas() == []

    def test_has_tool_returns_false_before_connect(self):
        client = MCPClient(name="test")
        assert not client.has_tool("anything")


# ── Test Registry ────────────────────────────────────────────


class TestRegistry:
    def test_empty_registry(self):
        registry = Registry()
        assert registry.get_schemas() == []
        assert registry.list_tools() == []

    def test_add_function_tool(self):
        registry = Registry()
        tool = FunctionTool(
            name="echo",
            description="Echo back",
            fn=lambda x: x,
            input_schema={"type": "object", "properties": {"x": {}}, "required": ["x"]},
        )
        registry.add_tool(tool)
        assert registry.list_tools() == ["echo"]
        assert len(registry.get_schemas()) == 1

    def test_add_duplicate_tool_raises(self):
        registry = Registry()
        tool = FunctionTool(
            name="echo", description="Echo", fn=lambda x: x, input_schema={},
        )
        registry.add_tool(tool)
        with pytest.raises(ValueError, match="already registered"):
            registry.add_tool(tool)

    def test_add_invalid_type_raises(self):
        registry = Registry()
        with pytest.raises(TypeError, match="Expected BaseTool"):
            registry.add_tool("not_a_tool")  # type: ignore

    def test_get_tool_returns_none_for_unknown(self):
        registry = Registry()
        assert registry.get_tool("unknown") is None

    def test_get_tool_returns_instance(self):
        registry = Registry()
        tool = FunctionTool(name="ping", description="Ping", fn=lambda: "pong", input_schema={})
        registry.add_tool(tool)
        assert registry.get_tool("ping") is tool

    def test_remove_tool(self):
        registry = Registry()
        tool = FunctionTool(name="tmp", description="Temp", fn=lambda: "", input_schema={})
        registry.add_tool(tool)
        registry.remove_tool("tmp")
        assert registry.list_tools() == []

    def test_remove_unknown_raises(self):
        registry = Registry()
        with pytest.raises(KeyError, match="not found"):
            registry.remove_tool("unknown")

    @pytest.mark.asyncio
    async def test_execute_function_tool(self):
        registry = Registry()
        tool = FunctionTool(
            name="add",
            description="Add",
            fn=lambda a, b: a + b,
            input_schema={
                "type": "object",
                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"],
            },
        )
        registry.add_tool(tool)
        result = await registry.execute("add", {"a": 5, "b": 3})
        assert result == "8"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_raises(self):
        registry = Registry()
        with pytest.raises(KeyError, match="not found"):
            await registry.execute("unknown", {})

    def test_get_schemas_format(self):
        registry = Registry()
        tool = FunctionTool(
            name="calculator",
            description="A calculator",
            fn=lambda a, b, op: 0,
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                    "op": {"type": "string", "enum": ["+", "-"]},
                },
                "required": ["a", "b", "op"],
            },
        )
        registry.add_tool(tool)
        schemas = registry.get_schemas()
        assert len(schemas) == 1
        s = schemas[0]
        assert s["type"] == "function"
        assert s["function"]["name"] == "calculator"
        assert s["function"]["description"] == "A calculator"
        assert "parameters" in s["function"]
        assert s["function"]["parameters"]["required"] == ["a", "b", "op"]

    def test_add_mcp_not_connected_raises(self):
        registry = Registry()
        client = MCPClient(name="disconnected")
        with pytest.raises(RuntimeError, match="not connected"):
            registry.add_mcp(client)

    @pytest.mark.asyncio
    async def test_add_mcp_connected(self):
        """Test add_mcp with a properly connected MCPClient."""
        registry = Registry()

        # Use news_server via subprocess
        client = MCPClient(name="news")
        await client.connect_stdio("python", "-m", "agent.mcp.news_server")

        registry.add_mcp(client)
        assert "search_news" in registry.list_tools()
        assert "get_hot_topics" in registry.list_tools()

        # Test executing an MCP tool that doesn't need DB
        result = await registry.execute("analyze_sentiment", {"text": "好优秀成功"})
        assert "score" in result

        await client.close()


# ── Test ReActExecutor ───────────────────────────────────────────


@pytest.fixture
def mock_hub_with_tools(monkeypatch):
    from agent import hub as hub_module

    monkeypatch.setitem(hub_module._PROVIDER_MAP, "openai", MockClient)
    return ModelHub(config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "sk-xxx"},
    })


class TestReActExecutor:
    def test_invalid_max_steps(self):
        with pytest.raises(ValueError, match="max_steps"):
            ReActExecutor(max_steps=0)

    @pytest.mark.asyncio
    async def test_run_without_tools(self, mock_hub_with_tools):
        """不带工具时，ReActExecutor 应退化为直调。"""
        executor = ReActExecutor()
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        result = await executor.run(ctx=ctx, brain=mock_hub_with_tools)
        assert result == "mock response for gpt-4o"
        assert ctx.assistant_output == "mock response for gpt-4o"
        assert ctx.step_count == 1

    @pytest.mark.asyncio
    async def test_run_with_tools_calls_tool(self, mock_hub_with_tools):
        """验证 ReActExecutor 能解析 tool_call 并执行工具。"""
        registry = Registry()
        registry.add_tool(FunctionTool(
            name="get_weather",
            description="Get weather",
            fn=lambda city: f"Weather in {city}: sunny, 25°C",
            input_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        ))

        # 设置 MockClient 先返回 tool_call，再返回最终文本
        client = mock_hub_with_tools.get_default()
        client.tool_calls_to_return = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "北京"}',
                },
            },
        ]

        executor = ReActExecutor(max_steps=5)
        ctx = Context(user_input="北京天气怎么样？", system_prompt="You are a weather bot.")

        result = await executor.run(ctx=ctx, brain=mock_hub_with_tools, tools=registry)

        # 验证工具调用被记录
        assert len(ctx.tool_calls) >= 1
        assert len(ctx.tool_results) >= 1
        assert "北京" in ctx.tool_results[0] or "sunny" in ctx.tool_results[0]

        # 验证 history 包含了工具结果
        assert any("工具 get_weather 返回" in m["content"] for m in ctx.history)

        # 验证 LLM 最终调用了 2 次（tool_call + 文本返回）
        assert ctx.step_count > 1

    @pytest.mark.asyncio
    async def test_run_with_tools_max_steps(self, mock_hub_with_tools):
        """超过 max_steps 应终止。"""
        registry = Registry()
        registry.add_tool(FunctionTool(
            name="loop_tool",
            description="Always loops",
            fn=lambda: "still going",
            input_schema={},
        ))

        client = mock_hub_with_tools.get_default()
        client.tool_calls_to_return = [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "loop_tool",
                    "arguments": "{}",
                },
            },
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
        assert "".join(tokens) == "mock response for gpt-4o"

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
        """验证 _build_messages 包含 memory_context。"""
        ctx = Context(
            user_input="hello",
            system_prompt="You are a bot.",
        )
        ctx.memory_context = "user likes python"

        messages = ReActExecutor._build_messages(ctx)
        assert len(messages) == 3  # system + memory + user
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "system"
        assert "相关记忆" in messages[1]["content"]
        assert messages[2]["role"] == "user"

    @pytest.mark.asyncio
    async def test_build_messages_with_history(self):
        """验证 _build_messages 包含 history。"""
        ctx = Context(user_input="hello", system_prompt="You are a bot.")
        ctx.history = [{"role": "user", "content": "工具结果: ok"}]

        messages = ReActExecutor._build_messages(ctx)
        assert len(messages) == 3  # system + user + history
        assert messages[2] == ctx.history[0]


# ── Test NewsRadar MCP Server ────────────────────────────────────


@pytest.mark.asyncio
async def test_news_server_via_stdio():
    """通过子进程启动 news_server，验证 JSON-RPC 通信。

    search_news / get_hot_topics 需要真实数据库，因此只验证
    无需 DB 的 analyze_sentiment 工具。
    """
    client = MCPClient(name="news-test")
    await client.connect_stdio("python", "-m", "agent.mcp.news_server")

    # 验证工具列表
    assert client.has_tool("search_news")
    assert client.has_tool("get_hot_topics")
    assert client.has_tool("get_news_detail")
    assert client.has_tool("analyze_sentiment")
    assert client.has_tool("get_source_stats")
    assert len(client.get_tools()) == 5

    # 验证无需 DB 的工具
    result = await client.call_tool("analyze_sentiment", {"text": "好优秀成功"})
    assert "score" in result

    # 验证未知工具
    with pytest.raises(RuntimeError, match="Unknown tool"):
        await client.call_tool("nonexistent", {})

    await client.close()


# ── Test Context tool_calls field ────────────────────────────────


class TestContextToolFields:
    def test_context_has_tool_fields(self):
        ctx = Context(user_input="hi")
        assert ctx.tool_calls == []
        assert ctx.tool_results == []
        assert ctx.history == []
        assert ctx.knowledge_context is None

    def test_context_can_store_tool_calls(self):
        ctx = Context(user_input="hi")
        ctx.tool_calls = [{"id": "call_1", "function": {"name": "test"}}]
        ctx.tool_results = ["result"]
        ctx.history = [{"role": "user", "content": "工具返回: ok"}]
        assert len(ctx.tool_calls) == 1
        assert len(ctx.tool_results) == 1
        assert len(ctx.history) == 1


# ── Test @tool decorator ──────────────────────────────────────────


class TestToolDecorator:
    """测试 @tool 装饰器及其自动 schema 生成。"""

    def test_decorator_no_args(self):
        @tool
        def hello(name: str) -> str:
            """Say hello to someone.

            Args:
                name: The person to greet
            """
            return f"Hello {name}"

        assert isinstance(hello, FunctionTool)
        assert hello.get_def().name == "hello"
        assert "Say hello" in hello.get_def().description

        schema = hello.get_def().input_schema
        assert schema["type"] == "object"
        assert "name" in schema["properties"]
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["required"] == ["name"]

    def test_decorator_with_params(self):
        @tool(name="greet", description="A custom greeting tool")
        def hello(name: str) -> str:
            return f"Hello {name}"

        assert hello.get_def().name == "greet"
        assert hello.get_def().description == "A custom greeting tool"

    def test_auto_schema_default_value(self):
        @tool
        def greet(greeting: str = "Hello", name: str = "World") -> str:
            return f"{greeting} {name}"

        schema = greet.get_def().input_schema
        # 有默认值的不在 required 里
        assert "required" not in schema or schema["required"] == []
        assert schema["properties"]["greeting"]["default"] == "Hello"
        assert schema["properties"]["name"]["default"] == "World"

    def test_auto_schema_list_type(self):
        @tool
        def repeat(items: list[str], times: int = 1) -> list[str]:
            """Repeat items.

            Args:
                items: Items to repeat
                times: How many times
            """
            return items * times

        schema = repeat.get_def().input_schema
        assert schema["properties"]["items"]["type"] == "array"
        assert schema["properties"]["items"]["items"]["type"] == "string"
        assert schema["required"] == ["items"]

    @pytest.mark.asyncio
    async def test_decorated_tool_execute(self):
        @tool
        async def add(a: int, b: int = 0) -> int:
            """Add two numbers.

            Args:
                a: First number
                b: Second number
            """
            return a + b

        result = await add.execute(a=3, b=4)
        assert result == "7"

    def test_docstring_params_extracted(self):
        @tool
        def search(query: str, limit: int = 10) -> str:
            """Search the database.

            Args:
                query: The search term to look for
                limit: Maximum number of results
            """
            return f"searching {query}"

        schema = search.get_def().input_schema
        assert "description" in schema["properties"]["query"]
        assert "search term" in schema["properties"]["query"]["description"].lower()
        assert "description" in schema["properties"]["limit"]
        assert "maximum" in schema["properties"]["limit"]["description"].lower()

    def test_callable_decorated_tool(self):
        """@tool 装饰后的函数仍可被直接调用。"""

        @tool
        def add(a: int, b: int) -> int:
            return a + b

        assert add(3, 4) == 7  # __call__
        assert isinstance(add, FunctionTool)

    def test_tool_decorator_level_default(self):
        """@tool 默认 level=1。"""

        @tool
        def my_tool(x: int) -> str:
            return str(x)

        assert my_tool.level == 1
        assert my_tool.get_def().level == 1

    def test_tool_decorator_level_explicit(self):
        """@tool(level=2) 正确设置 level。"""

        @tool(level=2)
        def my_tool(x: int) -> str:
            return str(x)

        assert my_tool.level == 2
        assert my_tool.get_def().level == 2

    def test_tool_decorator_level_high(self):
        """@tool(level=4) 正确设置 level。"""

        @tool(level=4)
        def dangerous_tool(x: str) -> str:
            return f"deleted {x}"

        assert dangerous_tool.level == 4
        assert dangerous_tool.get_def().level == 4

    def test_tool_decorator_level_not_in_schema(self):
        """level 不在 get_schemas() 输出中——LLM 不需要看到它。"""

        @tool(level=4)
        def my_tool(x: int) -> str:
            return str(x)

        td = my_tool.get_def()
        assert td.level == 4
        # 验证 schema 不含 level
        assert "level" not in td.input_schema


# ── Test Policy System ────────────────────────────────────────────


class TestPolicy:
    """测试工具治理策略系统。"""

    def test_check_policy_allow_low_level(self):
        """_check_policy: level < threshold → ALLOW。"""
        from agent.executor import Executor, PolicyDecision

        tool = FunctionTool(
            name="safe", description="Safe tool", fn=lambda: "ok", input_schema={}, level=1,
        )
        for mode in ("strict", "normal", "loose"):
            result = Executor._check_policy(tool, mode)
            assert result.decision == PolicyDecision.ALLOW, f"mode={mode}"

    def test_check_policy_strict_level2(self):
        """strict 模式: level >= 2 → APPROVAL_REQUIRED。"""
        from agent.executor import Executor, PolicyDecision

        tool = FunctionTool(
            name="news", description="News", fn=lambda: "news", input_schema={}, level=2,
        )
        result = Executor._check_policy(tool, "strict")
        assert result.decision == PolicyDecision.APPROVAL_REQUIRED

    def test_check_policy_normal_level3(self):
        """normal 模式: level >= 3 → APPROVAL_REQUIRED。"""
        from agent.executor import Executor, PolicyDecision

        tool = FunctionTool(
            name="write", description="Write", fn=lambda: "done", input_schema={}, level=3,
        )
        result = Executor._check_policy(tool, "normal")
        assert result.decision == PolicyDecision.APPROVAL_REQUIRED

    def test_check_policy_normal_level2_allowed(self):
        """normal 模式: level=2 放行。"""
        from agent.executor import Executor, PolicyDecision

        tool = FunctionTool(
            name="news", description="News", fn=lambda: "news", input_schema={}, level=2,
        )
        result = Executor._check_policy(tool, "normal")
        assert result.decision == PolicyDecision.ALLOW

    def test_check_policy_loose_only_level4(self):
        """loose 模式: 仅 level=4 需要审批。"""
        from agent.executor import Executor, PolicyDecision

        for lvl in (1, 2, 3):
            tool = FunctionTool(
                name=f"tool{lvl}", description="tool", fn=lambda: "", input_schema={}, level=lvl,
            )
            result = Executor._check_policy(tool, "loose")
            assert result.decision == PolicyDecision.ALLOW, f"level={lvl}"

        tool4 = FunctionTool(
            name="danger", description="dangerous", fn=lambda: "", input_schema={}, level=4,
        )
        result = Executor._check_policy(tool4, "loose")
        assert result.decision == PolicyDecision.APPROVAL_REQUIRED

    def test_check_policy_unknown_mode_defaults_normal(self):
        """未知 mode 默认 threshold=3（相当于 normal）。"""
        from agent.executor import Executor, PolicyDecision

        tool = FunctionTool(
            name="t", description="tool", fn=lambda: "", input_schema={}, level=3,
        )
        result = Executor._check_policy(tool, "unknown_mode")
        assert result.decision == PolicyDecision.APPROVAL_REQUIRED

    def test_default_agent_invalid_running_mode(self):
        """DefaultAgent 不接受无效 running_mode。"""
        with pytest.raises(ValueError, match="running_mode"):
            from agent import DefaultAgent
            DefaultAgent(config={}, running_mode="invalid")

    def test_default_agent_running_mode_property(self):
        """DefaultAgent 的 running_mode 属性可读写。"""
        from agent import DefaultAgent
        agent = DefaultAgent(config={}, running_mode="strict")
        assert agent.running_mode == "strict"

        agent.running_mode = "loose"
        assert agent.running_mode == "loose"

        with pytest.raises(ValueError, match="running_mode"):
            agent.running_mode = "invalid"

    def test_context_running_mode_default(self):
        """Context.running_mode 默认 normal。"""
        ctx = Context(user_input="hi")
        assert ctx.running_mode == "normal"

    def test_context_running_mode_custom(self):
        """Context 可设置 running_mode。"""
        ctx = Context(user_input="hi", running_mode="strict")
        assert ctx.running_mode == "strict"

    def test_function_tool_level_default(self):
        """FunctionTool 默认 level=1。"""
        tool = FunctionTool(
            name="t", description="Tool", fn=lambda: "", input_schema={},
        )
        assert tool.level == 1
        assert tool.get_def().level == 1

    def test_function_tool_level_explicit(self):
        """FunctionTool 可设置 level。"""
        tool = FunctionTool(
            name="t", description="Tool", fn=lambda: "", input_schema={}, level=3,
        )
        assert tool.level == 3
        assert tool.get_def().level == 3

    @pytest.mark.asyncio
    async def test_exec_tool_with_policy_allow(self):
        """ALLOW 时正常执行工具。"""
        from agent.executor import DirectExecutor

        registry = Registry()
        registry.add_tool(FunctionTool(
            name="add", description="Add", fn=lambda a, b: a + b,
            input_schema={"type": "object", "properties": {"a": {}, "b": {}}, "required": ["a", "b"]},
            level=1,
        ))

        executor = DirectExecutor()
        result = await executor._exec_tool_with_policy(registry, "add", {"a": 1, "b": 2}, "normal")
        assert result == "3"

    @pytest.mark.asyncio
    async def test_exec_tool_with_policy_reject(self):
        """REJECT 时返回拒绝消息。"""
        from agent.executor import DirectExecutor

        # strict 模式: level=2 需要审批，无 callback → 返回拒绝消息
        registry = Registry()
        registry.add_tool(FunctionTool(
            name="news", description="News", fn=lambda: "data", input_schema={}, level=2,
        ))

        executor = DirectExecutor()
        result = await executor._exec_tool_with_policy(registry, "news", {}, "strict")
        assert "[Policy]" in result
        assert "需要审批" in result

    @pytest.mark.asyncio
    async def test_exec_tool_with_policy_approval_approved(self):
        """审批回调返回 approved → 执行工具。"""
        from unittest.mock import AsyncMock
        from agent.executor import DirectExecutor

        registry = Registry()
        registry.add_tool(FunctionTool(
            name="write", description="Write", fn=lambda: "done", input_schema={}, level=3,
        ))

        callback = AsyncMock(return_value={"approved": True, "reason": "允许"})
        executor = DirectExecutor(approval_callback=callback)

        result = await executor._exec_tool_with_policy(registry, "write", {}, "normal")
        assert result == "done"
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exec_tool_with_policy_approval_rejected(self):
        """审批回调返回 rejected → 返回拒绝消息。"""
        from unittest.mock import AsyncMock
        from agent.executor import DirectExecutor

        registry = Registry()
        registry.add_tool(FunctionTool(
            name="write", description="Write", fn=lambda: "done", input_schema={}, level=3,
        ))

        callback = AsyncMock(return_value={"approved": False, "reason": "不安全"})
        executor = DirectExecutor(approval_callback=callback)

        result = await executor._exec_tool_with_policy(registry, "write", {}, "normal")
        assert "[Policy]" in result
        assert "拒绝" in result or "不安全" in result
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exec_tool_with_policy_tool_not_found(self):
        """工具不存在时返回明确消息。"""
        from agent.executor import DirectExecutor

        registry = Registry()

        executor = DirectExecutor()
        result = await executor._exec_tool_with_policy(registry, "unknown", {}, "normal")
        assert "[Policy]" in result
        assert "不存在" in result

    def test_registry_get_tool_level(self):
        """Registry.get_tool_level() 返回正确 level。"""
        registry = Registry()
        tool = FunctionTool(
            name="safe", description="Safe", fn=lambda: "", input_schema={}, level=1,
        )
        registry.add_tool(tool)
        assert registry.get_tool_level("safe") == 1

    def test_registry_get_tool_level_unknown_raises(self):
        """Registry.get_tool_level() 未知工具抛 KeyError。"""
        registry = Registry()
        with pytest.raises(KeyError):
            registry.get_tool_level("unknown")

    def test_builtin_tools_levels(self):
        """验证内置工具的 level 正确。"""
        from agent.tools.tools import (
            calc, get_current_time, get_current_weather,
            get_latest_news, get_random_number, roll_dice,
        )
        assert get_current_time.level == 1
        assert get_random_number.level == 1
        assert calc.level == 1
        assert roll_dice.level == 1
        assert get_current_weather.level == 1
        assert get_latest_news.level == 2


# ── Test built-in tools ───────────────────────────────────────────


class TestBuiltinTools:
    def test_get_current_time(self):
        from agent.tools.tools import get_current_time

        td = get_current_time.get_def()
        assert td.name == "get_current_time"
        assert td.input_schema["type"] == "object"

    def test_calculator_schema(self):
        from agent.tools.tools import calc

        td = calc.get_def()
        assert td.name == "calculator"
        assert td.description == "执行四则运算，支持加/减/乘/除"
        props = td.input_schema["properties"]
        assert props["a"]["type"] == "number"
        assert props["b"]["type"] == "number"
        assert props["op"]["type"] == "string"
        assert td.input_schema["required"] == ["a", "b", "op"]

    @pytest.mark.asyncio
    async def test_calculator_execute(self):
        from agent.tools.tools import calc

        result = await calc.execute(a=10, b=3, op="+")
        assert "10 + 3 = 13" in result

    def test_roll_dice_schema(self):
        from agent.tools.tools import roll_dice

        td = roll_dice.get_def()
        assert td.name == "roll_dice"
        # list[int] 的 schema
        assert td.input_schema["properties"]["count"]["type"] == "integer"
        assert td.input_schema["properties"]["sides"]["type"] == "integer"

    def test_setup_registry(self):
        from agent.tools.tools import setup_builtin_tools

        registry = setup_builtin_tools()
        tools = registry.list_tools()
        assert "get_current_time" in tools
        assert "get_random_number" in tools
        assert "calculator" in tools
        assert "roll_dice" in tools
        assert "get_current_weather" in tools
        assert "get_latest_news" in tools
        assert len(tools) == 6

    @pytest.mark.asyncio
    async def test_registry_execute(self):
        from agent.tools.tools import setup_builtin_tools

        registry = setup_builtin_tools()
        result = await registry.execute("calculator", {"a": 10, "b": 5, "op": "*"})
        assert "10 * 5 = 50" in result

    def test_schemas_format(self):
        from agent.tools.tools import setup_builtin_tools

        registry = setup_builtin_tools()
        schemas = registry.get_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert "calculator" in names
        assert "roll_dice" in names
        assert all(s["type"] == "function" for s in schemas)
        assert all("parameters" in s["function"] for s in schemas)
