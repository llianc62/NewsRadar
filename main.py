# coding=utf-8
"""
NewsRadar Daemon — local server entry point.

Startup sequence:
    1. Load configuration
    2. Connect to PostgreSQL, initialise schema (fast, sub-second)
    3. Start FastAPI web server (immediately, non-blocking)
    4. Launch background workers + timers (semaphore pattern)
    5. Manually trigger sync on startup

Usage::

    python main.py

The daemon runs until interrupted (SIGINT / SIGTERM).
"""

import sys
import signal
import asyncio

from typing import Any
from concurrent.futures import ThreadPoolExecutor

from config.loader import load_config
from storage.postgres import PostgreSQL
from web.app import create_app
from news.crawler import Crawler, OutputStyle


_SHUTDOWN_TIMEOUT = 10  # seconds to wait for async tasks to finish


class NewsRadarDaemon:
    """Orchestrates the local NewsRadar service.

    Uses a private ``ThreadPoolExecutor`` for all blocking I/O so that
    shutdown can call ``executor.shutdown(wait=False)`` and avoid
    blocking on long-running threads.

    Background tasks use an **asyncio.Queue channel pattern**::

        Timer ──put(None)──▶ asyncio.Queue ◀──get── Worker ──job──▶

    Each task type gets one ``asyncio.Queue``.  A *timer* puts
    ``None`` every N minutes (wake without notification); a manual
    *trigger* puts a callback closure (wake with notification).
    The *worker* takes items from the queue and executes the *job*.
    """

    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)
        self.db = PostgreSQL(self.config["postgresql"])
        self._shutdown_event = asyncio.Event()
        self._bg_tasks: list[asyncio.Task] = []
        self._uvicorn_server = None
        # Dedicated executor — we control its lifecycle
        self._executor = ThreadPoolExecutor(max_workers=4)

        # ── Channels (asyncio.Queue — Go-style signal + data carrier) ──
        self._crawl_queue: asyncio.Queue = asyncio.Queue()
        self._sync_queue: asyncio.Queue = asyncio.Queue()

        # ── Locks (prevent duplicate trigger while job is running) ──
        self._crawl_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()

    # ── Helpers ──────────────────────────────────────────────────

    async def _run_in_thread(self, func, *args):
        """Run a blocking *func* in our managed thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, func, *args)

    # ── Database init (fast, synchronous) ────────────────────────

    def _init_database(self) -> None:
        """Connect to PostgreSQL and initialise schema."""
        print("[Startup] Connecting to PostgreSQL...")
        self.db.connect()
        self.db.init_schema()
        print("[Startup] PostgreSQL connected and schema ready.")

    # ── Timer ────────────────────────────────────────────────────

    async def _timer(self, queue: asyncio.Queue, interval_min: int,
                     name: str, lock: asyncio.Lock) -> None:
        """Put None into *queue* every *interval_min* minutes to wake the Worker.

        Skips the put if *lock* is held, meaning the Worker is still running
        the previous job.
        """
        print(f"[Timer/{name}] every {interval_min} min")
        while not self._shutdown_event.is_set():
            await self._sleep_or_shutdown(interval_min * 60)
            if not self._shutdown_event.is_set() and not lock.locked():
                await queue.put(None)   # None = timer-triggered, no notification

    # ── Worker ───────────────────────────────────────────────────

    async def _worker(self, name: str, queue: asyncio.Queue, job,
                      lock: asyncio.Lock) -> None:
        """Wait for an item from *queue*, then execute *job* under *lock*.

        queue item ``None`` → timer-triggered, skip notification.
        queue item ``Callable`` → manual trigger, call it on completion.

        The lock is held for the entire job duration, preventing duplicate
        triggers from queueing while the job is still running.
        """
        print(f"[Worker/{name}] ready")
        while not self._shutdown_event.is_set():
            callback = await self._wait_queue(queue)
            if callback is None and self._shutdown_event.is_set():
                break
            async with lock:
                await self._try_run_job(name, job, callback)

    async def _wait_queue(self, queue: asyncio.Queue) -> Any:
        """Block until queue has data or shutdown is requested.

        Returns the queue item (None or callable), or None on shutdown.
        """
        get_task = asyncio.create_task(queue.get(), name="q_get")
        shut_task = asyncio.create_task(self._shutdown_event.wait(), name="q_shut")
        done, pending = await asyncio.wait(
            [get_task, shut_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if get_task in done:
            return get_task.result()  # consume item even if shutdown also fired
        return None  # shutdown fired, no queue item to consume

    async def _try_run_job(self, name: str, job, callback=None) -> None:
        """Execute *job*. If *callback* is not None, call it with the result.

        Args:
            name: Human-readable job name for logging.
            job: Async callable returning a dict.
            callback: ``(bool, str) -> None`` or ``None``.
                      ``None`` means timer-triggered — skip notification.
        """
        try:
            print(f"\n[{name}] Starting...")
            result = await job()
            print(f"[{name}] Complete.")
            if callback is not None:
                try:
                    if isinstance(result, dict) and "success" in result:
                        callback(result["success"], result.get("summary", f"{name} 完成"))
                    elif result:
                        callback(True, str(result)[:500])
                    else:
                        callback(True, f"{name} 完成")
                except Exception as cb_err:
                    print(f"[{name}] Callback failed (non-fatal): {cb_err}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{name}] Failed (non-fatal): {e}")
            if callback is not None:
                callback(False, str(e)[:500])

    # ── Jobs (the actual work — no duplication) ──────────────────

    async def _crawl_job(self) -> dict:
        """Fetch news (with content) → save to PostgreSQL → retry failures."""
        crawler = Crawler(self.config, pg_db=self.db)

        # 1. Normal fetch
        result = await self._run_in_thread(
            crawler.fetch_all, OutputStyle.POSTGRESQL, True, True
        )
        total = result.get("total", 0) if result else 0

        # 2. Retry previously failed tasks (separate from fetch_all)
        retry_result = await self._run_in_thread(
            crawler.retry_failed_tasks
        )

        # Merge summaries
        parts = []
        if total > 0:
            parts.append(f"抓取 {total} 条")
        else:
            parts.append("抓取完成，无新新闻")
        if retry_result:
            cs = retry_result.get("content_success", 0)
            iss = retry_result.get("image_success", 0)
            if cs or iss:
                parts.append(f"重试成功 content={cs} image={iss}")
        summary = "，".join(parts)

        return {"success": True, "summary": summary, "count": total}

    async def _sync_job(self) -> dict:
        """Sync cloud SQLite data into PostgreSQL."""
        cloud_config = self.config["storage"]["cloud"]
        if not (cloud_config.get("bucket_name") and cloud_config.get("endpoint_url")):
            return {"success": True, "summary": "云端未配置 — 已跳过同步", "count": 0}
        crawler = Crawler(self.config, pg_db=self.db)
        result = await self._run_in_thread(crawler.sync_from_cloud)
        total = result.get("upserted", 0) if result else 0
        return {
            "success": True,
            "summary": f"同步完成，新增 {total} 条" if total > 0 else "同步完成，无新数据",
            "count": total,
        }

    # ── Run ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main entry point.  Web starts first; data work is background."""
        print("\n" + "=" * 60)
        print("  NewsRadar Daemon — starting up")
        print("=" * 60 + "\n")

        # 1. Database init (fast, synchronous — must finish before web)
        self._init_database()

        # 2. Install signal handlers (set event, do NOT cancel tasks)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_signal)

        # 3. Start web server first — non-blocking
        queues = {
            "crawl": self._crawl_queue,
            "sync": self._sync_queue,
            "crawl_lock": self._crawl_lock,
            "sync_lock": self._sync_lock,
        }
        s3_config = self.config.get("storage", {}).get("resource", {})
        crawler = Crawler(self.config, pg_db=self.db)

        # 条件构建 Agent（仅当配置了 models 时）
        agent = None
        persona_manager = None
        persona_orchestrator = None
        if self.config.get("models"):
            from agent.factory import (
                create_agent,
                create_persona_orchestrator,
            )

            agent = await create_agent(
                self.config["models"],
                system_prompt="你是 NewsRadar 新闻助手，可以查询新闻、天气，以及使用其他工具。",
                register_mcp=True,
            )
            print("[Daemon] Agent built (with ReActExecutor + tools).")

            # 角色扮演：单角色懒构建管理器 + 多角色编排器（共享同一 manager）
            persona_orchestrator = await create_persona_orchestrator(
                self.config, db=self.db
            )
            persona_manager = persona_orchestrator._manager
            selectable = [s for s in persona_manager.available() if s.category != "editor"]
            print(
                f"[Daemon] PersonaOrchestrator built "
                f"({len(selectable)} selectable personas + editor)."
            )

        # 创建 Web 应用（含条件注册 Agent 路由）
        tool_registry = None
        agent_factory = None
        if self.config.get("models"):
            from agent.tools.tools import setup_builtin_tools
            from agent.factory import AgentFactory

            tool_registry = setup_builtin_tools()
            agent_factory = AgentFactory(
                self.config["models"],
                self.db,
                tool_registry,
            )

        app = create_app(self.db, s3_config, queues=queues, crawler=crawler,
                          agent_config=self.config, agent_instance=agent,
                          persona_manager=persona_manager,
                          persona_orchestrator=persona_orchestrator,
                          tool_registry=tool_registry,
                          agent_factory=agent_factory)

        web_task = asyncio.create_task(self._serve_web(app), name="web")

        # 4. Launch Workers (wait for signal → execute job → loop)
        for coro, lock in [
            (lambda: self._worker("Crawl", self._crawl_queue, self._crawl_job, self._crawl_lock), self._crawl_lock),
            (lambda: self._worker("Sync", self._sync_queue, self._sync_job, self._sync_lock), self._sync_lock),
        ]:
            worker = asyncio.create_task(coro(), name=coro.__name__)
            self._bg_tasks.append(worker)

        # 5. Launch Timers (put None into queue every N minutes)
        crawl_interval = self.config.get("crawler", {}).get("crawl_circle", 60)
        sync_interval = self.config.get("crawler", {}).get("sync_circle", 60)

        for queue, interval, name, lock in [
            (self._crawl_queue, crawl_interval, "Crawl", self._crawl_lock),
            (self._sync_queue, sync_interval, "Sync", self._sync_lock),
        ]:
            timer = asyncio.create_task(self._timer(queue, interval, name, lock), name=f"timer_{name}")
            self._bg_tasks.append(timer)

        # 6. Manually trigger both on startup (fire immediately)
        # await self._crawl_queue.put(None)
        # await self._sync_queue.put(None)

        # 7. Create shutdown watcher
        shutdown_task = asyncio.create_task(
            self._shutdown_event.wait(), name="shutdown_watcher"
        )

        print("[Daemon] Web server ready — background tasks running.")
        print("[Daemon] Press Ctrl+C to stop.\n")

        # 8. Wait for web failure or shutdown signal
        try:
            done, pending = await asyncio.wait(
                [web_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if web_task in done and not self._shutdown_event.is_set():
                exc = web_task.exception()
                if exc:
                    print(f"[Daemon] Web server failed: {exc}")
        except asyncio.CancelledError:
            pass

        # 9. Graceful shutdown
        await self._shutdown()

    def _handle_signal(self) -> None:
        """Signal handler — set the shutdown event."""
        if not self._shutdown_event.is_set():
            self._shutdown_event.set()
            print("\n[Daemon] Shutdown signal received. Stopping...")

    async def _shutdown(self) -> None:
        """Graceful shutdown — stops uvicorn, cancels tasks, frees executor."""
        print("[Daemon] Shutting down...")

        # 1. Tell uvicorn to exit gracefully
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

        # 2. Cancel all background async tasks
        for task in self._bg_tasks:
            if not task.done():
                task.cancel()

        # 3. Wait for async tasks (with timeout)
        all_tasks = self._bg_tasks + [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and t not in self._bg_tasks
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*all_tasks, return_exceptions=True),
                timeout=_SHUTDOWN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print("[Daemon] Some async tasks did not stop in time.")

        # 4. Shut down the thread pool — cancel pending, don't wait for running
        self._executor.shutdown(wait=False, cancel_futures=True)
        print("[Daemon] Thread pool stopped.")

        # 5. Close database
        self.db.close()
        print("[Daemon] Shutdown complete.")

    # ── Web server ───────────────────────────────────────────────

    async def _serve_web(self, app) -> None:
        """Run the FastAPI application via uvicorn."""
        import uvicorn

        web_cfg = self.config.get("web", {})
        host = web_cfg.get("host", "0.0.0.0")
        port = web_cfg.get("port", 8000)

        config = uvicorn.Config(
            app, host=host, port=port, log_level="info",
        )
        server = uvicorn.Server(config)
        self._uvicorn_server = server
        print(f"[Daemon] Web server starting on {host}:{port}")
        await server.serve()

    # ── Utilities ────────────────────────────────────────────────

    async def _sleep_or_shutdown(self, seconds: float) -> None:
        """Sleep that wakes early when shutdown is requested."""
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(), timeout=seconds
            )
        except asyncio.TimeoutError:
            pass


# =========================================================================
# Entry point
# =========================================================================

if __name__ == "__main__":
    try:
        daemon = NewsRadarDaemon()
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        print("\n[Daemon] Interrupted. Goodbye.")
        sys.exit(0)
    except Exception as e:
        print(f"\n[Daemon] Fatal error: {e}")
        sys.exit(1)
