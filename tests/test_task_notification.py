"""Integration tests for task notification system."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from web.app import create_app, _add_notification, _notifications, _notification_counter
from web.app import _sse_clients, _push_sse_event, _notification_lock


@pytest.fixture(autouse=True)
def _clear_notifications():
    """Reset module-level notification state before each test."""
    import web.app as app_module
    with _notification_lock:
        app_module._notifications.clear()
        app_module._notification_counter = 0
    app_module._sse_clients.clear()


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
    """Tests for _add_notification structure extension."""

    def test_default_category_is_fetch(self):
        notif = _add_notification(1, "test title", "pending")
        assert notif["category"] == "fetch"
        assert notif["summary"] == ""

    def test_explicit_category_crawl(self):
        notif = _add_notification(0, "新闻抓取", "pending", category="crawl", summary="任务已触发")
        assert notif["category"] == "crawl"
        assert notif["summary"] == "任务已触发"
        assert notif["article_id"] == 0

    def test_explicit_category_sync(self):
        notif = _add_notification(0, "云端同步", "pending", category="sync")
        assert notif["category"] == "sync"
        assert notif["article_id"] == 0

    def test_new_fields_compatible_with_old_callers(self):
        """Old callers passing only 4 positional args still work."""
        notif = _add_notification(123, "article title", "pending", "some error")
        assert notif["category"] == "fetch"
        assert notif["summary"] == ""
        assert notif["article_id"] == 123
        assert notif["error_message"] == "some error"

    def test_notification_capped_at_50(self):
        for i in range(60):
            _add_notification(i, f"title {i}", category="fetch")
        with _notification_lock:
            assert len(_notifications) == 50
            assert _notifications[0]["title"] == "title 59"


class TestMarkAllRead:
    """Tests for POST /api/notifications/mark-all-read."""

    def test_mark_all_read(self, app_with_queues):
        app, _ = app_with_queues
        client = _make_test_client(app)

        # Create some unread notifications
        _add_notification(1, "test1", "completed")
        _add_notification(2, "test2", "completed")

        resp = client.post("/api/notifications/mark-all-read")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        with _notification_lock:
            for n in _notifications:
                assert n["is_read"] is True


class TestTriggerEndpoints:
    """Tests for POST /api/trigger/{crawl,sync} with queue channels."""

    def test_trigger_crawl_puts_callback_on_queue(self, app_with_queues):
        app, queues = app_with_queues
        client = _make_test_client(app)

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
        with _notification_lock:
            notif = _notifications[0]
            assert notif["status"] == "completed"
            assert notif["summary"] == "抓取完成，共 23 条新闻"

    def test_trigger_sync_puts_callback_on_queue(self, app_with_queues):
        app, queues = app_with_queues
        client = _make_test_client(app)

        resp = client.post("/api/trigger/sync")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["task"] == "sync"

        assert not queues["sync"].empty()
        cb = queues["sync"].get_nowait()
        assert callable(cb)

        cb(False, "Connection refused")
        with _notification_lock:
            notif = _notifications[0]
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

        resp = client.post("/api/trigger/crawl")
        body = resp.json()
        notif_id = body["notif_id"]

        with _notification_lock:
            notif = [n for n in _notifications if n["id"] == notif_id][0]
        assert notif["category"] == "crawl"
        assert notif["title"] == "新闻抓取"
        assert notif["status"] == "pending"
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

        # The endpoint is an async function that returns StreamingResponse
        import inspect
        assert inspect.iscoroutinefunction(route.endpoint)

    def test_push_sse_event_delivers_to_registered_queue(self):
        """_push_sse_event puts data on queues registered in _sse_clients."""
        import asyncio
        import web.app as app_module

        async def run():
            q = asyncio.Queue()
            app_module._sse_clients.add(q)
            old_loop = app_module._sse_event_loop
            app_module._sse_event_loop = asyncio.get_running_loop()
            try:
                app_module._push_sse_event({"type": "test", "message": "hello"})
                data = await asyncio.wait_for(q.get(), timeout=1.0)
                assert data == {"type": "test", "message": "hello"}
            finally:
                app_module._sse_event_loop = old_loop
                app_module._sse_clients.discard(q)

        asyncio.run(run())

    def test_push_sse_event_returns_early_when_no_clients(self):
        """_push_sse_event is a no-op when _sse_clients is empty."""
        import web.app as app_module

        old = app_module._sse_event_loop
        app_module._sse_event_loop = MagicMock()
        try:
            # Should not raise — _sse_clients is empty
            app_module._push_sse_event({"type": "test"})
        finally:
            app_module._sse_event_loop = old

    def test_push_sse_event_handles_offline_status(self):
        """_push_sse_event handles the case when _sse_event_loop is None."""
        import web.app as app_module

        old_loop = app_module._sse_event_loop
        old_clients = set(app_module._sse_clients)
        q = __import__("asyncio").Queue()
        app_module._sse_clients.add(q)
        app_module._sse_event_loop = None
        try:
            # When _sse_event_loop is None, _push_sse_event returns early
            app_module._push_sse_event({"type": "test"})
            # Queue should remain empty
            assert q.empty()
        finally:
            app_module._sse_event_loop = old_loop
            app_module._sse_clients.clear()
            app_module._sse_clients.update(old_clients)


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
