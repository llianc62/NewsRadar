"""Tests for lifecycle methods in storage/postgres.py."""
import pytest
from unittest.mock import MagicMock, patch
from storage.postgres import PostgreSQL


@pytest.fixture
def pg_unconnected():
    """未连接状态的 PostgreSQL 实例。"""
    return PostgreSQL({
        "host": "localhost", "port": 5432, "database": "test",
        "user": "test", "password": "test",
    })


class TestConnectClose:
    def test_connect_creates_pool(self, pg_unconnected):
        with patch("storage.postgres.ThreadedConnectionPool") as mock_pool_cls:
            pg_unconnected.connect()
            assert pg_unconnected._pool is not None
            mock_pool_cls.assert_called_once()

    def test_connect_idempotent(self, pg_unconnected):
        with patch("storage.postgres.ThreadedConnectionPool") as mock_pool_cls:
            pg_unconnected.connect()
            pg_unconnected.connect()  # 第二次调用
            assert mock_pool_cls.call_count == 1

    def test_close(self, pg_unconnected):
        with patch("storage.postgres.ThreadedConnectionPool") as mock_pool_cls:
            pg_unconnected.connect()
            pg_unconnected.close()
            assert pg_unconnected._pool is None
            mock_pool_cls.return_value.closeall.assert_called_once()

    def test_close_when_not_connected(self, pg_unconnected):
        """未连接时 close 不抛异常。"""
        pg_unconnected.close()  # 不应抛异常
        assert pg_unconnected._pool is None

    def test_is_connected(self, pg_unconnected):
        assert pg_unconnected.is_connected is False
        with patch("storage.postgres.ThreadedConnectionPool"):
            pg_unconnected.connect()
        assert pg_unconnected.is_connected is True


