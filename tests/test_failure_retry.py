# coding=utf-8
"""Tests for failure recording and retry mechanism."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests as _requests


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def pg_db():
    """PostgreSQL instance with mocked pool. Follows the same pattern
    as tests/test_postgres_lifecycle.py."""
    from storage.postgres import PostgreSQL

    db = PostgreSQL({
        "host": "localhost", "port": 5432, "database": "test",
        "user": "test", "password": "test",
    })
    db._pool = MagicMock()
    return db


@pytest.fixture
def mock_conn_cursor(pg_db):
    """Set up mock connection and cursor on the pg_db pool.

    Returns the mock cursor so callers can configure fetchone/fetchall.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    pg_db._pool.getconn.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_cursor


# ═══════════════════════════════════════════════════════════════════
# http_get_with_retry tests
# ═══════════════════════════════════════════════════════════════════


class TestHttpGetWithRetry:
    """Tests for utils.http_get_with_retry."""

    def test_retry_success_first_attempt(self):
        """Returns response on first successful attempt."""
        from utils import http_get_with_retry

        session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        session.get.return_value = mock_resp

        resp, error = http_get_with_retry(session, "http://example.com")
        assert resp is mock_resp
        assert error is None
        assert session.get.call_count == 1

    def test_retry_success_second_attempt(self):
        """Retries and succeeds on second attempt."""
        from utils import http_get_with_retry

        session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        import requests
        session.get.side_effect = [
            requests.ConnectionError("timeout"),
            mock_resp,
        ]

        resp, error = http_get_with_retry(session, "http://example.com")
        assert resp is mock_resp
        assert error is None
        assert session.get.call_count == 2

    def test_retry_exhausted(self):
        """Returns None and error message when all retries exhausted."""
        from utils import http_get_with_retry

        session = MagicMock()
        import requests
        session.get.side_effect = requests.ConnectionError("timeout")

        resp, error = http_get_with_retry(session, "http://example.com")
        assert resp is None
        assert error is not None
        assert "timeout" in error
        assert session.get.call_count == 3  # MAX_IMMEDIATE_RETRIES


# ═══════════════════════════════════════════════════════════════════
# PostgreSQL method tests
# ═══════════════════════════════════════════════════════════════════


class TestRecordFailure:
    """Tests for PostgreSQL.record_failure."""

    def test_record_failure_inserts(self, pg_db, mock_conn_cursor):
        """Normal failure recording returns a task id."""
        mock_conn_cursor.fetchone.return_value = [42]

        task_id = pg_db.record_failure(
            "content_fetch",
            {"url": "https://example.com/article/1"},
        )
        assert task_id == 42
        # Verify the INSERT was called with correct params
        call_args = mock_conn_cursor.execute.call_args[0]
        assert "INSERT INTO failed_tasks" in call_args[0]
        assert call_args[1][0] == "content_fetch"
        assert json.loads(call_args[1][1]) == {"url": "https://example.com/article/1"}

    def test_record_failure_dedup(self, pg_db, mock_conn_cursor):
        """Same URL + task_type does not create duplicate pending tasks."""
        mock_conn_cursor.fetchone.return_value = None  # ON CONFLICT DO NOTHING

        task_id = pg_db.record_failure(
            "content_fetch",
            {"url": "https://example.com/article/dedup"},
        )
        assert task_id is None  # dedup — returns None

    def test_record_failure_different_type_same_url(self, pg_db, mock_conn_cursor):
        """Same URL + different task_type creates separate records."""
        mock_conn_cursor.fetchone.side_effect = [[10], [20]]

        id1 = pg_db.record_failure("content_fetch", {"url": "https://example.com/img.jpg"})
        id2 = pg_db.record_failure("image_download", {"url": "https://example.com/img.jpg"})
        assert id1 == 10
        assert id2 == 20
        assert id1 != id2

    def test_record_failure_no_url(self, pg_db, mock_conn_cursor):
        """Context without 'url' key still works (no dedup constraint match)."""
        mock_conn_cursor.fetchone.return_value = [7]

        task_id = pg_db.record_failure(
            "content_fetch",
            {"title": "no url here"},
        )
        assert task_id == 7

    def test_record_failure_completed_then_new_pending(self, pg_db, mock_conn_cursor):
        """After marking pending as completed, a new pending can be inserted.

        With mocks, we simulate the DB state: first insert succeeds (id=1),
        mark_completed updates status, second insert with same URL+type
        also succeeds (id=3, no conflict since first is no longer pending).
        """
        mock_conn_cursor.fetchone.side_effect = [[1], [3]]

        id1 = pg_db.record_failure("content_fetch", {"url": "https://example.com/re-insert"})
        assert id1 == 1
        pg_db.mark_failure_completed(id1)
        id2 = pg_db.record_failure("content_fetch", {"url": "https://example.com/re-insert"})
        assert id2 == 3
        assert id2 != id1

    def test_record_failure_custom_max_retry(self, pg_db, mock_conn_cursor):
        """record_failure accepts custom max_retry parameter."""
        mock_conn_cursor.fetchone.return_value = [99]

        task_id = pg_db.record_failure(
            "content_fetch",
            {"url": "https://example.com/custom-retry"},
            max_retry=5,
        )
        assert task_id == 99
        call_args = mock_conn_cursor.execute.call_args[0]
        assert call_args[1][2] == 5  # max_retry passed as third param


