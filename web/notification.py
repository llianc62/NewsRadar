# coding=utf-8
"""Notification module — ``NotificationState`` class.

Single responsibility: manage notifications and push SSE events.
Has no dependencies on any business module (news, agent, crawler).

Usage::

    from web.notification import NotificationState

    ns = NotificationState()
    ns.add_notification(scope="news", article_id=42, title="...")
    ns.push_sse_event({"type": "new", ...})
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any


class NotificationState:
    """Notification list + SSE broadcast state.

    All lock management is internal — callers never touch ``_lock`` or
    ``_notifications`` directly.
    """

    def __init__(self) -> None:
        self._notifications: list[dict] = []
        self._counter: int = 0
        self._lock = threading.Lock()
        self._sse_clients: set[asyncio.Queue] = set()
        self._sse_clients_lock = threading.Lock()
        self._sse_event_loop: asyncio.AbstractEventLoop | None = None

    # ── 公开 API ─────────────────────────────────────────────────────

    def add_notification(
        self,
        scope: str,
        article_id: int,
        title: str,
        status: str = "pending",
        error_message: str = "",
        category: str = "fetch",
        summary: str = "",
    ) -> dict:
        """Create a notification, append to list, push SSE ``new`` event."""
        with self._lock:
            self._counter += 1
            notif: dict = {
                "id": self._counter,
                "scope": scope,
                "category": category,
                "article_id": article_id,
                "title": title,
                "summary": summary,
                "status": status,
                "error_message": error_message,
                "is_read": False,
                "created_at": time.time(),
            }
            self._notifications.insert(0, notif)
            if len(self._notifications) > 50:
                self._notifications.pop()
        # Push outside the lock to keep lock scope tight
        self.push_sse_event({"type": "new", "notification": dict(notif)})
        return notif

    def push_sse_event(self, data: dict) -> None:
        """Push an SSE event to all connected clients. Thread-safe."""
        loop = self._sse_event_loop
        if loop is None:
            return

        def _put() -> None:
            with self._sse_clients_lock:
                clients = list(self._sse_clients)
            for q in clients:
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass

        try:
            running = asyncio.get_running_loop()
            if running is loop:
                _put()
            else:
                loop.call_soon_threadsafe(_put)
        except RuntimeError:
            loop.call_soon_threadsafe(_put)

    def register_client(self, queue: asyncio.Queue) -> None:
        """Register an SSE client queue."""
        with self._sse_clients_lock:
            self._sse_clients.add(queue)

    def unregister_client(self, queue: asyncio.Queue) -> None:
        """Unregister an SSE client queue."""
        with self._sse_clients_lock:
            self._sse_clients.discard(queue)

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the asyncio event loop for SSE cross-thread dispatch."""
        self._sse_event_loop = loop

    # ── 查询/修改（替代外部直接访问 _notifications + _lock）────────

    def get_notifications(
        self,
        scope: str | None = None,
        unread_only: bool = False,
    ) -> list[dict]:
        """Thread-safe read of notification list, optionally filtered."""
        with self._lock:
            result = [dict(n) for n in self._notifications]
        if scope:
            result = [n for n in result if n.get("scope") == scope]
        if unread_only:
            result = [n for n in result if not n.get("is_read")]
        return result

    def get_unread_count(self, scope: str | None = None) -> int:
        """Return the count of unread notifications, optionally filtered."""
        with self._lock:
            return sum(
                1 for n in self._notifications
                if not n.get("is_read")
                and (scope is None or n.get("scope") == scope)
            )

    def mark_read(self, notif_id: int) -> bool:
        """Mark a single notification as read.  Returns False if not found."""
        with self._lock:
            for n in self._notifications:
                if n["id"] == notif_id:
                    n["is_read"] = True
                    return True
        return False

    def mark_all_read(self) -> None:
        """Mark all notifications as read."""
        with self._lock:
            for n in self._notifications:
                n["is_read"] = True