class TestSchemaReady:
    def test_tables_exist(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = [True]

        assert pg_unconnected._schema_ready() is True

    def test_tables_not_exist(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = [False]

        assert pg_unconnected._schema_ready() is False


class TestInitSchema:
    def test_init_on_empty_database(self, pg_unconnected):
        """表不存在时执行 DDL。"""
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        # _schema_ready → False, 然后 _run_migrations 也需要 cursor
        mock_cursor.fetchone.side_effect = [
            [False],   # _schema_ready: tables don't exist
            [True],    # migration 001: idx_fulltext has content
            [True],    # migration 002: idx_fulltext_trgm exists
            [True],    # migration 003: failed_tasks table exists
            ["jsonb"], # migration 004: ranks column already JSONB
            [False],   # migration 005: crawled_at column does not exist (already dropped)
            [False],   # migration 006: idx_dedup_hotlist already on (url) only
        ]

        pg_unconnected.init_schema()
        # 验证 DDL 被执行
        executes = [c[0][0] for c in mock_cursor.execute.call_args_list if c[0]]
        ddl_calls = [s for s in executes if "CREATE TABLE" in str(s)]
        assert len(ddl_calls) >= 1

    def test_init_when_schema_exists(self, pg_unconnected):
        """表存在时跳过 DDL，仍执行 migration。"""
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [
            [True],   # _schema_ready: tables exist
            [True],   # migration 001: ok
            [True],   # migration 002: ok
            [True],   # migration 003: ok
            ["jsonb"], # migration 004: ranks column already JSONB
            [False],   # migration 005: crawled_at column does not exist (already dropped)
            [False],   # migration 006: idx_dedup_hotlist already on (url)
        ]

        pg_unconnected.init_schema()
        executes = [str(c[0][0]) for c in mock_cursor.execute.call_args_list if c[0]]
        ddl_calls = [s for s in executes if "CREATE TABLE" in s]
        assert len(ddl_calls) == 0  # DDL 被跳过


class TestRunMigrations:
    def _setup_pg_for_migration(self, pg_unconnected,
                                 idx_fulltext_with_content=True,
                                 idx_trgm_exists=True):
        """辅助：设置 mock pool/cursor，返回 mock_cursor。"""
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [
            [idx_fulltext_with_content],
            [idx_trgm_exists],
            [True],  # migration 003: failed_tasks table exists
            ["jsonb"],  # migration 004: ranks column already JSONB
            [False],   # migration 005: crawled_at column does not exist
            [False],   # migration 006: idx_dedup_hotlist already on (url)
        ]
        return mock_cursor

    def test_migration_001_skips_when_content_present(self, pg_unconnected):
        mock_cur = self._setup_pg_for_migration(pg_unconnected, True, True)
        pg_unconnected._run_migrations()
        executes = [str(c[0][0]) for c in mock_cur.execute.call_args_list]
        # 不应该有 DROP INDEX
        drop_calls = [s for s in executes if "DROP INDEX" in s]
        assert len(drop_calls) == 0

    def test_migration_001_rebuilds_when_content_missing(self, pg_unconnected):
        mock_cur = self._setup_pg_for_migration(pg_unconnected, False, True)
        pg_unconnected._run_migrations()
        executes = [str(c[0][0]) for c in mock_cur.execute.call_args_list]
        # 应该有 DROP INDEX + CREATE INDEX
        assert any("DROP INDEX" in s for s in executes)
        assert any("CREATE INDEX idx_fulltext" in s for s in executes)

    def test_migration_002_creates_pg_trgm_and_index(self, pg_unconnected):
        mock_cur = self._setup_pg_for_migration(pg_unconnected, True, False)
        pg_unconnected._run_migrations()
        executes = [str(c[0][0]) for c in mock_cur.execute.call_args_list]
        assert any("CREATE EXTENSION IF NOT EXISTS pg_trgm" in s for s in executes)
        assert any("idx_fulltext_trgm" in s for s in executes)

    def test_migration_002_skips_when_index_exists(self, pg_unconnected):
        mock_cur = self._setup_pg_for_migration(pg_unconnected, True, True)
        pg_unconnected._run_migrations()
        executes = [str(c[0][0]) for c in mock_cur.execute.call_args_list]
        # idx_fulltext_trgm 不应该被重复创建
        create_trgm_calls = [s for s in executes if "CREATE INDEX idx_fulltext_trgm" in s]
        assert len(create_trgm_calls) == 0

    def test_migrations_idempotent(self, pg_unconnected):
        """两次调用 _run_migrations 第二次无变化。"""
        mock_cur = self._setup_pg_for_migration(pg_unconnected, True, True)
        pg_unconnected._run_migrations()
        first_count = len(mock_cur.execute.call_args_list)

        # Reset mock for second call
        mock_cur2 = MagicMock()
        mock_cur2.fetchone.side_effect = [[True], [True], [True], ["jsonb"], [False], [False]]
        pg_unconnected._pool.getconn.return_value.cursor.return_value.__enter__.return_value = mock_cur2

        pg_unconnected._run_migrations()
        # 第二次调用只有 2 个 SELECT EXISTS（检查索引状态）
        # fetchone 被调用 2 次（每个 migration 检查一次），不应该有 DDL
        ddl_calls = [
            str(c[0][0]) for c in mock_cur2.execute.call_args_list
            if "CREATE INDEX" in str(c[0][0]) or "DROP INDEX" in str(c[0][0])
        ]
        assert len(ddl_calls) == 0


class TestGetConn:
    def test_raises_when_not_connected(self, pg_unconnected):
        with pytest.raises(RuntimeError, match="not connected"):
            with pg_unconnected.get_conn():
                pass

    def test_commits_on_success(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn

        with pg_unconnected.get_conn() as conn:
            assert conn is mock_conn
        mock_conn.commit.assert_called_once()

    def test_rollback_on_exception(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn

        with pytest.raises(ValueError):
            with pg_unconnected.get_conn():
                raise ValueError("test error")
        mock_conn.rollback.assert_called_once()

    def test_putconn_in_finally(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn

        try:
            with pg_unconnected.get_conn():
                raise ValueError("test")
        except ValueError:
            pass
        pg_unconnected._pool.putconn.assert_called_once_with(mock_conn)