class TestGetPendingFailures:
    """Tests for PostgreSQL.get_pending_failures."""

    def test_get_pending_returns_pending(self, pg_db, mock_conn_cursor):
        """Returns tasks with status='pending' and retry_times < max_retry."""
        mock_conn_cursor.fetchall.return_value = [
            (1, "content_fetch", {"url": "https://example.com/pending"},
             0, 3, None, "pending", "2026-06-21 10:00:00", "2026-06-21 10:00:00"),
        ]

        tasks = pg_db.get_pending_failures(task_type="content_fetch")
        assert len(tasks) >= 1
        assert all(t["status"] == "pending" for t in tasks)

    def test_get_pending_excludes_exceeded(self, pg_db, mock_conn_cursor):
        """Excludes tasks where retry_times >= max_retry."""
        # First call (get_pending_failures for record_failure context check):
        # No tasks initially
        # Then the task is recorded (INSERT returns id=5)
        # Then get_pending_failures returns the task
        # Then mark_failure_retried is called
        # Then get_pending_failures returns empty (task now has retry_times >= max_retry)

        # This test is simpler with direct mock: just verify the SQL query
        # filters retry_times < max_retry
        mock_conn_cursor.fetchall.return_value = []  # no pending tasks
        tasks = pg_db.get_pending_failures(task_type="content_fetch")
        # Verify the query includes retry_times < max_retry
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        assert "retry_times < max_retry" in sql

    def test_get_pending_all_types(self, pg_db, mock_conn_cursor):
        """Without task_type filter, returns all pending types."""
        mock_conn_cursor.fetchall.return_value = [
            (1, "content_fetch", {"url": "https://example.com/a"},
             0, 3, None, "pending", "2026-06-21 10:00:00", "2026-06-21 10:00:00"),
            (2, "image_download", {"url": "https://example.com/b.jpg"},
             0, 3, None, "pending", "2026-06-21 10:01:00", "2026-06-21 10:01:00"),
        ]

        tasks = pg_db.get_pending_failures()  # no filter
        assert len(tasks) == 2
        types = {t["task_type"] for t in tasks}
        assert "content_fetch" in types
        assert "image_download" in types

    def test_get_pending_result_structure(self, pg_db, mock_conn_cursor):
        """Each returned task dict has the expected keys."""
        mock_conn_cursor.fetchall.return_value = [
            (1, "content_fetch", {"url": "https://example.com/x"},
             0, 3, None, "pending", "2026-06-21 10:00:00", "2026-06-21 10:00:00"),
        ]

        tasks = pg_db.get_pending_failures()
        t = tasks[0]
        assert "id" in t
        assert "task_type" in t
        assert "context" in t
        assert "retry_times" in t
        assert "max_retry" in t
        assert "status" in t
        assert "created_at" in t
        assert "updated_at" in t

    def test_get_pending_empty(self, pg_db, mock_conn_cursor):
        """Returns empty list when no pending tasks."""
        mock_conn_cursor.fetchall.return_value = []
        tasks = pg_db.get_pending_failures()
        assert tasks == []


class TestArticleHasContent:
    """Tests for PostgreSQL.article_has_content."""

    def test_article_has_content_false(self, pg_db, mock_conn_cursor):
        """Returns False when no article with the URL exists."""
        mock_conn_cursor.fetchone.return_value = None
        assert pg_db.article_has_content("https://nonexistent.example.com") is False

    def test_article_has_content_true(self, pg_db, mock_conn_cursor):
        """Returns True when an article with non-empty content exists."""
        mock_conn_cursor.fetchone.return_value = [1]
        assert pg_db.article_has_content("https://example.com/existing") is True

    def test_article_has_content_checks_sql(self, pg_db, mock_conn_cursor):
        """Verify the SQL query checks content IS NOT NULL and content != ''."""
        mock_conn_cursor.fetchone.return_value = None
        pg_db.article_has_content("https://example.com/test")
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        assert "content IS NOT NULL" in sql
        assert "content != ''" in sql
        assert call_args[1][0] == "https://example.com/test"


