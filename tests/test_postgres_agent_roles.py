"""单元测试 - storage/postgres.py 的 AgentDefinition 和 AgentKnowledge CRUD。

遵循 test_agent_db.py 和 test_postgres_knowledge.py 的 mock cursor 模式：
断言生成的 SQL 和参数，而非返回值。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent.models import AgentDefinition, AgentKnowledge
from storage.postgres import PostgreSQL
from tests.conftest_db import capture_sql


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def pg_agent():
    """PostgreSQL instance with pool mocked, ready for agent CRUD tests."""
    pg = PostgreSQL({
        "host": "localhost", "port": 5432, "database": "test",
        "user": "test", "password": "test",
    })
    pg._pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    pg._pool.getconn.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_cursor.fetchone.return_value = None
    mock_cursor.fetchall.return_value = []
    return pg


@pytest.fixture
def sample_definition():
    """A sample AgentDefinition for use in tests."""
    return AgentDefinition(
        id="test-id-001",
        name="巴菲特",
        description="价值投资大师",
        system_prompt="你是一位价值投资大师，遵循格雷厄姆和巴菲特的原则。",
        tools=["search_news", "get_hot_topics"],
        knowledge_id="kb-001",
        metadata={"style": "value", "era": "modern"},
    )


@pytest.fixture
def sample_knowledge():
    """A sample AgentKnowledge for use in tests."""
    return AgentKnowledge(
        id="kb-001",
        name="巴菲特投资哲学",
        description="巴菲特历年股东信和演讲",
        namespace="kb_kb-001",
    )


# ── AgentDefinition CRUD ──────────────────────────────────────────────

class TestCreateAgentDefinition:
    def test_inserts_and_returns_id(self, pg_agent, sample_definition):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        pg_agent.create_agent_definition(sample_definition)
        sql, params = capture_sql(mock_cursor)
        assert "INSERT INTO agent_definitions" in sql
        # params: (id, name, description, system_prompt, tools_json, knowledge_id, metadata_json)
        assert params[0] == "test-id-001"
        assert params[1] == "巴菲特"
        assert params[2] == "价值投资大师"
        assert params[3] == "你是一位价值投资大师，遵循格雷厄姆和巴菲特的原则。"
        assert json.loads(params[4]) == ["search_news", "get_hot_topics"]
        assert params[5] == "kb-001"
        assert json.loads(params[6]) == {"style": "value", "era": "modern"}

    def test_generates_id_when_none(self, pg_agent):
        """id 为空时自动生成 UUID。"""
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        defn = AgentDefinition(
            id="",
            name="测试",
            system_prompt="prompt",
        )
        pg_agent.create_agent_definition(defn)
        sql, params = capture_sql(mock_cursor)
        # id 应该被自动生成为 36 字符的 UUID
        assert len(params[0]) == 36
        assert params[0] != ""

    def test_empty_tools_and_metadata(self, pg_agent):
        """默认的 tools=[] 和 metadata={} 应正确序列化。"""
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        defn = AgentDefinition(
            id="test-id",
            name="测试",
            system_prompt="prompt",
        )
        pg_agent.create_agent_definition(defn)
        sql, params = capture_sql(mock_cursor)
        assert json.loads(params[4]) == []
        assert json.loads(params[6]) == {}

    def test_null_knowledge_id(self, pg_agent):
        """knowledge_id=None 时传入 None。"""
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        defn = AgentDefinition(
            id="test-id",
            name="测试",
            system_prompt="prompt",
            knowledge_id=None,
        )
        pg_agent.create_agent_definition(defn)
        sql, params = capture_sql(mock_cursor)
        assert params[5] is None


class TestGetAgentDefinition:
    def test_returns_none_when_not_found(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = None
        result = pg_agent.get_agent_definition("nonexistent")
        assert result is None

    def test_returns_definition_when_found(self, pg_agent, sample_definition):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = {
            "id": "test-id-001",
            "name": "巴菲特",
            "description": "价值投资大师",
            "system_prompt": "你是一位价值投资大师",
            "tools": '["search_news","get_hot_topics"]',
            "knowledge_id": "kb-001",
            "metadata": '{"style":"value"}',
            "created_at": "2026-07-01T00:00:00+00:00",
            "updated_at": "2026-07-01T00:00:00+00:00",
        }
        result = pg_agent.get_agent_definition("test-id-001")
        sql, params = capture_sql(mock_cursor)
        assert "SELECT * FROM agent_definitions" in sql
        assert params == ("test-id-001",)
        assert result is not None
        assert result.id == "test-id-001"
        assert result.name == "巴菲特"
        assert result.tools == ["search_news", "get_hot_topics"]
        assert result.metadata == {"style": "value"}

    def test_handles_jsonb_already_parsed(self, pg_agent):
        """psycopg2 with jsonb adapter may return already-parsed JSON (list/dict)."""
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = {
            "id": "test-id",
            "name": "测试",
            "description": "",
            "system_prompt": "prompt",
            "tools": ["search_news"],
            "knowledge_id": None,
            "metadata": {"style": "value"},
            "created_at": "2026-07-01T00:00:00+00:00",
            "updated_at": "2026-07-01T00:00:00+00:00",
        }
        result = pg_agent.get_agent_definition("test-id")
        assert result.tools == ["search_news"]
        assert result.metadata == {"style": "value"}

    def test_handles_jsonb_none(self, pg_agent):
        """tools 或 metadata 为 None 时返回空 list/dict。"""
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = {
            "id": "test-id",
            "name": "测试",
            "description": "",
            "system_prompt": "prompt",
            "tools": None,
            "knowledge_id": None,
            "metadata": None,
            "created_at": "2026-07-01T00:00:00+00:00",
            "updated_at": "2026-07-01T00:00:00+00:00",
        }
        result = pg_agent.get_agent_definition("test-id")
        assert result.tools == []
        assert result.metadata == {}

    def test_uses_realdict_cursor(self, pg_agent):
        """应使用 RealDictCursor 以返回字段名 dict。"""
        mock_conn = pg_agent._pool.getconn.return_value
        pg_agent.get_agent_definition("test-id")
        cursor_call = mock_conn.cursor.call_args
        assert "RealDictCursor" in str(cursor_call)


class TestListAgentDefinitions:
    def test_returns_empty_list_when_none(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchall.return_value = []
        result = pg_agent.list_agent_definitions()
        assert result == []

    def test_returns_all_definitions_ordered_by_created_at(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchall.return_value = [
            {
                "id": "id-1", "name": "角色一", "description": "", "system_prompt": "p1",
                "tools": "[]", "knowledge_id": None, "metadata": "{}",
                "created_at": "2026-07-02T00:00:00+00:00",
                "updated_at": "2026-07-02T00:00:00+00:00",
            },
            {
                "id": "id-2", "name": "角色二", "description": "", "system_prompt": "p2",
                "tools": "[]", "knowledge_id": None, "metadata": "{}",
                "created_at": "2026-07-01T00:00:00+00:00",
                "updated_at": "2026-07-01T00:00:00+00:00",
            },
        ]
        result = pg_agent.list_agent_definitions()
        sql, params = capture_sql(mock_cursor)
        assert "SELECT * FROM agent_definitions" in sql
        assert "ORDER BY created_at DESC" in sql
        assert len(result) == 2
        assert all(isinstance(d, AgentDefinition) for d in result)


class TestUpdateAgentDefinition:
    def test_updates_and_returns_true(self, pg_agent, sample_definition):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.rowcount = 1
        result = pg_agent.update_agent_definition(sample_definition)
        sql, params = capture_sql(mock_cursor)
        assert "UPDATE agent_definitions" in sql
        assert "SET" in sql
        assert "updated_at=NOW()" in sql
        assert result is True

    def test_returns_false_when_not_found(self, pg_agent, sample_definition):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.rowcount = 0
        result = pg_agent.update_agent_definition(sample_definition)
        assert result is False

    def test_updates_all_fields(self, pg_agent, sample_definition):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.rowcount = 1
        pg_agent.update_agent_definition(sample_definition)
        sql, params = capture_sql(mock_cursor)
        # params order: name, description, system_prompt, tools_json, knowledge_id, metadata_json, id
        assert params[0] == "巴菲特"
        assert params[1] == "价值投资大师"
        assert params[2] == "你是一位价值投资大师，遵循格雷厄姆和巴菲特的原则。"
        assert json.loads(params[3]) == ["search_news", "get_hot_topics"]
        assert params[4] == "kb-001"
        assert json.loads(params[5]) == {"style": "value", "era": "modern"}
        assert params[6] == "test-id-001"


class TestDeleteAgentDefinition:
    def test_deletes_and_returns_true(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.rowcount = 1
        result = pg_agent.delete_agent_definition("test-id")
        sql, params = capture_sql(mock_cursor)
        assert "DELETE FROM agent_definitions" in sql
        assert params == ("test-id",)
        assert result is True

    def test_returns_false_when_not_found(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.rowcount = 0
        result = pg_agent.delete_agent_definition("nonexistent")
        assert result is False


# ── AgentKnowledge CRUD ───────────────────────────────────────────────

class TestCreateAgentKnowledge:
    def test_inserts_and_returns_id(self, pg_agent, sample_knowledge):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        pg_agent.create_agent_knowledge(sample_knowledge)
        sql, params = capture_sql(mock_cursor)
        assert "INSERT INTO agent_knowledge" in sql
        assert params[0] == "kb-001"
        assert params[1] == "巴菲特投资哲学"
        assert params[2] == "巴菲特历年股东信和演讲"
        assert params[3] == "kb_kb-001"

    def test_generates_id_and_namespace_when_empty(self, pg_agent):
        """id 和 namespace 为空时自动生成。"""
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        kb = AgentKnowledge(
            id="",
            name="测试知识库",
            description="测试描述",
            namespace="",
        )
        pg_agent.create_agent_knowledge(kb)
        sql, params = capture_sql(mock_cursor)
        # id 应为 36 字符 UUID
        assert len(params[0]) == 36
        # namespace 应为 "kb_<uuid>"
        assert params[3].startswith("kb_")
        assert len(params[3]) == 39  # "kb_" + 36


class TestGetAgentKnowledge:
    def test_returns_none_when_not_found(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = None
        result = pg_agent.get_agent_knowledge("nonexistent")
        assert result is None

    def test_returns_knowledge_when_found(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = {
            "id": "kb-001",
            "name": "巴菲特投资哲学",
            "description": "巴菲特历年股东信",
            "namespace": "kb_kb-001",
            "created_at": "2026-07-01T00:00:00+00:00",
            "updated_at": "2026-07-01T00:00:00+00:00",
        }
        result = pg_agent.get_agent_knowledge("kb-001")
        sql, params = capture_sql(mock_cursor)
        assert "SELECT * FROM agent_knowledge" in sql
        assert params == ("kb-001",)
        assert result is not None
        assert result.id == "kb-001"
        assert result.name == "巴菲特投资哲学"
        assert isinstance(result, AgentKnowledge)


class TestListAgentKnowledge:
    def test_returns_empty_list(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchall.return_value = []
        result = pg_agent.list_agent_knowledge()
        assert result == []

    def test_includes_chunk_count(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchall.return_value = [
            {
                "id": "kb-001", "name": "知识库一", "description": "",
                "namespace": "ns1", "chunk_count": 42,
                "created_at": "2026-07-01T00:00:00+00:00",
                "updated_at": "2026-07-01T00:00:00+00:00",
            },
        ]
        result = pg_agent.list_agent_knowledge()
        sql, params = capture_sql(mock_cursor)
        assert "LEFT JOIN" in sql
        assert "knowledge_chunks" in sql
        assert "COALESCE(c.cnt, 0) AS chunk_count" in sql
        assert "ORDER BY k.created_at DESC" in sql
        assert len(result) == 1
        assert isinstance(result[0], AgentKnowledge)


class TestDeleteAgentKnowledge:
    def test_deletes_knowledge_and_chunks(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        # First call: get_agent_knowledge (returns the kb)
        # RealDictCursor is used for the get, but we need to mock it
        mock_cursor.fetchone.side_effect = [
            {
                "id": "kb-001", "name": "测试", "description": "",
                "namespace": "ns1", "created_at": "2026-07-01T00:00:00+00:00",
                "updated_at": "2026-07-01T00:00:00+00:00",
            },
            None,  # Second call (if any)
        ]
        result = pg_agent.delete_agent_knowledge("kb-001")
        assert result is True
        # Check that both DELETE statements were executed
        executes = [str(c[0][0]) for c in mock_cursor.execute.call_args_list if c[0]]
        delete_chunks = [s for s in executes if "DELETE FROM knowledge_chunks" in s]
        delete_kb = [s for s in executes if "DELETE FROM agent_knowledge" in s]
        assert len(delete_chunks) == 1
        assert len(delete_kb) == 1

    def test_returns_false_when_not_found(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = None
        result = pg_agent.delete_agent_knowledge("nonexistent")
        assert result is False


# ── get_agent_sessions_by_agent ───────────────────────────────────────

class TestGetAgentSessionsByAgent:
    def test_returns_empty_list(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchall.return_value = []
        result = pg_agent.get_agent_sessions_by_agent("agent-001")
        assert result == []

    def test_returns_sessions_with_correct_fields(self, pg_agent):
        import datetime
        now = datetime.datetime(2026, 7, 7, 12, 0, 0)
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchall.return_value = [
            (1, "Session 1", 5, now, now),
            (2, "Session 2", 3, now, now),
        ]
        result = pg_agent.get_agent_sessions_by_agent("agent-001")
        sql, params = capture_sql(mock_cursor)
        assert "WHERE agent_id = %s" in sql
        assert params[0] == "agent-001"
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[0]["title"] == "Session 1"
        assert result[0]["message_count"] == 5

    def test_respects_limit_offset(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        pg_agent.get_agent_sessions_by_agent("agent-001", limit=10, offset=5)
        sql, params = capture_sql(mock_cursor)
        assert params == ("agent-001", 10, 5)