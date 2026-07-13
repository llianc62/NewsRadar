"""Test notification API endpoints for the optimized drawer behavior."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from web.app import create_app


@pytest.fixture
def client():
    """Create a TestClient with a mock database."""
    db = MagicMock()
    s3_config = {
        "endpoint_url": "http://localhost:9000",
        "bucket_name": "test",
        "access_key_id": "test",
        "secret_access_key": "test",
        "region": "us-east-1",
    }
    with patch("web.app.S3Storage") as mock_s3:
        mock_s3.return_value = MagicMock()
        app = create_app(db, s3_config, crawler=MagicMock())
    return TestClient(app)


def _seed_notification(ns, id_, **kw):
    """Insert a raw notification dict into the notification state's internal list."""
    entry = {
        "id": id_,
        "article_id": kw.get("article_id", 1),
        "title": kw.get("title", "Test"),
        "status": kw.get("status", "completed"),
        "error_message": kw.get("error_message", ""),
        "is_read": kw.get("is_read", False),
        "created_at": kw.get("created_at", 1700000000.0),
        "scope": kw.get("scope", "news"),
    }
    with ns._lock:
        ns._notifications.append(entry)
        ns._counter = max(ns._counter, id_)


class TestListNotifications:
    def test_returns_all_notifications_including_read(self, client):
        """GET /api/notifications returns all notifications, not just unread."""
        ns = client.app.state.notification_state
        _seed_notification(ns, 1, article_id=1, title="Unread", is_read=False)
        _seed_notification(ns, 2, article_id=2, title="Read", is_read=True)

        response = client.get("/api/notifications")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 2
        ids = {n["id"] for n in data}
        assert 1 in ids and 2 in ids


class TestMarkNotificationRead:
    def test_mark_single_notification_as_read(self, client):
        """POST /api/notifications/{id}/read marks the notification as read."""
        ns = client.app.state.notification_state
        _seed_notification(ns, 1, article_id=1, title="Test Article", is_read=False)

        response = client.post("/api/notifications/1/read")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        notif = next((n for n in ns._notifications if n["id"] == 1), None)
        assert notif is not None
        assert notif["is_read"] is True

    def test_mark_nonexistent_notification_returns_404(self, client):
        """POST /api/notifications/99999/read returns 404."""
        response = client.post("/api/notifications/99999/read")
        assert response.status_code == 404


class TestUnreadCount:
    def test_unread_count_endpoint(self, client):
        """GET /api/notifications/unread-count returns exact unread count."""
        ns = client.app.state.notification_state
        _seed_notification(ns, 1, article_id=1, title="Unread", is_read=False)
        _seed_notification(ns, 2, article_id=2, title="Read", is_read=True)

        response = client.get("/api/notifications/unread-count")
        assert response.status_code == 200
        assert response.json() == {"count": 1}
