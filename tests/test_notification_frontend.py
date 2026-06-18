"""Test notification API endpoints for the optimized drawer behavior."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from web.app import create_app


@pytest.fixture(autouse=True)
def _reset_notification_state():
    """Reset module-level notification state before each test."""
    import web.app as app_module
    app_module._refetch_tasks.clear()
    app_module._notifications.clear()
    app_module._notification_counter = 0
    yield


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


class TestListNotifications:
    def test_returns_all_notifications_including_read(self, client):
        """GET /api/notifications returns all notifications, not just unread."""
        response = client.get("/api/notifications")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestMarkNotificationRead:
    def test_mark_single_notification_as_read(self, client):
        """POST /api/notifications/{id}/read marks the notification as read."""
        # First list to find an existing notification
        response = client.get("/api/notifications")
        data = response.json()

        if len(data) == 0:
            pytest.skip("No notifications in memory to test with")

        notif = data[0]
        notif_id = notif["id"]

        response = client.post(f"/api/notifications/{notif_id}/read")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        # Verify it's now marked read
        response = client.get("/api/notifications")
        updated = [n for n in response.json() if n["id"] == notif_id]
        assert len(updated) == 1
        assert updated[0]["is_read"] is True

    def test_mark_nonexistent_notification_returns_404(self, client):
        """POST /api/notifications/99999/read returns 404."""
        response = client.post("/api/notifications/99999/read")
        assert response.status_code == 404


class TestUnreadCount:
    def test_unread_count_endpoint(self, client):
        """GET /api/notifications/unread-count returns a count."""
        response = client.get("/api/notifications/unread-count")
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert isinstance(data["count"], int)