class TestMarkFailureCompleted:
    """Tests for PostgreSQL.mark_failure_completed."""

    def test_mark_failure_completed_sets_status(self, pg_db, mock_conn_cursor):
        """Sets status to 'completed' for the given task id."""
        pg_db.mark_failure_completed(42)
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        assert "UPDATE failed_tasks" in sql
        assert "SET status = 'completed'" in sql
        assert "WHERE id = %s" in sql
        assert call_args[1][0] == 42

    def test_mark_failure_completed_only_pending(self, pg_db, mock_conn_cursor):
        """Only updates tasks with status='pending'."""
        pg_db.mark_failure_completed(7)
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        assert "status = 'pending'" in sql

    def test_mark_failure_completed_updates_timestamp(self, pg_db, mock_conn_cursor):
        """Sets updated_at to NOW()."""
        pg_db.mark_failure_completed(10)
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        assert "updated_at = NOW()" in sql


class TestMarkFailureRetried:
    """Tests for PostgreSQL.mark_failure_retried."""

    def test_mark_failure_retried_increments_retry_times(self, pg_db, mock_conn_cursor):
        """SQL updates retry_times = retry_times + 1."""
        pg_db.mark_failure_retried(42)
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        assert "retry_times = retry_times + 1" in sql
        assert "last_retry = NOW()" in sql

    def test_mark_failure_retried_case_pending(self, pg_db, mock_conn_cursor):
        """When retry_times + 1 < max_retry, status stays 'pending'."""
        pg_db.mark_failure_retried(42)
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        # The CASE expression sets status based on retry_times + 1 >= max_retry
        assert "CASE" in sql
        assert "WHEN retry_times + 1 >= max_retry" in sql

    def test_mark_failure_retried_only_pending(self, pg_db, mock_conn_cursor):
        """Only updates tasks with status='pending'."""
        pg_db.mark_failure_retried(7)
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        assert "status = 'pending'" in sql

    def test_mark_failure_retried_accepts_error(self, pg_db, mock_conn_cursor):
        """Accepts optional error parameter."""
        pg_db.mark_failure_retried(42, error="connection timeout")
        call_args = mock_conn_cursor.execute.call_args[0]
        assert call_args[1][0] == 42  # task_id is the first param


class TestFindArticlesByImageUrl:
    """Tests for PostgreSQL.find_articles_by_image_url."""

    def test_find_articles_by_image_url_found(self, pg_db, mock_conn_cursor):
        """Returns article IDs whose content contains the image URL."""
        mock_conn_cursor.fetchall.return_value = [[101], [202], [303]]
        result = pg_db.find_articles_by_image_url("https://example.com/img.jpg")
        assert result == [101, 202, 303]

    def test_find_articles_by_image_url_empty(self, pg_db, mock_conn_cursor):
        """Returns empty list when no articles contain the image URL."""
        mock_conn_cursor.fetchall.return_value = []
        result = pg_db.find_articles_by_image_url("https://example.com/nonexistent.jpg")
        assert result == []

    def test_find_articles_by_image_url_sql(self, pg_db, mock_conn_cursor):
        """Verifies the query uses position() for exact substring matching."""
        mock_conn_cursor.fetchall.return_value = []
        pg_db.find_articles_by_image_url("https://example.com/test.png")
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        assert "position(" in sql
        assert call_args[1][0] == "https://example.com/test.png"


class TestUpdateArticleImageUrl:
    """Tests for PostgreSQL.update_article_image_url."""

    def test_update_article_image_url_replaces(self, pg_db, mock_conn_cursor):
        """Replaces old URL with new path in article content."""
        pg_db.update_article_image_url(
            article_id=100,
            old_url="https://example.com/old.jpg",
            new_path="/media/news/abc.jpg",
        )
        call_args = mock_conn_cursor.execute.call_args[0]
        sql = call_args[0]
        assert "REPLACE(content" in sql
        assert "UPDATE news_articles" in sql
        assert call_args[1][0] == "https://example.com/old.jpg"
        assert call_args[1][1] == "/media/news/abc.jpg"
        assert call_args[1][2] == 100


