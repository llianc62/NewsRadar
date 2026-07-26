from agent.data import AgentConfig, AgentDefinition, AgentKnowledge


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
