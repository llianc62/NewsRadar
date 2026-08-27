"""Integration tests for agent REST API endpoints."""
import datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def config_with_llm():
    """LLM config fixture for agent routes registration."""
    return {
        "agent": {
            "window_size": 10,
            "memory_enabled": True,
            "compression_strategy": "window",
            "default_model": "quick",
            "models": {
                "deep": {
                    "protocol": "anthropic",
                    "model": "claude-sonnet-5",
                    "api_key": "test-key",
                    "base_url": "",
                },
                "quick": {
                    "protocol": "openai",
                    "model": "qwen-plus",
                    "api_key": "test-key",
                    "base_url": "",
                },
            },
        },
    }


@pytest.fixture
def agent_client(db, config_with_llm, mock_cursor):
    """Create a TestClient with agent routes registered on the FastAPI app.

    Uses the mock PostgreSQL fixture from conftest_db.py so that
    agent CRUD methods (create_agent_session, get_agent_sessions, etc.)
    operate on MagicMock objects instead of a real database.

    ``agent_config`` is stored on ``app.state`` so the WebSocket handler
    can resolve model config at runtime.
    """
    from web.app import create_app

    s3_config = {}
    app = create_app(db, s3_config, agent_config=config_with_llm)

    # Default: create_agent_session returns session id 1
    mock_cursor.fetchone.return_value = [1]
    # Default: delete_agent_session succeeds (rowcount > 0)
    mock_cursor.rowcount = 1

    return TestClient(app)


