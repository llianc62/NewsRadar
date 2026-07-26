from agent.data import (
    AgentConfig,
    AgentDefinition,
    AgentKnowledge,
    Context,
    MemoryBlock,
    Message,
    ToolResult,
)


def test_message_has_model_used_and_tool_result():
    msg = Message(role="assistant", content="hi", model_used="gpt-4o-mini-2024")
    assert msg.model_used == "gpt-4o-mini-2024"
    assert msg.tool_result is None


def test_message_no_stop_reason():
    msg = Message(role="assistant")
    assert not hasattr(msg, "stop_reason")


def test_message_tool_with_tool_result():
    tr = ToolResult(name="search", args={"q": "x"})
    msg = Message(role="tool", tool_call_id="c1", name="search", content="r", tool_result=tr)
    assert msg.tool_result is tr


def test_memory_block_defaults():
    mb = MemoryBlock(title="相关记忆", content="text")
    assert mb.source == ""
    assert mb.order == 0


def test_tool_result_defaults():
    tr = ToolResult(name="n", args={})
    assert tr.result == ""
    assert tr.success is True
    assert tr.timing_ms == 0
    assert tr.tool_call_id == ""


def test_agent_config_defaults():
    config = AgentConfig()
    assert config.brain is None
    assert config.executor is None
    assert config.memory is None
    assert config.tools is None
    assert config.knowledge is None
    assert config.system_prompt == ""


def test_agent_definition_defaults():
    definition = AgentDefinition(id="uuid-1", name="Tester")
    assert definition.id == "uuid-1"
    assert definition.name == "Tester"
    assert definition.description == ""
    assert definition.system_prompt == ""
    assert definition.tools == []
    assert definition.knowledge_id is None
    assert definition.metadata == {}
    assert definition.created_at == ""
    assert definition.updated_at == ""


def test_agent_definition_tools_isolation():
    a = AgentDefinition(id="a", name="A", tools=["search"])
    b = AgentDefinition(id="b", name="B")
    assert a.tools == ["search"]
    assert b.tools == []


def test_agent_knowledge_defaults():
    kb = AgentKnowledge(id="kb-1", name="Macro")
    assert kb.id == "kb-1"
    assert kb.name == "Macro"
    assert kb.description == ""
    assert kb.namespace == ""
    assert kb.created_at == ""
    assert kb.updated_at == ""


def test_context_input_zone():
    ctx = Context(user_input="hi", session_id="s1", system_prompt="sys")
    assert ctx.history_messages == []
    assert ctx.memories == []


def test_context_execution_zone():
    ctx = Context()
    assert ctx.messages == []
    assert ctx.step_count == 0


def test_context_final_output_from_messages():
    ctx = Context()
    ctx.messages = [
        Message(role="user", content="q"),
        Message(role="assistant", content="answer", model_used="m1"),
    ]
    assert ctx.final_output == "answer"


def test_context_final_output_empty():
    ctx = Context()
    assert ctx.final_output == ""


def test_context_removed_fields():
    ctx = Context()
    assert not hasattr(ctx, "memory_context")
    assert not hasattr(ctx, "knowledge_context")
    assert not hasattr(ctx, "assistant_output")
    assert not hasattr(ctx, "model_used")
    assert not hasattr(ctx, "tool_calls")
    assert not hasattr(ctx, "total_tokens")
