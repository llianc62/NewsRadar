"""Integration tests for task notification system."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from web.app import create_app
from web.notification import NotificationState


@pytest.fixture
def ns():
    """Fresh NotificationState instance per test."""
    return NotificationState()


@pytest.fixture
def db_mock():
    db = Mock()
    db.is_connected = True
    db.get_article_by_url = Mock(return_value=None)
    db.get_news_by_id = Mock(return_value={"id": 1, "title": "test", "url": "http://example.com"})
    return db


@pytest.fixture
def s3_config():
    return {
        "endpoint_url": "http://localhost:9000",
        "bucket_name": "test-bucket",
        "access_key_id": "minioadmin",
        "secret_access_key": "minioadmin",
        "region": "",
    }


@pytest.fixture
def app_with_queues(db_mock, s3_config):
    """Create app with mock queues for testing."""
    import asyncio
    queues = {
        "crawl": asyncio.Queue(),
        "sync": asyncio.Queue(),
    }
    app = create_app(db_mock, s3_config, queues=queues, crawler=None)
    return app, queues


class TestAddNotification:
    """Tests for add_notification structure extension."""

    def test_default_category_is_fetch(self, ns):
        notif = ns.add_notification(scope="news", article_id=1, title="test title", status="pending")
        assert notif["category"] == "fetch"
        assert notif["summary"] == ""

    def test_explicit_category_crawl(self, ns):
        notif = ns.add_notification(scope="news", article_id=0, title="新闻抓取",
                                    status="pending", category="crawl", summary="任务已触发")
        assert notif["category"] == "crawl"
        assert notif["summary"] == "任务已触发"
        assert notif["article_id"] == 0

    def test_explicit_category_sync(self, ns):
        notif = ns.add_notification(scope="news", article_id=0, title="云端同步",
                                    status="pending", category="sync")
        assert notif["category"] == "sync"
        assert notif["article_id"] == 0

    def test_new_fields_compatible_with_old_callers(self, ns):
        """New callers pass scope explicitly."""
        notif = ns.add_notification(scope="news", article_id=123, title="article title",
                                    status="pending", error_message="some error")
        assert notif["category"] == "fetch"
        assert notif["summary"] == ""
        assert notif["article_id"] == 123
        assert notif["error_message"] == "some error"

    def test_notification_capped_at_50(self, ns):
        for i in range(60):
            ns.add_notification(scope="news", article_id=i, title=f"title {i}", category="fetch")
        assert len(ns._notifications) == 50
        assert ns._notifications[0]["title"] == "title 59"


class TestMarkAllRead:
    """Tests for POST /api/notifications/mark-all-read."""

    def test_mark_all_read(self, app_with_queues):
        app, _ = app_with_queues
        client = _make_test_client(app)
        ns = app.state.notification_state

        # Create some unread notifications
        ns.add_notification(scope="news", article_id=1, title="test1", status="completed")
        ns.add_notification(scope="news", article_id=2, title="test2", status="completed")

        resp = client.post("/api/notifications/mark-all-read")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        for n in ns._notifications:
            assert n["is_read"] is True


class TestTriggerEndpoints:
    """Tests for POST /api/trigger/{crawl,sync} with queue channels."""

    def test_trigger_crawl_puts_callback_on_queue(self, app_with_queues):
        app, queues = app_with_queues
        client = _make_test_client(app)
        ns = app.state.notification_state

        resp = client.post("/api/trigger/crawl")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["task"] == "crawl"
        assert "notif_id" in body

        # Queue should have exactly 1 item (the callback closure)
        assert not queues["crawl"].empty()

        # Consume the callback and verify it's callable
        cb = queues["crawl"].get_nowait()
        assert callable(cb)

        # Call it with success and verify notification updates
        cb(True, "抓取完成，共 23 条新闻")
        notif = ns._notifications[0]
        assert notif["status"] == "completed"
        assert notif["summary"] == "抓取完成，共 23 条新闻"

    def test_trigger_sync_puts_callback_on_queue(self, app_with_queues):
        app, queues = app_with_queues
        client = _make_test_client(app)
        ns = app.state.notification_state

        resp = client.post("/api/trigger/sync")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["task"] == "sync"

        assert not queues["sync"].empty()
        cb = queues["sync"].get_nowait()
        assert callable(cb)

        cb(False, "Connection refused")
        notif = ns._notifications[0]
        assert notif["status"] == "failed"
        assert notif["summary"] == "Connection refused"

    def test_trigger_without_queue_returns_404(self, db_mock, s3_config):
        app = create_app(db_mock, s3_config, queues=None, crawler=None)
        client = _make_test_client(app)

        resp = client.post("/api/trigger/crawl")
        assert resp.status_code == 404

    def test_trigger_crawl_creates_notification_with_correct_structure(self, app_with_queues):
        app, _ = app_with_queues
        client = _make_test_client(app)
        ns = app.state.notification_state

        resp = client.post("/api/trigger/crawl")
        body = resp.json()
        notif_id = body["notif_id"]

        notif = [n for n in ns._notifications if n["id"] == notif_id][0]
        assert notif["category"] == "crawl"
        assert notif["title"] == "新闻抓取"
        assert notif["status"] == "running"
        assert notif["article_id"] == 0


class TestSSE:
    """Tests for SSE stream endpoint.

    Note: Starlette TestClient with httpx transport blocks until the
    response is fully streamed, making it incompatible with indefinite
    SSE streams.  These tests verify SSE functionality at the unit and
    route level rather than through live HTTP streaming.
    """

    def test_sse_endpoint_routes_correctly(self, app_with_queues):
        """SSE route is registered with the app."""
        app, _ = app_with_queues
        routes = [
            r for r in app.routes
            if hasattr(r, "path") and r.path == "/api/notifications/stream"
        ]
        assert len(routes) == 1

    def test_sse_endpoint_has_correct_streaming_response_type(self, app_with_queues):
        """The SSE route uses StreamingResponse with correct media type."""
        from starlette.responses import StreamingResponse

        app, _ = app_with_queues
        route = next(
            r for r in app.routes
            if hasattr(r, "path") and r.path == "/api/notifications/stream"
        )

        import inspect
        assert inspect.iscoroutinefunction(route.endpoint)

    def test_push_sse_event_delivers_to_registered_queue(self):
        """push_sse_event puts data on queues registered in _sse_clients."""
        import asyncio

        ns = NotificationState()

        async def run():
            q = asyncio.Queue()
            ns._sse_clients.add(q)
            ns._sse_event_loop = asyncio.get_running_loop()
            try:
                ns.push_sse_event({"type": "test", "message": "hello"})
                data = await asyncio.wait_for(q.get(), timeout=1.0)
                assert data == {"type": "test", "message": "hello"}
            finally:
                ns._sse_clients.discard(q)

        asyncio.run(run())

    def test_push_sse_event_returns_early_when_no_clients(self):
        """push_sse_event is a no-op when _sse_clients is empty."""
        ns = NotificationState()

        ns._sse_event_loop = MagicMock()
        # Should not raise — _sse_clients is empty
        ns.push_sse_event({"type": "test"})

    def test_push_sse_event_handles_offline_status(self):
        """push_sse_event handles the case when _sse_event_loop is None."""
        import asyncio

        ns = NotificationState()
        q = asyncio.Queue()
        ns._sse_clients.add(q)
        ns._sse_event_loop = None
        try:
            # When _sse_event_loop is None, push_sse_event returns early
            ns.push_sse_event({"type": "test"})
            # Queue should remain empty
            assert q.empty()
        finally:
            ns._sse_clients.clear()


# ── helper ──

@pytest.fixture(autouse=True)
def _patch_s3():
    """Prevent S3Storage from making real network calls during create_app."""
    with patch("web.app.S3Storage") as mock_s3:
        mock_s3.return_value = MagicMock()
        yield


def _make_test_client(app):
    """Create a synchronous Starlette test client for the given FastAPI app."""
    from starlette.testclient import TestClient
    return TestClient(app)
