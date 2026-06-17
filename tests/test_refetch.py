"""Tests for news refetch API endpoints."""
import time
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
    db.update_article_content.return_value = True
    return db


@pytest.fixture
def mock_crawler():
    """Mock Crawler with session, parser, and timeout."""
    c = MagicMock()
    c.timeout = 30
    # Make session().get() take long enough for duplicate-dedup test
    mock_response = MagicMock()
    mock_response.text = "<html><body><p>新闻正文内容</p></body></html>"

    def delayed_get(*args, **kwargs):
        time.sleep(0.3)
        return mock_response

    mock_session = MagicMock()
    mock_session.get.side_effect = delayed_get
    c.session.return_value = mock_session

    mock_parser = MagicMock()
    mock_parser.parse.return_value = {"markdown": "新闻正文内容"}
    c.parser = mock_parser
    return c


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

    def test_notifications_unread_only_filter(self, client):
        """unread_only=true should only return unread notifications."""
        resp = client.post("/api/news/1/refetch")
        assert resp.status_code == 200
        resp = client.get("/api/notifications?unread_only=true")
        data = resp.json()
        assert len(data) >= 0
        for n in data:
            assert n.get("is_read") is False

    def test_mark_notification_read_success(self, client):
        """Should mark a notification as read."""
        client.post("/api/news/1/refetch")
        resp = client.post("/api/notifications/1/read")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_mark_notification_read_404(self, client):
        """Should return 404 for non-existent notification."""
        resp = client.post("/api/notifications/99999/read")
        assert resp.status_code == 404
        assert resp.json()["ok"] is False

    def test_refetch_when_crawler_not_ready(self, client):
        """Should return error when crawler is None."""
        from web.app import create_app
        from unittest.mock import patch
        mock_db2 = MagicMock()
        mock_db2.get_news_by_id.return_value = {
            "id": 1, "title": "Test", "url": "https://example.com",
            "source_name": "Test", "content": "",
        }
        s3_config = {
            "endpoint_url": "http://localhost:9000",
            "bucket_name": "test", "access_key_id": "test",
            "secret_access_key": "test", "region": "us-east-1",
        }
        with patch("web.app.S3Storage") as mock_s3:
            mock_s3.return_value = MagicMock()
            app2 = create_app(mock_db2, s3_config, crawler=None)
        client2 = TestClient(app2)
        resp = client2.post("/api/news/1/refetch")
        assert resp.json()["ok"] is False
        assert "未就绪" in resp.json().get("error", "")

    def test_unread_count_decrements_after_mark_read(self, client):
        """Unread count should decrement after marking a notification as read."""
        client.post("/api/news/1/refetch")
        resp = client.get("/api/notifications/unread-count")
        assert resp.json()["count"] == 1
        client.post("/api/notifications/1/read")
        resp = client.get("/api/notifications/unread-count")
        assert resp.json()["count"] == 0
