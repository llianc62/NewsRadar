"""Tests for article deletion — POST-free DELETE endpoint + db.delete_news wiring.

The DELETE /api/news/{id} endpoint mirrors the refetch endpoint's validation
shape (get_news_by_id → 404) but is irreversible, so the contract is stricter:
a second existence check via delete_news guards the race where the article
vanishes between the GET and the DELETE.

These tests use a mock db (same pattern as test_refetch.py); the real
ON DELETE CASCADE behaviour is guaranteed by the schema DDL in
storage/postgres.sql, not by application code.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from web.app import create_app


@pytest.fixture
def mock_db():
    """Mock PostgreSQL with an existing article."""
    db = MagicMock()
    db.get_news_by_id.return_value = {
        "id": 1,
        "title": "测试新闻标题",
        "url": "https://example.com/news/1",
        "source_name": "测试来源",
        "content": "",
    }
    db.delete_news.return_value = True
    return db


@pytest.fixture(autouse=True)
def _reset_refetch_state():
    """Reset module-level refetch state before each test."""
    import web.app as app_module
    app_module._refetch_tasks.clear()
    app_module._notifications.clear()
    app_module._notification_counter = 0
    yield


@pytest.fixture
def client(mock_db):
    """Create test client with mock db."""
    s3_config = {
        "endpoint_url": "http://localhost:9000",
        "bucket_name": "test",
        "access_key_id": "test",
        "secret_access_key": "test",
        "region": "us-east-1",
    }
    with patch("web.app.S3Storage") as mock_s3:
        mock_s3.return_value = MagicMock()
        app = create_app(mock_db, s3_config, crawler=MagicMock())
    return TestClient(app)


class TestDeleteEndpoint:
    """Tests for DELETE /api/news/{id}."""

    def test_delete_returns_ok_for_valid_article(self, client, mock_db):
        """Should delete an existing article and return ok."""
        resp = client.delete("/api/news/1")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        mock_db.delete_news.assert_called_once_with(1)

    def test_delete_returns_404_for_missing_article(self, client, mock_db):
        """Should return 404 when get_news_by_id finds nothing."""
        mock_db.get_news_by_id.return_value = None
        resp = client.delete("/api/news/999")
        assert resp.status_code == 404
        data = resp.json()
        assert data["ok"] is False
        # delete_news must not be called when the article doesn't exist
        mock_db.delete_news.assert_not_called()

    def test_delete_returns_404_when_delete_affects_no_rows(self, client, mock_db):
        """Should return 404 if the article vanished between GET and DELETE."""
        # Article exists for the existence check, but delete_news reports 0 rows
        mock_db.delete_news.return_value = False
        resp = client.delete("/api/news/1")
        assert resp.status_code == 404
        assert resp.json()["ok"] is False

    def test_delete_clears_lingering_refetch_task(self, client, mock_db):
        """A pending refetch task for the deleted article should be dropped."""
        import web.app as app_module
        # Simulate a pending refetch task for this article
        app_module._refetch_tasks[1] = {
            "article_id": 1, "title": "测试",
            "status": "pending", "created_at": "2026-06-18T00:00:00",
        }
        assert 1 in app_module._refetch_tasks

        client.delete("/api/news/1")

        assert 1 not in app_module._refetch_tasks

    def test_delete_passes_correct_id_to_db(self, client, mock_db):
        """The article_id from the path must reach delete_news unchanged."""
        client.delete("/api/news/42")
        mock_db.delete_news.assert_called_once_with(42)
