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

    Background tasks use a **semaphore pattern**::

        Timer ──set()──▶ asyncio.Event ◀──await── Worker ──exec──▶ Job

    Each task type gets one ``asyncio.Event`` signal.  A *timer* sets
    the signal every N minutes; a *worker* waits for the signal then
    executes the *job*.  At startup the sync signal is set manually so
    it fires immediately.
    """

    def __init__(self, config_path: str = "config.yaml"):
        self.config = load_config(config_path)
        self.db = PostgreSQL(self.config["postgresql"])
        self._shutdown_event = asyncio.Event()
        self._bg_tasks: list[asyncio.Task] = []
        self._uvicorn_server = None
        # Dedicated executor — we control its lifecycle
        self._executor = ThreadPoolExecutor(max_workers=4)

        # ── Semaphores (one per task type) ──
        self._crawl_signal = asyncio.Event()
        self._sync_signal = asyncio.Event()

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

    async def _timer(self, signal: asyncio.Event, interval_min: int, name: str) -> None:
        """Set *signal* every *interval_min* minutes."""
        print(f"[Timer/{name}] every {interval_min} min")
        while not self._shutdown_event.is_set():
            await self._sleep_or_shutdown(interval_min * 60)
            if not self._shutdown_event.is_set():
                signal.set()

    # ── Worker ───────────────────────────────────────────────────

    async def _worker(self, name: str, signal: asyncio.Event, job) -> None:
        """Wait for *signal* → execute *job* → clear → loop."""
        print(f"[Worker/{name}] ready")
        while not self._shutdown_event.is_set():
            await self._wait_signal(signal)
            if self._shutdown_event.is_set():
                break
            signal.clear()
            await self._try_run_job(name, job)

    async def _wait_signal(self, signal: asyncio.Event) -> None:
        """Wait for *signal* or *shutdown_event* — whichever fires first."""
        sig_task = asyncio.create_task(signal.wait(), name="sig_wait")
        shut_task = asyncio.create_task(self._shutdown_event.wait(), name="shut_wait")
        done, pending = await asyncio.wait(
            [sig_task, shut_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

    async def _try_run_job(self, name: str, job) -> None:
        """Execute *job*, logging errors as non-fatal."""
        try:
            print(f"\n[{name}] Starting...")
            await job()
            print(f"[{name}] Complete.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{name}] Failed (non-fatal): {e}")

    # ── Jobs (the actual work — no duplication) ──────────────────

    async def _crawl_job(self) -> None:
        """Fetch news (with content) → save to PostgreSQL."""
        crawler = Crawler(self.config, pg_db=self.db)
        await self._run_in_thread(
            crawler.fetch_all, OutputStyle.POSTGRESQL, True, True
        )

    async def _sync_job(self) -> None:
        """Sync cloud SQLite data into PostgreSQL."""
        cloud_config = self.config["storage"]["cloud"]
        if not (cloud_config.get("bucket_name") and cloud_config.get("endpoint_url")):
            print("[Sync] Cloud not configured — skipping.")
            return
        crawler = Crawler(self.config, pg_db=self.db)
        await self._run_in_thread(crawler.sync_from_cloud)

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
        signals = {
            "crawl": self._crawl_signal,
            "sync": self._sync_signal,
        }
        s3_config = self.config.get("storage", {}).get("resource", {})
        app = create_app(self.db, s3_config, signals=signals)
        web_task = asyncio.create_task(self._serve_web(app), name="web")

        # 4. Launch Workers (wait for signal → execute job → loop)
        for coro in [
            lambda: self._worker("Crawl", self._crawl_signal, self._crawl_job),
            lambda: self._worker("Sync", self._sync_signal, self._sync_job),
        ]:
            t = asyncio.create_task(coro(), name=coro.__name__)
            self._bg_tasks.append(t)

        # 5. Launch Timers (set signal every N minutes)
        crawl_interval = self.config.get("crawler", {}).get("daemon_interval_minutes", 60)
        sync_interval = self.config.get("crawler", {}).get("sync_interval_minutes", 60)

        for sig, interval, name in [
            (self._crawl_signal, crawl_interval, "Crawl"),
            (self._sync_signal, sync_interval, "Sync"),
        ]:
            t = asyncio.create_task(self._timer(sig, interval, name), name=f"timer_{name}")
            self._bg_tasks.append(t)

        # 6. Manually trigger sync at startup (fire immediately)
        self._sync_signal.set()
        self._crawl_signal.set()

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