# ═══════════════════════════════════════════════════════════════════
# Integration tests (require PostgreSQL + test infrastructure)
# ═══════════════════════════════════════════════════════════════════


def _httpbin_reachable() -> bool:
    """Check if test URLs are reachable for integration tests."""
    try:
        # Content fetch test URL
        resp = _requests.get(
            "https://example.com/",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return False
        # Image download test URL
        resp = _requests.get(
            "https://www.python.org/static/img/python-logo.png",
            timeout=10,
        )
        return resp.status_code == 200 and "image" in resp.headers.get("Content-Type", "")
    except Exception:
        return False


@pytest.fixture(scope="module")
def integration_pg_db():
    """PostgreSQL instance connected to the real test database.

    The schema is initialised (including the ``failed_tasks`` table)
    before tests, and the pool is closed afterwards.
    """
    from config.loader import load_config
    from storage.postgres import PostgreSQL

    config = load_config("config.yaml")
    db = PostgreSQL(config["postgresql"])
    db.connect()
    db.init_schema()
    yield db
    db.close()


@pytest.fixture(scope="module")
def integration_crawler(integration_pg_db):
    """Crawler wired to the real PostgreSQL database.

    Shares the same ``integration_pg_db`` instance that tests use for assertions.
    """
    from config.loader import load_config
    from news.crawler import Crawler

    config = load_config("config.yaml")
    c = Crawler(config, pg_db=integration_pg_db)
    yield c
    c.close()


@pytest.mark.integration
@pytest.mark.skipif(not _httpbin_reachable(), reason="External test URLs not reachable")
class TestRetryFailedTasksIntegration:
    """End-to-end tests for retry_failed_tasks flow."""

    @staticmethod
    def _cleanup_test_data(db, urls):
        """Remove test data (articles + failed_tasks) for the given URLs."""
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                for url in urls:
                    cur.execute(
                        "DELETE FROM failed_tasks WHERE context->>'url' = %s",
                        (url,),
                    )
                    cur.execute(
                        "DELETE FROM news_articles WHERE url = %s",
                        (url,),
                    )

    def test_retry_content_fetch_flow(self, integration_pg_db, integration_crawler):
        """Pending content_fetch task -> retry -> success -> article persisted."""
        test_url = "https://example.com/"
        context = {
            "url": test_url,
            "source_id": "test",
            "source_type": "rss",
            "source_name": "Test Source",
            "title": "Test Article",
            "rank": 0,
            "guid": "test-guid-001",
            "mobile_url": "",
            "published_at": "",
        }
        try:
            integration_pg_db.record_failure("content_fetch", context)
            result = integration_crawler.retry_failed_tasks(with_image=False)
            assert result["content_retried"] >= 1, (
                f"Expected content_retried >= 1, got {result}"
            )
            assert integration_pg_db.article_has_content(test_url), (
                "Article should have content after successful retry"
            )
        finally:
            self._cleanup_test_data(integration_pg_db, [test_url])

    def test_retry_image_download_flow(self, integration_pg_db, integration_crawler):
        """Pending image_download task -> retry -> success -> content updated."""
        article_url = "https://example.com/test-image-article"
        image_url = "https://www.python.org/static/img/python-logo.png"

        try:
            # Insert an article that references the image URL in its content
            with integration_pg_db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO news_articles
                           (source_id, source_name, source_type, tier, url,
                            title, content, crawled_from)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT DO NOTHING""",
                        (
                            "test", "Test Source", "rss", 4,
                            article_url, "Test Image Article",
                            f"Some content with image: {image_url}",
                            "local",
                        ),
                    )

            # Record image download failure
            integration_pg_db.record_failure(
                "image_download",
                {"url": image_url, "target_dir": "news/test/images"},
            )

            result = integration_crawler.retry_failed_tasks(with_image=True)
            assert result["image_retried"] >= 1, (
                f"Expected image_retried >= 1, got {result}"
            )

            # Verify the content was updated (image URL replaced with local path)
            with integration_pg_db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT content FROM news_articles WHERE url = %s",
                        (article_url,),
                    )
                    row = cur.fetchone()
            assert row is not None, "Article should exist"
            updated_content = row[0]
            assert "images/" in updated_content, (
                "Content should contain local image path after retry"
            )
            assert image_url not in updated_content, (
                "Original image URL should have been replaced"
            )
        finally:
            self._cleanup_test_data(integration_pg_db, [article_url, image_url])
