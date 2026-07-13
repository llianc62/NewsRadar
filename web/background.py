# coding=utf-8
"""Generic background task executor — ``BackgroundTaskRunner``.

Usage::

    from web.background import BackgroundTaskRunner

    runner = BackgroundTaskRunner(max_workers=10)
    runner.submit("refetch-42", my_func, arg1, arg2)
    status = runner.get_status("refetch-42")  # {"status": "running", ...}
    runner.remove("refetch-42")
    runner.shutdown()
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class BackgroundTaskRunner:
    """Generic background task executor.

    Accepts any callable + arguments, runs in a ``ThreadPoolExecutor``.
    Provides dedup (by ``task_id``), status tracking, and lifecycle management.

    This class has **zero business dependencies** — it knows nothing about
    crawlers, databases, or notifications.
    """

    def __init__(self, max_workers: int = 10) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── 公开 API ─────────────────────────────────────────────────────

    def submit(self, task_id: str, func: Callable, *args: Any, **kwargs: Any) -> dict:
        """Submit a background task.

        *task_id* is used for dedup — if a task with the same ID is already
        pending or running, this is a no-op.

        Returns ``{"status": "submitted"|"duplicate", "task_id": ..., "error": ...}``.
        """
        with self._lock:
            existing = self._tasks.get(task_id)
            if existing and existing["status"] in ("pending", "running"):
                return {
                    "status": "duplicate",
                    "task_id": task_id,
                    "error": "任务已在执行中",
                }

            entry: dict = {
                "task_id": task_id,
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "error": None,
            }
            self._tasks[task_id] = entry

        self._executor.submit(self._run, task_id, func, args, kwargs)
        return {"status": "submitted", "task_id": task_id}

    def get_status(self, task_id: str) -> dict | None:
        """Return task status dict, or ``None`` if *task_id* is unknown."""
        with self._lock:
            entry = self._tasks.get(task_id)
            return dict(entry) if entry else None

    def remove(self, task_id: str) -> None:
        """Remove a task from tracking."""
        with self._lock:
            self._tasks.pop(task_id, None)

    def shutdown(self, wait: bool = False) -> None:
        """Shutdown the executor and clear all task state."""
        self._executor.shutdown(wait=wait)
        self._tasks.clear()

    # ── 内部 ─────────────────────────────────────────────────────────

    def _run(self, task_id: str, func: Callable, args: tuple, kwargs: dict) -> None:
        """Execute ``func(*args, **kwargs)`` and update task status."""
        with self._lock:
            entry = self._tasks.get(task_id)
            if entry is None:
                return  # already removed
            entry["status"] = "running"
            entry["started_at"] = time.time()

        try:
            func(*args, **kwargs)
        except Exception as e:
            with self._lock:
                entry = self._tasks.get(task_id)
                if entry:
                    entry["status"] = "failed"
                    entry["error"] = str(e)[:500]
                    entry["finished_at"] = time.time()
            return

        with self._lock:
            entry = self._tasks.get(task_id)
            if entry:
                entry["status"] = "completed"
                entry["finished_at"] = time.time()
