"""Integration tests for agent REST API endpoints."""
import datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def config_with_llm():
    """LLM config fixture for agent routes registration."""
    return {
        "llm": {
            "deep": {
                "protocol": "anthropic",
                "model": "claude-sonnet-5",
                "api_key": "test-key",
                "base_url": "",
                "temperature": 0.7,
            },
            "quick": {
                "protocol": "openai",
                "model": "qwen-plus",
                "api_key": "test-key",
                "base_url": "",
                "temperature": 0.3,
            },
        },
        "agent": {
            "window_size": 10,
            "memory_enabled": True,
            "compression_strategy": "window",
            "default_model": "quick",
        },
    }


@pytest.fixture
def agent_client(db, config_with_llm, mock_cursor):
    """Create a TestClient with agent routes registered on the FastAPI app.

    Uses the mock PostgreSQL fixture from conftest_db.py so that
    agent CRUD methods (create_agent_session, get_agent_sessions, etc.)
    operate on MagicMock objects instead of a real database.
    """
    from web.app import create_app, register_agent_routes

    s3_config = {}
    app = create_app(db, s3_config)
    register_agent_routes(app, config_with_llm, db)

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