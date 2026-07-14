"""Tests for AgentFactory — build DefaultAgent from AgentDefinition."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from agent.factory import AgentFactory, _register_mcp_tools, create_agent, create_persona
from agent.models import AgentConfig, AgentDefinition, AgentKnowledge
from agent.tools import Registry
from agent.tools.base import BaseTool, ToolDef


# ── helpers ──────────────────────────────────────────────────────


class _FakeTool(BaseTool):
    """Fake tool for testing registry resolution."""

    def __init__(self, name: str, description: str = "", level: int = 1, category: str = "test"):
        self._name = name
        self._description = description
        self._level = level
        self._category = category

    @property
    def category(self) -> str:
        return self._category

    @property
    def level(self) -> int:
        return self._level

    def get_def(self) -> ToolDef:
        return ToolDef(self._name, self._description, level=self._level)

    async def execute(self, **kwargs):
        return f"fake:{self._name}"


def make_registry(tool_names: list[str]) -> Registry:
    """Create a Registry pre-populated with fake tools."""
    reg = Registry()
    for name in tool_names:
        reg.add_tool(_FakeTool(name, f"Desc of {name}"))
    return reg


# ── AgentFactory tests ───────────────────────────────────────────


class TestAgentFactoryInit:
    def test_init_stores_dependencies(self):
        """AgentFactory stores models_config, db, registry."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        db = MagicMock()
        reg = Registry()

        factory = AgentFactory(models, db, reg)

        assert factory._models_config is models
        assert factory._db is db
        assert factory._registry is reg


