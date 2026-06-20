"""Shared fixtures for PostgreSQL unit tests."""
import pytest
from unittest.mock import MagicMock
from storage.postgres import PostgreSQL


@pytest.fixture
def mock_pool():
    """Mock ThreadedConnectionPool."""
    return MagicMock()


@pytest.fixture
def mock_conn():
    """Mock psycopg2 connection.  MagicMock 自动支持 context-manager 协议。"""
    return MagicMock()


@pytest.fixture
def mock_cursor():
    """Mock psycopg2 cursor — fetchone/fetchall 默认返回 None/[]."""
    cur = MagicMock()
    cur.fetchone.return_value = None
    cur.fetchall.return_value = []
    return cur


@pytest.fixture
def db(mock_pool, mock_conn, mock_cursor):
    """PostgreSQL 实例，pool 已 mock，get_conn() 会返回 mock_conn。

    用法::

        def test_xxx(db, mock_cursor):
            mock_cursor.fetchone.return_value = [42]
            result = db.get_news_count()
            sql, params = capture_sql(mock_cursor)
            assert "COUNT(*)" in sql
    """
    pg = PostgreSQL({
        "host": "localhost",
        "port": 5432,
        "database": "test",
        "user": "test",
        "password": "test",
    })
    pg._pool = mock_pool
    mock_pool.getconn.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return pg


def capture_sql(mock_cursor):
    """从 mock cursor 的最后一次 execute 调用中提取 (sql_template, params_tuple)。

    返回:
        (sql, params) — sql 是字符串，params 是参数元组。
        如果 cursor 从未被调用，返回 ("", ())。
    """
    if not mock_cursor.execute.call_args_list:
        return ("", ())
    call = mock_cursor.execute.call_args_list[-1]
    sql = call[0][0] if call[0] else ""
    params = call[0][1] if len(call[0]) > 1 else ()
    return (sql, params)
