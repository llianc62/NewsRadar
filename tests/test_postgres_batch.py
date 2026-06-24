"""Tests for batch helpers in storage/postgres.py."""
import pytest
from unittest.mock import MagicMock, patch
from storage.postgres import PostgreSQL


def _make_test_item(**overrides):
    """Construct a minimal NewsItem."""
    from news.models import NewsItem
    defaults = dict(
        title="Test Title",
        source_id="src1",
        source_name="TestSource",
        source_type="hotlist",
        url="https://example.com/1",
        mobile_url="",
        guid="",
        rank=1,
        ranks=[[1, 20]],
        summary="summary",
        author="author",
        content="content",
        category="tech",
        tags=["AI"],
        published_at="2026-06-21T08:00:00+08:00",
    )
    defaults.update(overrides)
    return NewsItem(**defaults)


class TestBuildRow:
    """Tests for PostgreSQL._build_row static method."""

    def test_returns_20_element_tuple(self):
        row = PostgreSQL._build_row(
            _make_test_item(),
            source_id="src1",
            tier=2,
            priority=5,
            crawled_from="local",
        )
        assert len(row) == 20

    def test_field_positions(self):
        """Verify key field positions in the tuple."""
        item = _make_test_item(title="Position Test", category="tech", tags=["AI", "ML"])
        row = PostgreSQL._build_row(item, "src1", 1, 10, "local")
        assert row[0] == "Position Test"   # title
        assert row[1] == "src1"             # source_id
        assert row[2] == "TestSource"       # source_name
        assert row[3] == "hotlist"          # source_type
        assert row[4] == 1                  # tier
        assert row[5] == 10                 # priority
        assert row[6] == item.url           # url
        assert row[8] == 1                  # rank
        assert row[14] == "tech"            # category
        assert row[15] == ["AI", "ML"]      # tags
        assert row[16] == "local"           # crawled_from
        assert row[17] == '[[1, 20]]'       # ranks (jsonb → json.dumps)

    def test_none_category_becomes_none(self):
        row = PostgreSQL._build_row(
            _make_test_item(category=None),
            "src1", 4, 0, "local",
        )
        assert row[14] is None

    def test_empty_tags_becomes_empty_list(self):
        row = PostgreSQL._build_row(
            _make_test_item(tags=None),
            "src1", 4, 0, "local",
        )
        assert row[15] == []

    def test_none_published_at(self):
        row = PostgreSQL._build_row(
            _make_test_item(published_at=None),
            "src1", 4, 0, "local",
        )
        assert row[10] is None  # ts_pub


class TestExecuteBatch:
    """Tests for PostgreSQL._execute_batch."""

    @pytest.fixture
    def pg(self):
        pg = PostgreSQL({
            "host": "localhost", "port": 5432, "database": "test",
            "user": "test", "password": "test",
        })
        pg._pool = MagicMock()
        return pg

    def test_single_page_batch(self, pg):
        """Fewer items than page_size succeed in one call."""
        mock_cur = MagicMock()
        sql = "INSERT INTO t VALUES %s"
        items = [(1,), (2,), (3,)]

        with patch("psycopg2.extras.execute_values") as mock_exec:
            processed, skipped = pg._execute_batch(mock_cur, sql, items, page_size=100)
            assert processed == 3
            assert skipped == 0
            mock_exec.assert_called_once()

    def test_multiple_pages(self, pg):
        """More items than page_size are split into multiple calls."""
        mock_cur = MagicMock()
        sql = "INSERT INTO t VALUES %s"
        items = [(i,) for i in range(250)]  # 250 items with page_size=100 => 3 pages

        with patch.object(pg, "_execute_batch_retry", return_value=(100, 0)) as mock_retry:
            processed, skipped = pg._execute_batch(mock_cur, sql, items, page_size=100)
            assert processed == 300  # 3 calls * 100
            assert mock_retry.call_count == 3

    def test_empty_items(self, pg):
        """Empty items list returns zero counts."""
        mock_cur = MagicMock()
        processed, skipped = pg._execute_batch(mock_cur, "INSERT ...", [], page_size=100)
        assert processed == 0
        assert skipped == 0


class TestExecuteBatchRetry:
    """Tests for PostgreSQL._execute_batch_retry."""

    @pytest.fixture
    def pg(self):
        pg = PostgreSQL({
            "host": "localhost", "port": 5432, "database": "test",
            "user": "test", "password": "test",
        })
        pg._pool = MagicMock()
        return pg

    def test_successful_batch(self, pg):
        """Happy path: batch succeeds on first try."""
        import psycopg2.extras
        mock_cur = MagicMock()
        mock_conn = MagicMock()
        sql = "INSERT INTO t VALUES %s"

        with patch("psycopg2.extras.execute_values") as mock_exec:
            processed, skipped = pg._execute_batch_retry(
                mock_cur, mock_conn, sql, [(1,), (2,)], 100,
            )
            assert processed == 2
            assert skipped == 0

    def test_single_row_failure(self, pg):
        """page_size=1 failure returns (0, 1) without exception."""
        import psycopg2
        import psycopg2.extras
        mock_cur = MagicMock()
        mock_conn = MagicMock()

        with patch("psycopg2.extras.execute_values",
                   side_effect=psycopg2.Error("bad row")):
            processed, skipped = pg._execute_batch_retry(
                mock_cur, mock_conn, "INSERT INTO t VALUES %s", [(1,)], 1,
            )
            assert processed == 0
            assert skipped == 1

    def test_batch_failure_retries_with_smaller_pages(self, pg):
        """Large batch failure degrades to page_size=10 retries."""
        import psycopg2
        mock_cur = MagicMock()
        mock_conn = MagicMock()
        items = [(i,) for i in range(25)]

        with patch("psycopg2.extras.execute_values") as mock_exec:
            # First call fails, subsequent calls succeed
            mock_exec.side_effect = [
                psycopg2.Error("batch too large"),   # page_size=100 => fail
                None, None, None,                     # 3 sub-batches of 10 => succeed
            ]
            processed, skipped = pg._execute_batch_retry(
                mock_cur, mock_conn, "INSERT INTO t VALUES %s", items, 100,
            )
            assert processed + skipped == 25
            assert mock_exec.call_count == 4  # 1 fail + 3 retries