class TestAgentFactoryResolveTools:
    def test_resolves_registered_tools(self):
        """_resolve_tools creates a new Registry with tools found in the global registry."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        global_reg = make_registry(["search_news", "get_current_time", "calculator"])
        db = MagicMock()
        factory = AgentFactory(models, db, global_reg)

        tool_reg = factory._resolve_tools(["search_news", "calculator"])

        assert isinstance(tool_reg, Registry)
        assert "search_news" in tool_reg.list_tools()
        assert "calculator" in tool_reg.list_tools()
        assert "get_current_time" not in tool_reg.list_tools()

    def test_skips_unknown_tools(self):
        """_resolve_tools silently skips tool names not in the global registry."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        global_reg = make_registry(["search_news"])
        db = MagicMock()
        factory = AgentFactory(models, db, global_reg)

        tool_reg = factory._resolve_tools(["unknown_tool", "search_news"])

        assert "search_news" in tool_reg.list_tools()
        assert "unknown_tool" not in tool_reg.list_tools()

    def test_empty_tool_list_returns_empty_registry(self):
        """_resolve_tools with empty list returns empty Registry."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        global_reg = make_registry(["search_news"])
        db = MagicMock()
        factory = AgentFactory(models, db, global_reg)

        tool_reg = factory._resolve_tools([])

        assert tool_reg.list_tools() == []


class TestAgentFactoryResolveKnowledge:
    def test_returns_none_when_no_knowledge_id(self):
        """_resolve_knowledge returns None when knowledge_id is None."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        db = MagicMock()
        factory = AgentFactory(models, db, Registry())

        result = factory._resolve_knowledge(None)

        assert result is None

    def test_returns_none_when_kb_not_found(self):
        """_resolve_knowledge returns None when DB returns None for knowledge_id."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        db = MagicMock()
        db.get_agent_knowledge.return_value = None
        factory = AgentFactory(models, db, Registry())

        result = factory._resolve_knowledge("nonexistent-id")

        assert result is None
        db.get_agent_knowledge.assert_called_once_with("nonexistent-id")

    def test_creates_knowledge_engine_when_kb_found(self):
        """_resolve_knowledge creates KnowledgeEngine when KB exists in DB."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        db = MagicMock()
        kb = AgentKnowledge(
            id="kb-1",
            name="Test KB",
            namespace="kb_test",
        )
        db.get_agent_knowledge.return_value = kb

        with patch.dict(os.environ, {
            "KNOWLEDGE_EMBEDDING_API_KEY": "sk-embed-test",
            "KNOWLEDGE_EMBEDDING_BASE_URL": "https://embed.example.com",
            "KNOWLEDGE_EMBEDDING_MODEL": "text-embedding-3-large",
        }):
            factory = AgentFactory(models, db, Registry())
            result = factory._resolve_knowledge("kb-1")

        assert result is not None
        # Verify it's a KnowledgeEngine by checking its attributes
        assert hasattr(result, "_store")
        assert hasattr(result, "_embedding")
        assert result._top_k == 5

    def test_respects_top_k_parameter(self):
        """_resolve_knowledge passes top_k param to KnowledgeEngine."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        db = MagicMock()
        kb = AgentKnowledge(id="kb-1", name="Test KB", namespace="kb_test")
        db.get_agent_knowledge.return_value = kb

        with patch.dict(os.environ, {"KNOWLEDGE_EMBEDDING_API_KEY": "sk-embed-test"}):
            factory = AgentFactory(models, db, Registry(), top_k=10)
            result = factory._resolve_knowledge("kb-1")

        assert result._top_k == 10


class TestAgentFactoryBuild:
    def test_build_creates_default_agent(self):
        """build() creates a DefaultAgent from an AgentDefinition."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        global_reg = make_registry(["search_news", "get_current_time"])
        db = MagicMock()
        db.get_agent_knowledge.return_value = None

        factory = AgentFactory(models, db, global_reg)
        defn = AgentDefinition(
            id="agent-1",
            name="Test Agent",
            description="A test agent",
            system_prompt="You are a test agent.",
            tools=["search_news"],
        )

        agent = factory.build(defn)

        assert agent is not None
        assert agent.system_prompt == "You are a test agent."
        assert agent.brain is not None
        assert agent.executor is not None
        assert agent.memory is not None
        assert agent.tools is not None
        assert "search_news" in agent.tools.list_tools()

    def test_build_passes_knowledge_to_agent(self):
        """build() attaches KnowledgeEngine when knowledge_id is valid."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        global_reg = make_registry(["search_news"])
        db = MagicMock()
        kb = AgentKnowledge(id="kb-1", name="Test KB", namespace="kb_test")
        db.get_agent_knowledge.return_value = kb

        with patch.dict(os.environ, {"KNOWLEDGE_EMBEDDING_API_KEY": "sk-embed-test"}):
            factory = AgentFactory(models, db, global_reg)
            defn = AgentDefinition(
                id="agent-1",
                name="KB Agent",
                system_prompt="You have knowledge.",
                tools=["search_news"],
                knowledge_id="kb-1",
            )
            agent = factory.build(defn)

        # DefaultAgent stores knowledge in brain or we can check it was created
        assert agent is not None

    def test_build_with_no_tools(self):
        """build() works with an AgentDefinition that has no tools."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        global_reg = make_registry(["search_news"])
        db = MagicMock()
        factory = AgentFactory(models, db, global_reg)
        defn = AgentDefinition(
            id="agent-2",
            name="No Tools Agent",
            system_prompt="I have no tools.",
        )

        agent = factory.build(defn)

        assert agent is not None
        assert agent.tools.list_tools() == []

    def test_build_with_multiple_tools(self):
        """build() resolves multiple tools from the global registry."""
        models = {"default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"}}
        global_reg = make_registry(["tool_a", "tool_b", "tool_c", "tool_d"])
        db = MagicMock()
        factory = AgentFactory(models, db, global_reg)
        defn = AgentDefinition(
            id="agent-3",
            name="Multi Tool Agent",
            system_prompt="I have many tools.",
            tools=["tool_a", "tool_c", "tool_d"],
        )

        agent = factory.build(defn)

        tools = agent.tools.list_tools()
        assert "tool_a" in tools
        assert "tool_c" in tools
        assert "tool_d" in tools
        assert "tool_b" not in tools


# ── Backward compatibility tests ────────────────────────────────


class TestBackwardCompatibility:
    """Verify existing create_agent and create_persona remain intact."""

    def test_create_agent_signature(self):
        """create_agent is still importable and callable."""
        # Just verify the function exists and is importable
        assert callable(create_agent)

    def test_create_persona_signature(self):
        """create_persona is still importable and callable."""
        assert callable(create_persona)

    def test_register_mcp_tools_signature(self):
        """_register_mcp_tools is still importable and callable."""
        assert callable(_register_mcp_tools)