@pytest.mark.integration
class TestAgentSessions:
    """Integration tests for agent session REST endpoints."""

    def test_create_session(self, agent_client):
        """POST /api/agent/sessions should create a new session."""
        resp = agent_client.post("/api/agent/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["title"] == "新会话"

    def test_list_sessions(self, agent_client, mock_cursor):
        """GET /api/agent/sessions should return the session list."""
        now = datetime.datetime(2026, 7, 7, 12, 0, 0)
        mock_cursor.fetchall.return_value = [
            (1, "新会话", 0, now, now),
        ]
        resp = agent_client.get("/api/agent/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert len(data["sessions"]) >= 1

    def test_delete_session(self, agent_client):
        """DELETE /api/agent/sessions/{id} should delete an existing session."""
        resp = agent_client.post("/api/agent/sessions")
        session_id = resp.json()["id"]
        resp = agent_client.delete(f"/api/agent/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_nonexistent_session(self, agent_client, mock_cursor):
        """DELETE /api/agent/sessions/{id} should return 404 for nonexistent."""
        mock_cursor.rowcount = 0
        resp = agent_client.delete("/api/agent/sessions/99999")
        assert resp.status_code == 404

    def test_get_messages_empty(self, agent_client, mock_cursor):
        """GET /api/agent/sessions/{id}/messages returns empty for new session."""
        mock_cursor.fetchall.return_value = []
        resp = agent_client.post("/api/agent/sessions")
        session_id = resp.json()["id"]
        resp = agent_client.get(f"/api/agent/sessions/{session_id}/messages")
        assert resp.status_code == 200
        assert resp.json()["messages"] == []

    def test_agent_page_renders(self, agent_client):
        """GET /agent should render the chat page with 'AI 助手'."""
        resp = agent_client.get("/agent")
        assert resp.status_code == 200
        assert "AI 助手" in resp.text or "agent" in resp.text.lower()


# ── Agent CRUD + KB + Tools 测试 ──────────────────────────────────


@pytest.fixture
def agent_crud_client(db, config_with_llm, mock_cursor):
    """Create TestClient with tool_registry and agent_factory on app.state."""
    from agent.tools.tools import setup_builtin_tools
    from agent.factory import AgentFactory
    from web.app import create_app

    tool_registry = setup_builtin_tools()
    agent_factory = AgentFactory(
        config_with_llm["agent"]["models"],
        db,
        tool_registry,
    )
    mock_cursor.rowcount = 1
    app = create_app(
        db, {}, agent_config=config_with_llm,
        tool_registry=tool_registry,
        agent_factory=agent_factory,
    )
    return TestClient(app)


@pytest.mark.integration
class TestAgentCRUD:
    """Integration tests for /api/agents CRUD endpoints."""

    def test_create_agent(self, agent_crud_client, mock_cursor):
        """POST /api/agents should create a new agent definition."""
        resp = agent_crud_client.post("/api/agents", json={
            "name": "Test Agent",
            "description": "A test agent",
            "system_prompt": "You are a helpful assistant.",
            "tools": ["calculator", "get_current_time"],
            "knowledge_id": None,
            "metadata": {"key": "value"},
        })
        assert resp.status_code == 200
        assert "id" in resp.json()
        assert isinstance(resp.json()["id"], str)
        assert len(resp.json()["id"]) > 0

    def test_list_agents(self, agent_crud_client, mock_cursor):
        """GET /api/agents should return the agent list."""
        from agent.data import AgentDefinition
        mock_cursor.fetchall.return_value = [
            {"id": "a1", "name": "Agent 1", "description": "Desc 1",
             "system_prompt": "prompt1", "tools": '["calc"]',
             "knowledge_id": None, "metadata": '{}',
             "created_at": "2026-07-07T12:00:00+08:00",
             "updated_at": "2026-07-07T12:00:00+08:00"},
            {"id": "a2", "name": "Agent 2", "description": "Desc 2",
             "system_prompt": "prompt2", "tools": '["time"]',
             "knowledge_id": None, "metadata": '{}',
             "created_at": "2026-07-07T13:00:00+08:00",
             "updated_at": "2026-07-07T13:00:00+08:00"},
        ]
        resp = agent_crud_client.get("/api/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert len(data["agents"]) == 2
        assert data["agents"][0]["name"] == "Agent 1"

    def test_get_agent(self, agent_crud_client, mock_cursor):
        """GET /api/agents/{id} should return a single agent."""
        mock_cursor.fetchone.return_value = {
            "id": "a1", "name": "Test Agent", "description": "Desc",
            "system_prompt": "prompt", "tools": '["calc"]',
            "knowledge_id": None, "metadata": '{}',
            "created_at": "2026-07-07T12:00:00+08:00",
            "updated_at": "2026-07-07T12:00:00+08:00",
        }
        resp = agent_crud_client.get("/api/agents/a1")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Agent"

    def test_get_agent_not_found(self, agent_crud_client, mock_cursor):
        """GET /api/agents/{id} should return 404 for nonexistent."""
        mock_cursor.fetchone.return_value = None
        resp = agent_crud_client.get("/api/agents/nonexistent")
        assert resp.status_code == 404

    def test_update_agent(self, agent_crud_client, mock_cursor):
        """PUT /api/agents/{id} should update an agent."""
        mock_cursor.fetchone.side_effect = [
            {"id": "a1", "name": "Old Name", "description": "Old Desc",
             "system_prompt": "old prompt", "tools": '["calc"]',
             "knowledge_id": None, "metadata": '{}',
             "created_at": "2026-07-07T12:00:00+08:00",
             "updated_at": "2026-07-07T12:00:00+08:00"},
            None,  # subsequent calls return None
        ]
        mock_cursor.rowcount = 1
        resp = agent_crud_client.put("/api/agents/a1", json={
            "name": "New Name",
            "description": "New Desc",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_update_agent_not_found(self, agent_crud_client, mock_cursor):
        """PUT /api/agents/{id} should return 404 for nonexistent."""
        mock_cursor.fetchone.return_value = None
        resp = agent_crud_client.put("/api/agents/nonexistent", json={})
        assert resp.status_code == 404

    def test_delete_agent(self, agent_crud_client, mock_cursor):
        """DELETE /api/agents/{id} should delete an agent."""
        mock_cursor.rowcount = 1
        resp = agent_crud_client.delete("/api/agents/a1")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_agent_not_found(self, agent_crud_client, mock_cursor):
        """DELETE /api/agents/{id} should return 404 for nonexistent."""
        mock_cursor.rowcount = 0
        resp = agent_crud_client.delete("/api/agents/nonexistent")
        assert resp.status_code == 404


@pytest.mark.integration
class TestKnowledgeRoutes:
    """Integration tests for /api/agent/knowledge endpoints."""

    def test_create_knowledge(self, agent_crud_client, mock_cursor):
        """POST /api/agent/knowledge should create a knowledge base."""
        resp = agent_crud_client.post("/api/agent/knowledge", json={
            "name": "Test KB",
            "description": "A test knowledge base",
        })
        assert resp.status_code == 200
        assert "id" in resp.json()
        assert isinstance(resp.json()["id"], str)
        assert len(resp.json()["id"]) > 0

    def test_list_knowledge(self, agent_crud_client, mock_cursor):
        """GET /api/agent/knowledge should return KB list."""
        mock_cursor.fetchall.return_value = [
            {"id": "kb1", "name": "KB 1", "description": "Desc 1",
             "namespace": "ns_kb1", "chunk_count": 5,
             "created_at": "2026-07-07T12:00:00+08:00",
             "updated_at": "2026-07-07T12:00:00+08:00"},
            {"id": "kb2", "name": "KB 2", "description": "Desc 2",
             "namespace": "ns_kb2", "chunk_count": 3,
             "created_at": "2026-07-07T13:00:00+08:00",
             "updated_at": "2026-07-07T13:00:00+08:00"},
        ]
        resp = agent_crud_client.get("/api/agent/knowledge")
        assert resp.status_code == 200
        data = resp.json()
        assert "knowledge_bases" in data
        assert len(data["knowledge_bases"]) == 2
        assert data["knowledge_bases"][0]["name"] == "KB 1"
        assert data["knowledge_bases"][0]["chunk_count"] == 5

    def test_delete_knowledge(self, agent_crud_client, mock_cursor):
        """DELETE /api/agent/knowledge/{id} should delete a KB."""
        # delete_agent_knowledge internally calls get_agent_knowledge
        mock_cursor.fetchone.return_value = {
            "id": "kb1", "name": "KB 1", "description": "Desc",
            "namespace": "ns_kb1",
            "created_at": "2026-07-07T12:00:00+08:00",
            "updated_at": "2026-07-07T12:00:00+08:00",
        }
        mock_cursor.rowcount = 1
        resp = agent_crud_client.delete("/api/agent/knowledge/kb1")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_knowledge_not_found(self, agent_crud_client, mock_cursor):
        """DELETE /api/agent/knowledge/{id} should return 404 for nonexistent."""
        mock_cursor.rowcount = 0
        resp = agent_crud_client.delete("/api/agent/knowledge/nonexistent")
        assert resp.status_code == 404


@pytest.mark.integration
class TestToolRoutes:
    """Integration tests for /api/tools endpoint."""

    def test_list_tools(self, agent_crud_client):
        """GET /api/tools should return available tools."""
        resp = agent_crud_client.get("/api/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert "tools" in data
        # Should have at least the 6 builtin tools
        assert len(data["tools"]) >= 6
        tool_names = [t["name"] for t in data["tools"]]
        assert "calculator" in tool_names
        assert "get_current_time" in tool_names
        for t in data["tools"]:
            assert {"name", "description", "category"} <= set(t)

    def test_list_tools_empty_when_no_registry(self, agent_client):
        """GET /api/tools should return empty list when no registry."""
        resp = agent_client.get("/api/tools")
        assert resp.status_code == 200
        assert resp.json()["tools"] == []


@pytest.mark.integration
class TestAgentWebSocket:
    """Integration tests for /api/agent/ws WebSocket endpoint."""

    def test_websocket_agent_id_not_found(self, agent_crud_client, mock_cursor):
        """WS /api/agent/ws?agent_id=nonexistent should close with 4004."""
        mock_cursor.fetchone.return_value = None
        with agent_crud_client.websocket_connect(
            "/api/agent/ws?agent_id=nonexistent"
        ) as ws:
            # The server closes the connection with code 4004
            data = ws.receive()
            # Should receive close
            assert data["type"] == "websocket.close"
            assert data["code"] == 4004

    def test_websocket_agent_id_builds_agent(self, agent_crud_client, mock_cursor):
        """WS /api/agent/ws?agent_id=valid should build agent from definition."""
        mock_cursor.fetchone.return_value = {
            "id": "a1", "name": "Test Agent", "description": "A test agent",
            "system_prompt": "You are a helpful assistant.",
            "tools": '["calculator", "get_current_time"]',
            "knowledge_id": None, "metadata": '{}',
            "created_at": "2026-07-07T12:00:00+08:00",
            "updated_at": "2026-07-07T12:00:00+08:00",
        }
        with agent_crud_client.websocket_connect(
            "/api/agent/ws?agent_id=a1"
        ) as ws:
            # Connection should be accepted (no close)
            ws.send_json({"type": "chat", "message": "hello", "session_id": 1})
            # Should receive some response (token or error about model config)
            data = ws.receive_json()
            assert data["type"] in ("token", "error")