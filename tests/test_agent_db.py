"""Tests for agent CRUD methods in storage/postgres.py."""
import pytest
from unittest.mock import MagicMock, call
from storage.postgres import PostgreSQL
from tests.conftest_db import capture_sql


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


class TestCreateAgentSession:
    def test_create_returns_valid_id(self, pg_agent):
        pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = [7]
        session_id = pg_agent.create_agent_session()
        assert session_id == 7
        assert isinstance(session_id, int)

    def test_create_with_custom_title(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = [1]
        pg_agent.create_agent_session(title="Custom Title")
        sql, params = capture_sql(mock_cursor)
        assert "INSERT INTO agent_sessions" in sql
        assert params == ("Custom Title",)

    def test_create_default_title(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = [1]
        pg_agent.create_agent_session()
        sql, params = capture_sql(mock_cursor)
        assert params == ("新会话",)


class TestGetAgentSessions:
    def test_returns_list(self, pg_agent):
        result = pg_agent.get_agent_sessions()
        assert isinstance(result, list)
        assert result == []

    def test_returns_sessions_with_correct_fields(self, pg_agent):
        import datetime
        now = datetime.datetime(2026, 7, 7, 12, 0, 0)
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchall.return_value = [
            (1, "Session 1", 5, now, now),
            (2, "Session 2", 3, now, now),
        ]
        result = pg_agent.get_agent_sessions()
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[0]["title"] == "Session 1"
        assert result[0]["message_count"] == 5
        assert "created_at" in result[0]
        assert "updated_at" in result[0]

    def test_respects_limit_offset(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        pg_agent.get_agent_sessions(limit=10, offset=5)
        sql, params = capture_sql(mock_cursor)
        assert params == (10, 5)


class TestDeleteAgentSession:
    def test_delete_returns_true_when_found(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.rowcount = 1
        result = pg_agent.delete_agent_session(1)
        assert result is True

    def test_delete_returns_false_for_nonexistent(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.rowcount = 0
        result = pg_agent.delete_agent_session(999)
        assert result is False


class TestGetAgentMessages:
    def test_returns_empty_for_new_session(self, pg_agent):
        result = pg_agent.get_agent_messages(1)
        assert isinstance(result, list)
        assert result == []

    def test_returns_messages_with_correct_fields(self, pg_agent):
        import datetime
        now = datetime.datetime(2026, 7, 7, 12, 0, 0)
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchall.return_value = [
            (1, 1, "user", "Hello", now),
            (2, 1, "assistant", "Hi there!", now),
        ]
        result = pg_agent.get_agent_messages(1)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "Hello"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "Hi there!"

    def test_respects_limit(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        pg_agent.get_agent_messages(1, limit=10)
        sql, params = capture_sql(mock_cursor)
        assert params == (1, 10)


class TestSaveAgentMessage:
    def test_inserts_and_returns_id(self, pg_agent):
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = [42]
        msg_id = pg_agent.save_agent_message(1, "user", "Hello world")
        assert msg_id == 42

    def test_sets_title_on_first_user_message(self, pg_agent):
        """First user message should update the session title from default."""
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = [1]
        pg_agent.save_agent_message(1, "user", "What is the market trend today?")
        # Check that the title update SQL was executed
        executes = [str(c[0][0]) for c in mock_cursor.execute.call_args_list if c[0]]
        title_updates = [s for s in executes if "SET title = LEFT" in s]
        assert len(title_updates) == 1
        assert "title = '新会话'" in title_updates[0]

    def test_does_not_set_title_for_assistant_message(self, pg_agent):
        """Assistant messages should NOT trigger title update."""
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = [1]
        pg_agent.save_agent_message(1, "assistant", "The market trend is positive.")
        executes = [str(c[0][0]) for c in mock_cursor.execute.call_args_list if c[0]]
        title_updates = [s for s in executes if "SET title = LEFT" in s]
        assert len(title_updates) == 0

    def test_increments_message_count(self, pg_agent):
        """Message count should be incremented on every save."""
        mock_cursor = pg_agent._pool.getconn.return_value.cursor.return_value.__enter__.return_value
        mock_cursor.fetchone.return_value = [1]
        pg_agent.save_agent_message(1, "user", "Hello")
        executes = [str(c[0][0]) for c in mock_cursor.execute.call_args_list if c[0]]
        count_updates = [s for s in executes if "message_count = message_count + 1" in s]
        assert len(count_updates) == 1

    def test_raises_valueerror_for_invalid_role(self, pg_agent):
        with pytest.raises(ValueError, match="role must be 'user' or 'assistant'"):
            pg_agent.save_agent_message(1, "admin", "content")

    def test_raises_valueerror_for_empty_content(self, pg_agent):
        with pytest.raises(ValueError, match="content must not be empty"):
            pg_agent.save_agent_message(1, "user", "")

    def test_raises_valueerror_for_whitespace_only_content(self, pg_agent):
        with pytest.raises(ValueError, match="content must not be empty"):
            pg_agent.save_agent_message(1, "user", "   ")

    def test_raises_valueerror_for_negative_session_id(self, pg_agent):
        with pytest.raises(ValueError, match="session_id must be positive"):
            pg_agent.save_agent_message(-1, "user", "content")

    def test_raises_valueerror_for_zero_session_id(self, pg_agent):
        with pytest.raises(ValueError, match="session_id must be positive"):
            pg_agent.save_agent_message(0, "user", "content")