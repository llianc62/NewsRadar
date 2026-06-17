"""Tests for news refetch API endpoints."""
import pytest
from fastapi.testclient import TestClient
from web.app import create_app

# We'll use a mock crawler and mock db for testing
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_db():
    """Mock PostgreSQL with get_news_by_id returning a test article."""
    db = MagicMock()
    db.get_news_by_id.return_value = {
        "id": 1,
        "title": "测试新闻标题",
        "url": "https://example.com/news/1",
        "source_name": "测试来源",
        "content": "",
    }
    return db


@pytest.fixture
def mock_crawler():
    """Mock Crawler."""
    return MagicMock()


@pytest.fixture(autouse=True)
def _reset_refetch_state():
    """Reset module-level refetch state before each test."""
    import web.app as app_module
    app_module._refetch_tasks.clear()
    app_module._notifications.clear()
    app_module._notification_counter = 0
    yield


@pytest.fixture
def client(mock_db, mock_crawler):
    """Create test client with mock db and crawler."""
    s3_config = {
        "endpoint_url": "http://localhost:9000",
        "bucket_name": "test",
        "access_key_id": "test",
        "secret_access_key": "test",
        "region": "us-east-1",
    }
    with patch("web.app.S3Storage") as mock_s3:
        mock_s3.return_value = MagicMock()
        app = create_app(mock_db, s3_config, crawler=mock_crawler)
    return TestClient(app)


class TestRefetchEndpoint:
    """Tests for POST /api/news/{id}/refetch."""

    def test_refetch_returns_ok_for_valid_article(self, client, mock_db):
        """Should accept refetch request for an article with a URL."""
        resp = client.post("/api/news/1/refetch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "task" in data

    def test_refetch_rejects_duplicate(self, client):
        """Second refetch for same article should be rejected while running."""
        client.post("/api/news/1/refetch")
        resp = client.post("/api/news/1/refetch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "正在抓取中" in data.get("error", "")

    def test_refetch_rejects_article_without_url(self, client, mock_db):
        """Article without URL should be rejected."""
        mock_db.get_news_by_id.return_value = {
            "id": 2,
            "title": "无链接新闻",
            "url": "",
            "source_name": "测试",
            "content": "",
        }
        resp = client.post("/api/news/2/refetch")
        data = resp.json()
        assert data["ok"] is False

    def test_refetch_returns_404_for_missing_article(self, client, mock_db):
        """Missing article should return 404."""
        mock_db.get_news_by_id.return_value = None
        resp = client.post("/api/news/999/refetch")
        assert resp.status_code == 404


class TestNotificationsEndpoint:
    """Tests for GET /api/notifications."""

    def test_notifications_returns_list(self, client):
        """Should return a list (empty initially)."""
        resp = client.get("/api/notifications")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_unread_count_returns_zero_initially(self, client):
        """Should return count 0 with no notifications."""
        resp = client.get("/api/notifications/unread-count")
        assert resp.status_code == 200
        assert resp.json() == {"count": 0}
