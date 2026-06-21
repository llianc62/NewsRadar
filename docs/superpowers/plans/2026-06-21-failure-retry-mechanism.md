# Failure Recording & Lazy Retry Mechanism — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce immediate HTTP retry (3×, in-function) and lazy cross-cycle retry (max 3 cycles, PostgreSQL-backed `failed_tasks` table) for content fetch and image download failures, without modifying the `fetch_all` flow.

**Architecture:** `http_get_with_retry` extracted to `utils.py` as a shared helper. `_download_and_parse` and `ImageProcessor._download_and_save` use it for immediate retry, recording failures into `failed_tasks`. A standalone `Crawler.retry_failed_tasks()` method handles lazy retry, called by `_crawl_job` after `fetch_all`. Lazy retry is daemon-only; Cloud CI gets immediate retry only.

**Tech Stack:** Python 3.12+, PostgreSQL 16, psycopg2, requests

## Global Constraints

- Minimum test coverage: 80%
- Immediate retry: hardcoded 3 attempts (not configurable)
- Lazy retry: default max 3 cycles, configurable via `CRAWLER_MAX_RETRY`
- All business data in `failed_tasks.context` JSONB; fixed columns only for retry state machine
- `fetch_all` must NOT contain any lazy retry logic
- Cloud CI mode (`cli/crawl.py`) does NOT call `retry_failed_tasks`

---

### Task 1: Shared HTTP retry helper — `utils.py`

**Files:**
- Modify: `utils.py:1-107`

**Interfaces:**
- Produces: `MAX_IMMEDIATE_RETRIES: int = 3`, `http_get_with_retry(session, url, timeout, label) -> Tuple[Optional[requests.Response], Optional[str]]`

- [ ] **Step 1: Add `import time` and `import requests` to utils.py**

The file currently imports from `datetime`, `typing`, `urllib.parse`, and `pytz`. Add the missing imports:

```python
import time
from typing import Optional, Tuple

import requests
```

- [ ] **Step 2: Add `MAX_IMMEDIATE_RETRIES` constant and `http_get_with_retry` function**

Append after the `normalize_url` function (after line 107):

```python
# ── HTTP retry helper ─────────────────────────────────────────────

MAX_IMMEDIATE_RETRIES = 3


def http_get_with_retry(
    session: requests.Session,
    url: str,
    timeout: int = 30,
    label: str = "",
) -> Tuple[Optional[requests.Response], Optional[str]]:
    """HTTP GET with exponential backoff retry.

    Args:
        session: ``requests.Session`` to use.
        url: Target URL.
        timeout: Request timeout in seconds.
        label: Human-readable label for log messages (defaults to url).

    Returns:
        ``(response, None)`` on success, ``(None, error_message)`` on
        final failure after exhausting all retries.
    """
    display = label or url
    for attempt in range(1, MAX_IMMEDIATE_RETRIES + 1):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp, None
        except requests.RequestException as e:
            if attempt == MAX_IMMEDIATE_RETRIES:
                return None, str(e)
            time.sleep(2 ** attempt)  # 2s, 4s
```

- [ ] **Step 3: Verify the module imports correctly**

```bash
python -c "from utils import MAX_IMMEDIATE_RETRIES, http_get_with_retry; print('OK:', MAX_IMMEDIATE_RETRIES)"
```

Expected: `OK: 3`

- [ ] **Step 4: Commit**

```bash
git add utils.py
git commit -m "feat: add http_get_with_retry helper with exponential backoff"
```

---

### Task 2: Config — `max_retry` in crawler section

**Files:**
- Modify: `config/loader.py:58-65`

**Interfaces:**
- Produces: `config["crawler"]["max_retry"]` (int, default 3)
- Consumes: `CRAWLER_MAX_RETRY` env var (optional)

- [ ] **Step 1: Add `max_retry` and `sync_interval_minutes` to `_load_crawler_config`**

Replace the function body at [config/loader.py:58-65](config/loader.py#L58):

```python
def _load_crawler_config(raw: Dict) -> Dict:
    crawler = raw.get("crawler", {})
    return {
        "daemon_interval_minutes": crawler.get("daemon_interval_minutes", 60),
        "sync_interval_minutes": crawler.get("sync_interval_minutes", 60),
        "max_retry": _get_env_int("CRAWLER_MAX_RETRY")
        or crawler.get("max_retry", 3),
        "max_workers": crawler.get("max_workers", 8),
        "timeout": crawler.get("timeout", 30),
        "newsnow": _load_newsnow_config(raw),
        "rss": _load_rss_config(raw),
    }
```

> **Note:** `max_workers` and `timeout` were previously read directly from `crawler` dict inside `Crawler.__init__`. Moving them into the config loader is a cleanup; the `Crawler.__init__` will now read them from the already-processed config dict (see Task 6).

- [ ] **Step 2: Verify config loads correctly**

```bash
python -c "from config.loader import load_config; c = load_config(); print('max_retry:', c['crawler']['max_retry'])"
```

Expected: `max_retry: 3`

- [ ] **Step 3: Commit**

```bash
git add config/loader.py
git commit -m "feat: add max_retry to crawler config with CRAWLER_MAX_RETRY env var"
```

---

### Task 3: PostgreSQL — `failed_tasks` DDL + 7 methods

**Files:**
- Modify: `storage/postgres.py`

**Interfaces:**
- Produces:
  - `record_failure(self, task_type: str, context: dict, max_retry: int = 3) -> Optional[int]`
  - `get_pending_failures(self, task_type: Optional[str] = None) -> List[Dict[str, Any]]`
  - `article_has_content(self, url: str) -> bool`
  - `mark_failure_completed(self, task_id: int) -> None`
  - `mark_failure_retried(self, task_id: int, error: str = "") -> None`
  - `find_articles_by_image_url(self, image_url: str) -> List[int]`
  - `update_article_image_url(self, article_id: int, old_url: str, new_path: str) -> None`

- [ ] **Step 1: Add `failed_tasks` DDL to `_run_migrations`**

In `_run_migrations` (after the existing Migration 002 block, before the `finally`), add Migration 003:

```python
                # Migration 003: create failed_tasks table for failure recording & lazy retry
                cur.execute(
                    """SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name = 'failed_tasks'
                    )"""
                )
                has_failed_tasks = cur.fetchone()[0]
                if not has_failed_tasks:
                    print("[DB] Migrating: creating failed_tasks table...")
                    cur.execute(
                        """CREATE TABLE IF NOT EXISTS failed_tasks (
                            id              BIGSERIAL PRIMARY KEY,
                            task_type       VARCHAR(50) NOT NULL,
                            context         JSONB NOT NULL DEFAULT '{}',
                            retry_times     INTEGER NOT NULL DEFAULT 0,
                            max_retry       INTEGER NOT NULL DEFAULT 3,
                            last_retry      TIMESTAMPTZ DEFAULT NULL,
                            status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                                                CHECK (status IN ('pending', 'failed', 'completed')),
                            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )"""
                    )
                    cur.execute(
                        """CREATE UNIQUE INDEX IF NOT EXISTS idx_failed_tasks_dedup
                           ON failed_tasks (task_type, (context->>'url'))
                           WHERE status = 'pending'"""
                    )
                    cur.execute(
                        """CREATE INDEX IF NOT EXISTS idx_failed_tasks_status
                           ON failed_tasks (status, task_type, retry_times)"""
                    )
                    conn.commit()
                    print("[DB] Migration complete: failed_tasks table created.")
```

- [ ] **Step 2: Add the 7 new methods to the `PostgreSQL` class**

Insert after the `mark_notified` method (or at the end of the class, before any private helpers). Find the right insertion point by searching for the last public method, then append:

```python
    # ── Failed tasks (failure recording & lazy retry) ──────────────

    def record_failure(
        self,
        task_type: str,
        context: dict,
        max_retry: int = 3,
    ) -> Optional[int]:
        """Record a failed task for later retry.

        Uses ``INSERT ... ON CONFLICT DO NOTHING`` so duplicate pending
        tasks for the same URL + task_type are silently ignored.

        Returns:
            The new task ``id``, or ``None`` if a pending task for the
            same URL + task_type already exists.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO failed_tasks (task_type, context, max_retry)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (task_type, (context->>'url'))
                       WHERE status = 'pending'
                       DO NOTHING
                       RETURNING id""",
                    (task_type, json.dumps(context), max_retry),
                )
                row = cur.fetchone()
                return row[0] if row else None

    def get_pending_failures(
        self,
        task_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return pending failed tasks where retry_times < max_retry.

        Args:
            task_type: Optional filter.  When ``None``, returns all types.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                if task_type:
                    cur.execute(
                        """SELECT id, task_type, context, retry_times, max_retry,
                                  last_retry, status, created_at, updated_at
                           FROM failed_tasks
                           WHERE status = 'pending'
                             AND retry_times < max_retry
                             AND task_type = %s
                           ORDER BY created_at""",
                        (task_type,),
                    )
                else:
                    cur.execute(
                        """SELECT id, task_type, context, retry_times, max_retry,
                                  last_retry, status, created_at, updated_at
                           FROM failed_tasks
                           WHERE status = 'pending'
                             AND retry_times < max_retry
                           ORDER BY created_at"""
                    )
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "task_type": r[1],
                        "context": r[2],
                        "retry_times": r[3],
                        "max_retry": r[4],
                        "last_retry": r[5],
                        "status": r[6],
                        "created_at": r[7],
                        "updated_at": r[8],
                    }
                    for r in rows
                ]

    def article_has_content(self, url: str) -> bool:
        """Check whether any article with *url* already has non-empty content.

        Used to skip content_fetch retry when the article was already
        fetched successfully through the normal crawl path.
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM news_articles
                       WHERE url = %s
                         AND content IS NOT NULL
                         AND content != ''
                       LIMIT 1""",
                    (url,),
                )
                return cur.fetchone() is not None

    def mark_failure_completed(self, task_id: int) -> None:
        """Mark a failed task as successfully retried."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE failed_tasks
                       SET status = 'completed',
                           updated_at = NOW()
                       WHERE id = %s""",
                    (task_id,),
                )

    def mark_failure_retried(self, task_id: int, error: str = "") -> None:
        """Increment retry_times and set last_retry.

        If retry_times reaches max_retry after increment, set status to
        ``'failed'`` (permanent).
        """
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE failed_tasks
                       SET retry_times = retry_times + 1,
                           last_retry = NOW(),
                           updated_at = NOW(),
                           status = CASE
                               WHEN retry_times + 1 >= max_retry
                               THEN 'failed'
                               ELSE 'pending'
                           END
                       WHERE id = %s""",
                    (task_id,),
                )

    def find_articles_by_image_url(self, image_url: str) -> List[int]:
        """Return article IDs whose content contains *image_url*."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id FROM news_articles
                       WHERE content LIKE %s""",
                    (f"%{image_url}%",),
                )
                return [r[0] for r in cur.fetchall()]

    def update_article_image_url(
        self,
        article_id: int,
        old_url: str,
        new_path: str,
    ) -> None:
        """Replace *old_url* with *new_path* in an article's content."""
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE news_articles
                       SET content = REPLACE(content, %s, %s),
                           updated_at = NOW()
                       WHERE id = %s""",
                    (old_url, new_path, article_id),
                )
```

- [ ] **Step 3: Add `import json` at the top of storage/postgres.py if not already present**

Check the imports; if `json` is not imported, add it:

```python
import json
```

- [ ] **Step 4: Run the existing tests to confirm no regression**

```bash
pytest tests/ -v --timeout=30
```

Expected: all existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add storage/postgres.py
git commit -m "feat: add failed_tasks table and 7 query methods for failure recording"
```

---

### Task 4: Fetcher retry — NewsnowFetcher + RssFetcher

**Files:**
- Modify: `news/fetcher/newsnow.py:49-103`
- Modify: `news/fetcher/rss.py:411-477`

**Interfaces:**
- Consumes: `MAX_IMMEDIATE_RETRIES` from `utils`, `http_get_with_retry` from `utils`

- [ ] **Step 1: NewsnowFetcher — use `MAX_IMMEDIATE_RETRIES` constant**

In `news/fetcher/newsnow.py`, add the import at the top:

```python
from utils import MAX_IMMEDIATE_RETRIES
```

Then in `NewsFetcher.fetch_data`, change the default value of `max_retries` from `2` to `MAX_IMMEDIATE_RETRIES - 1` (since the existing loop uses `max_retries + 1` total attempts):

```python
    def fetch_data(
        self,
        id_info: Union[str, Tuple[str, str]],
        max_retries: int = MAX_IMMEDIATE_RETRIES - 1,  # was 2
    ) -> Tuple[Optional[str], str, str]:
```

> `MAX_IMMEDIATE_RETRIES = 3`, so `max_retries = 2` → `range(3)` = 3 total attempts. Same behavior, unified constant.

- [ ] **Step 2: RssFetcher — use `http_get_with_retry` in `fetch_feed`**

In `news/fetcher/rss.py`, add the import at the top:

```python
from utils import http_get_with_retry
```

Then in `RssFetcher.fetch_feed`, replace lines 424-426:

```python
        try:
            response = self._session.get(feed.url, timeout=self._timeout)
            response.raise_for_status()
```

with:

```python
        try:
            response, http_error = http_get_with_retry(
                self._session, feed.url, self._timeout, label=feed.name
            )
            if response is None:
                return [], http_error
```

- [ ] **Step 3: Run tests to confirm no regression**

```bash
pytest tests/ -v --timeout=30
```

- [ ] **Step 4: Commit**

```bash
git add news/fetcher/newsnow.py news/fetcher/rss.py
git commit -m "feat: add immediate retry to fetcher API calls (NewsnowFetcher + RssFetcher)"
```

---

### Task 5: ImageProcessor — immediate retry in `_download_and_save`

**Files:**
- Modify: `news/images.py:143-175`

**Interfaces:**
- Consumes: `http_get_with_retry` from `utils`

- [ ] **Step 1: Add import at the top of `news/images.py`**

```python
from utils import http_get_with_retry
```

- [ ] **Step 2: Replace `_download_and_save` with retry version**

Replace the method at [news/images.py:143-175](news/images.py#L143):

```python
    def _download_and_save(
        self,
        url: str,
        target_path: str,
        storage: FileStorage,
    ) -> Optional[str]:
        """Download *url* and save directly to *target_path* (full S3 key).

        HTTP GET uses exponential backoff (3 attempts).  Save also retries
        up to 3 times for transient storage errors.

        Returns ``"images/xxx.jpg"`` (relative path for content
        replacement) on success, or ``None`` on failure.
        """
        # Phase 1: HTTP download with retry
        resp, error = http_get_with_retry(
            self.session, url, timeout=30, label=url
        )
        if resp is None:
            print(f"[ImageProcessor] HTTP error for {url}: {error}")
            return None

        content_type = (
            resp.headers.get("Content-Type", "image/jpeg")
            .split(";")[0]
            .strip()
        )
        image_data = resp.content

        # Phase 2: Save with retry (MinIO may be temporarily unavailable)
        ext = self.EXT_MAP.get(content_type, ".jpg")
        filename = self._extract_filename(url, ext)
        file_path = f"{target_path}/{filename}"

        for attempt in range(1, 4):  # 3 attempts
            try:
                storage.save(image_data, file_path, content_type)
                return f"images/{filename}"
            except Exception as e:
                if attempt == 3:
                    print(f"[ImageProcessor] Save failed [{url}]: {e}")
                    return None
                time.sleep(2 ** attempt)
```

- [ ] **Step 3: Add `import time` at the top of `news/images.py` if not already present**

Check existing imports; add if missing:

```python
import time
```

- [ ] **Step 4: Commit**

```bash
git add news/images.py
git commit -m "feat: add immediate retry to image download and save in ImageProcessor"
```

---

### Task 6: Crawler — core changes (retry + recording + retry_failed_tasks)

**Files:**
- Modify: `news/crawler.py`

**Interfaces:**
- Consumes: `http_get_with_retry`, `MAX_IMMEDIATE_RETRIES` from `utils`
- Produces: `retry_failed_tasks(self, with_image: bool = True) -> dict` (public)
- Produces (internal): `_record_content_fetch_failure`, `_record_image_download_failures`, `_retry_content_fetch_failures`, `_retry_image_download_failures`

- [ ] **Step 1: Add `max_retry` to `Crawler.__init__`**

In `Crawler.__init__` (around line 74-76), update to read `max_retry` from config:

```python
        cfg = config.get("crawler", {})
        self.max_workers = cfg.get("max_workers", 8)
        self.timeout = cfg.get("timeout", 30)
        self.max_retry = cfg.get("max_retry", 3)          # ★ new
```

- [ ] **Step 2: Add import for `http_get_with_retry` at the top of `news/crawler.py`**

```python
from utils import (
    format_date_today, format_time_now, sanitize_filename,
    http_get_with_retry,  # ★ new
)
```

- [ ] **Step 3: Modify `_download_and_parse` — use `http_get_with_retry` + record failures**

Replace the HTTP GET block in `_download_and_parse` (lines 381-386):

```python
        try:
            resp = self.session().get(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[Crawler] HTTP error for {url}: {e}")
            return False
```

with:

```python
        resp, error = http_get_with_retry(
            self.session(), url, self.timeout, label=url
        )
        if resp is None:
            print(f"[Crawler] HTTP error for {url}: {error}")
            self._record_content_fetch_failure(item, error)
            return False
```

- [ ] **Step 4: Modify `_run_batch_parse` — skip items that already have content**

Change the `valid` filter at line 346:

```python
        valid = [it for it in items if it.get("url")]
```

to:

```python
        valid = [it for it in items if it.get("url") and not it.get("content")]
```

- [ ] **Step 5: Modify `_run_batch_image_download` — record image download failures**

Save the original url_map (URL→target_dir) before download so `_record_image_download_failures` can include `target_dir` in the failure context. After the `url_map = processor.download(...)` call, add failure recording:

```python
        # Save original url_map for failure recording (preserves target_dir)
        _original_url_map = dict(url_map)               # ★ new
        url_map = processor.download(url_map, storage=image_storage)

        # Record failures for lazy retry
        self._record_image_download_failures(            # ★ new
            _original_url_map, url_map
        )

        if not url_map:
            print("[Crawler] Phase 2 done (no images downloaded)")
            return
```

- [ ] **Step 6: Add `_record_content_fetch_failure` method**

Insert in the "Internal" section (after `_extract_image_urls` is a good spot):

```python
    def _record_content_fetch_failure(
        self, item: Dict[str, Any], error: str,
    ) -> None:
        """Record a failed content fetch to the ``failed_tasks`` table."""
        context = {
            "url": item.get("url", ""),
            "source_id": item.get("source_id", ""),
            "source_type": item.get("source_type", ""),
            "source_name": item.get("source_name", ""),
            "title": item.get("title", ""),
            "rank": item.get("rank", 0),
            "guid": item.get("guid", ""),
            "mobile_url": item.get("mobile_url", ""),
            "published_at": item.get("published_at", ""),
        }
        try:
            pg = self._get_pg_db()
            task_id = pg.record_failure("content_fetch", context, self.max_retry)
            if task_id:
                print(f"[Crawler] Recorded content_fetch failure: {item.get('url')}")
        except Exception as e:
            print(f"[Crawler] Failed to record content_fetch failure: {e}")
```

- [ ] **Step 7: Add `_record_image_download_failures` method**

```python
    def _record_image_download_failures(
        self,
        original_url_map: Dict[str, str],
        results: Dict[str, str],
    ) -> None:
        """For each image URL where result is ``""``, record a failure.

        Args:
            original_url_map: ``{url: target_dir}`` (pre-download, for context).
            results: ``{url: saved_path_or_""}`` (post-download, for checking).
        """
        for url, saved_path in results.items():
            if saved_path:
                continue
            context = {
                "url": url,
                "target_dir": original_url_map.get(url, ""),
            }
            try:
                pg = self._get_pg_db()
                pg.record_failure("image_download", context, self.max_retry)
                print(f"[Crawler] Recorded image_download failure: {url}")
            except Exception as e:
                print(f"[Crawler] Failed to record image_download failure: {e}")
```

- [ ] **Step 8: Add `_retry_content_fetch_failures` method**

```python
    def _retry_content_fetch_failures(self) -> List[Dict[str, Any]]:
        """Retry previously failed content_fetch tasks.

        Queries ``failed_tasks`` for pending content_fetch tasks, calls
        ``_download_and_parse`` for each, and returns successfully
        retried items as dicts.
        """
        pg = self._get_pg_db()
        tasks = pg.get_pending_failures(task_type="content_fetch")
        if not tasks:
            return []

        print(f"[Crawler] Retrying {len(tasks)} content_fetch failures...")
        retried: List[Dict[str, Any]] = []

        for task in tasks:
            ctx = task["context"]
            url = ctx.get("url", "")
            if not url:
                pg.mark_failure_completed(task["id"])
                continue

            # Prevent duplicate download: check if article already has content
            if pg.article_has_content(url):
                pg.mark_failure_completed(task["id"])
                print(f"[Crawler] Article already has content, skip retry: {url}")
                continue

            # Reconstruct item dict from context
            item: Dict[str, Any] = {
                "url": url,
                "source_id": ctx.get("source_id", ""),
                "source_type": ctx.get("source_type", ""),
                "source_name": ctx.get("source_name", ""),
                "title": ctx.get("title", ""),
                "rank": ctx.get("rank", 0),
                "guid": ctx.get("guid", ""),
                "mobile_url": ctx.get("mobile_url", ""),
                "published_at": ctx.get("published_at", ""),
                "summary": "",
                "author": "",
                "content": "",
                "category": "",
                "tags": [],
                "ranks": [],
            }

            success = self._download_and_parse(item)
            if success:
                pg.mark_failure_completed(task["id"])
                retried.append(item)
                print(f"[Crawler] Retry success (content_fetch): {url}")
            else:
                pg.mark_failure_retried(task["id"], error="HTTP failed after retries")
                print(f"[Crawler] Retry failed (content_fetch): {url}")

        return retried
```

- [ ] **Step 9: Add `_retry_image_download_failures` method**

```python
    def _retry_image_download_failures(self) -> Dict[str, int]:
        """Retry previously failed image_download tasks.

        Downloads each failed image, updates article content with the
        new image path, and marks the task completed.

        Must be called AFTER articles are persisted.
        """
        pg = self._get_pg_db()
        tasks = pg.get_pending_failures(task_type="image_download")
        if not tasks:
            return {"total": 0, "success": 0}

        print(f"[Crawler] Retrying {len(tasks)} image_download failures...")
        processor = self._get_image_processor()
        storage = self._resource_storage

        total = len(tasks)
        success = 0

        for task in tasks:
            ctx = task["context"]
            url = ctx.get("url", "")
            target_dir = ctx.get("target_dir", "")

            if not url:
                pg.mark_failure_completed(task["id"])
                continue

            result = processor.download({url: target_dir}, storage)
            saved_path = result.get(url, "")

            if saved_path:
                pg.mark_failure_completed(task["id"])
                success += 1

                # Update article content — replace old URL with new path
                article_ids = pg.find_articles_by_image_url(url)
                for article_id in article_ids:
                    pg.update_article_image_url(article_id, url, saved_path)

                print(f"[Crawler] Retry success (image_download): {url}")
            else:
                pg.mark_failure_retried(task["id"], error="Image download failed")
                print(f"[Crawler] Retry failed (image_download): {url}")

        return {"total": total, "success": success}
```

- [ ] **Step 10: Add `retry_failed_tasks` — the public lazy retry entry point**

```python
    def retry_failed_tasks(self, with_image: bool = True) -> dict:
        """Retry previously failed content_fetch and image_download tasks.

        Called by the daemon AFTER ``fetch_all`` in each crawl cycle.
        Does NOT modify ``fetch_all`` — lazy retry is a separate step.

        Returns a summary dict with counts.
        """
        print("\n[Crawler] === Lazy retry: checking failed tasks ===")
        result = {
            "content_retried": 0,
            "content_success": 0,
            "image_retried": 0,
            "image_success": 0,
        }

        # 1. Retry content_fetch failures
        try:
            retried_items = self._retry_content_fetch_failures()
            result["content_retried"] = len(retried_items)

            if retried_items:
                # Enrich + persist retried items
                self.enrich_content(*retried_items, with_image=with_image)
                self.persist(
                    *retried_items, output_style=OutputStyle.POSTGRESQL
                )
                result["content_success"] = len(retried_items)
        except Exception as e:
            print(f"[Crawler] Content retry error (non-fatal): {e}")

        # 2. Retry image_download failures (must be AFTER persist)
        if with_image:
            try:
                img_result = self._retry_image_download_failures()
                result["image_retried"] = img_result["total"]
                result["image_success"] = img_result["success"]
            except Exception as e:
                print(f"[Crawler] Image retry error (non-fatal): {e}")

        print(f"[Crawler] Lazy retry done: {result}")
        return result
```

- [ ] **Step 11: Run tests to confirm no regression**

```bash
pytest tests/ -v --timeout=30
```

- [ ] **Step 12: Commit**

```bash
git add news/crawler.py
git commit -m "feat: add failure recording and lazy retry to Crawler (retry_failed_tasks)"
```

---

### Task 7: Daemon integration — `_crawl_job` in `main.py`

**Files:**
- Modify: `main.py:169-180`

**Interfaces:**
- Consumes: `Crawler.retry_failed_tasks()`

- [ ] **Step 1: Replace `_crawl_job` with version that includes retry**

Replace the method at [main.py:169-180](main.py#L169):

```python
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
```

- [ ] **Step 2: Verify Python syntax**

```bash
python -c "import main; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: integrate retry_failed_tasks into daemon _crawl_job"
```

---

### Task 8: Unit tests — `tests/test_failure_retry.py`

**Files:**
- Create: `tests/test_failure_retry.py`

- [ ] **Step 1: Write the test file**

```python
# coding=utf-8
"""Tests for failure recording and retry mechanism."""

import json
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════
# PostgreSQL method tests
# ═══════════════════════════════════════════════════════════════════


class TestRecordFailure:
    """Tests for PostgreSQL.record_failure."""

    def test_record_failure_inserts(self, pg_db):
        """Normal failure recording returns a task id."""
        task_id = pg_db.record_failure(
            "content_fetch",
            {"url": "https://example.com/article/1"},
        )
        assert task_id is not None
        assert task_id > 0

    def test_record_failure_dedup(self, pg_db):
        """Same URL + task_type does not create duplicate pending tasks."""
        context = {"url": "https://example.com/article/dedup"}
        id1 = pg_db.record_failure("content_fetch", context)
        id2 = pg_db.record_failure("content_fetch", context)
        assert id1 is not None
        assert id2 is None  # dedup — returns None

    def test_record_failure_different_type_same_url(self, pg_db):
        """Same URL + different task_type creates separate records."""
        context = {"url": "https://example.com/img.jpg"}
        id1 = pg_db.record_failure("content_fetch", context)
        id2 = pg_db.record_failure("image_download", context)
        assert id1 is not None
        assert id2 is not None
        assert id1 != id2

    def test_record_failure_no_url(self, pg_db):
        """Context without 'url' key still works (no dedup constraint match)."""
        task_id = pg_db.record_failure(
            "content_fetch",
            {"title": "no url here"},
        )
        assert task_id is not None

    def test_record_failure_completed_then_new_pending(self, pg_db):
        """After marking pending as completed, a new pending can be inserted."""
        context = {"url": "https://example.com/re-insert"}
        id1 = pg_db.record_failure("content_fetch", context)
        pg_db.mark_failure_completed(id1)
        id2 = pg_db.record_failure("content_fetch", context)
        assert id2 is not None
        assert id2 != id1


class TestGetPendingFailures:
    """Tests for PostgreSQL.get_pending_failures."""

    def test_get_pending_returns_pending(self, pg_db):
        """Returns tasks with status='pending' and retry_times < max_retry."""
        pg_db.record_failure(
            "content_fetch",
            {"url": "https://example.com/pending"},
        )
        tasks = pg_db.get_pending_failures(task_type="content_fetch")
        assert len(tasks) >= 1
        assert all(t["status"] == "pending" for t in tasks)

    def test_get_pending_excludes_exceeded(self, pg_db):
        """Excludes tasks where retry_times >= max_retry."""
        pg_db.record_failure(
            "content_fetch",
            {"url": "https://example.com/exceeded"},
            max_retry=1,
        )
        # Manually exceed max_retry by calling mark_failure_retried
        tasks_before = pg_db.get_pending_failures(task_type="content_fetch")
        for t in tasks_before:
            pg_db.mark_failure_retried(t["id"])
        tasks_after = pg_db.get_pending_failures(task_type="content_fetch")
        urls = [t["context"].get("url") for t in tasks_after]
        assert "https://example.com/exceeded" not in urls

    def test_get_pending_all_types(self, pg_db):
        """Without task_type filter, returns all pending types."""
        pg_db.record_failure(
            "content_fetch", {"url": "https://example.com/a"},
        )
        pg_db.record_failure(
            "image_download", {"url": "https://example.com/b.jpg"},
        )
        tasks = pg_db.get_pending_failures()
        types = {t["task_type"] for t in tasks}
        assert "content_fetch" in types
        assert "image_download" in types


class TestArticleHasContent:
    """Tests for PostgreSQL.article_has_content."""

    def test_article_has_content_false(self, pg_db):
        """Returns False when no article with the URL exists."""
        assert pg_db.article_has_content("https://nonexistent.example.com") is False

    # test_article_has_content_true requires a real article row —
    # covered in integration tests (Task 9).


class TestMarkFailureCompleted:
    """Tests for PostgreSQL.mark_failure_completed."""

    def test_mark_failure_completed(self, pg_db):
        """Sets status to 'completed'."""
        task_id = pg_db.record_failure(
            "content_fetch",
            {"url": "https://example.com/to-complete"},
        )
        pg_db.mark_failure_completed(task_id)
        # Verify — re-query should not return this task as pending
        tasks = pg_db.get_pending_failures(task_type="content_fetch")
        ids = [t["id"] for t in tasks]
        assert task_id not in ids


class TestMarkFailureRetried:
    """Tests for PostgreSQL.mark_failure_retried."""

    def test_mark_failure_retried_increments(self, pg_db):
        """Increments retry_times and sets last_retry."""
        task_id = pg_db.record_failure(
            "content_fetch",
            {"url": "https://example.com/retry-incr"},
        )
        pg_db.mark_failure_retried(task_id)
        # Task should still be pending (retry_times=1 < max_retry=3)
        tasks = pg_db.get_pending_failures(task_type="content_fetch")
        ids = [t["id"] for t in tasks]
        assert task_id in ids  # still pending, retries not exhausted

    def test_mark_failure_retried_permanent(self, pg_db):
        """When retry_times reaches max_retry, status becomes 'failed'."""
        task_id = pg_db.record_failure(
            "content_fetch",
            {"url": "https://example.com/permanent"},
            max_retry=1,
        )
        pg_db.mark_failure_retried(task_id)
        # Should no longer appear in pending
        tasks = pg_db.get_pending_failures(task_type="content_fetch")
        ids = [t["id"] for t in tasks]
        assert task_id not in ids


class TestFindArticlesByImageUrl:
    """Tests for PostgreSQL.find_articles_by_image_url."""

    def test_find_articles_by_image_url(self, pg_db):
        """Returns article IDs whose content contains the image URL."""
        # Requires a real article — tested in integration
        pass


class TestUpdateArticleImageUrl:
    """Tests for PostgreSQL.update_article_image_url."""

    def test_update_article_image_url(self, pg_db):
        """Replaces old URL with new path in article content."""
        # Requires a real article — tested in integration
        pass


# ═══════════════════════════════════════════════════════════════════
# http_get_with_retry tests
# ═══════════════════════════════════════════════════════════════════


class TestHttpGetWithRetry:
    """Tests for utils.http_get_with_retry."""

    def test_retry_success_first_attempt(self):
        """Returns response on first successful attempt."""
        from utils import http_get_with_retry

        session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        session.get.return_value = mock_resp

        resp, error = http_get_with_retry(session, "http://example.com")
        assert resp is mock_resp
        assert error is None
        assert session.get.call_count == 1

    def test_retry_success_second_attempt(self):
        """Retries and succeeds on second attempt."""
        from utils import http_get_with_retry

        session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None

        import requests
        session.get.side_effect = [
            requests.ConnectionError("timeout"),
            mock_resp,
        ]

        resp, error = http_get_with_retry(session, "http://example.com")
        assert resp is mock_resp
        assert error is None
        assert session.get.call_count == 2

    def test_retry_exhausted(self):
        """Returns None and error message when all retries exhausted."""
        from utils import http_get_with_retry

        session = MagicMock()
        import requests
        session.get.side_effect = requests.ConnectionError("timeout")

        resp, error = http_get_with_retry(session, "http://example.com")
        assert resp is None
        assert error is not None
        assert "timeout" in error
        assert session.get.call_count == 3  # MAX_IMMEDIATE_RETRIES


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def pg_db():
    """Create a PostgreSQL instance connected to test database."""
    from storage.postgres import PostgreSQL

    pg_config = {
        "host": "localhost",
        "port": 5432,
        "database": "newsradar_test",
        "user": "newsradar",
        "password": "",
        "min_connections": 1,
        "max_connections": 2,
    }
    db = PostgreSQL(pg_config)
    db.connect()
    db.init_schema()
    yield db
    # Cleanup: remove test data
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM failed_tasks")
    db.close()
```

- [ ] **Step 2: Run the unit tests**

```bash
pytest tests/test_failure_retry.py -v --timeout=30
```

Expected: tests that don't require a real PostgreSQL will pass; DB-dependent tests will skip/fail gracefully if `newsradar_test` DB is not available.

- [ ] **Step 3: Commit**

```bash
git add tests/test_failure_retry.py
git commit -m "test: add unit tests for failure recording and retry mechanism"
```

---

### Task 9: Integration test — end-to-end retry flow

**Files:**
- Modify: `tests/test_failure_retry.py` (append)

- [ ] **Step 1: Add integration test class**

Append to `tests/test_failure_retry.py`:

```python
# ═══════════════════════════════════════════════════════════════════
# Integration tests (require PostgreSQL + test infrastructure)
# ═══════════════════════════════════════════════════════════════════


class TestRetryFailedTasksIntegration:
    """End-to-end tests for retry_failed_tasks flow."""

    def test_retry_content_fetch_flow(self, pg_db, crawler):
        """Pending content_fetch task → retry → success → article persisted."""
        context = {
            "url": "https://httpbin.org/html",
            "source_id": "test",
            "source_type": "rss",
            "source_name": "Test Source",
            "title": "Test Article",
            "rank": 0,
            "guid": "test-guid-001",
            "mobile_url": "",
            "published_at": "",
        }
        pg_db.record_failure("content_fetch", context)
        result = crawler.retry_failed_tasks(with_image=False)
        assert result["content_retried"] >= 1
        # After successful retry, the article should have content
        assert pg_db.article_has_content(context["url"])

    def test_retry_image_download_flow(self, pg_db, crawler):
        """Pending image_download task → retry → success → content updated."""
        # Insert an article with a known image URL
        # First, insert an article
        from storage.postgres import _to_timestamptz
        import json

        article_url = "https://example.com/test-image-article"
        image_url = "https://httpbin.org/image/png"

        with pg_db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO news_articles
                       (source_id, source_name, source_type, tier, url,
                        title, content, crawled_from)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (
                        "test", "Test Source", "rss", 4,
                        article_url, "Test Image Article",
                        f"Some content with image: {image_url}",
                        "local",
                    ),
                )

        # Record image download failure
        pg_db.record_failure(
            "image_download",
            {"url": image_url, "target_dir": "news/test/images"},
        )

        result = crawler.retry_failed_tasks(with_image=True)
        assert result["image_retried"] >= 1
```

- [ ] **Step 2: Run integration tests**

```bash
pytest tests/test_failure_retry.py::TestRetryFailedTasksIntegration -v --timeout=60
```

- [ ] **Step 3: Run the full test suite**

```bash
pytest tests/ -v --timeout=30
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_failure_retry.py
git commit -m "test: add integration tests for retry_failed_tasks flow"
```

---

### Task 10: Final verification — daemon smoke test

- [ ] **Step 1: Start Docker services**

```bash
docker compose up -d
```

- [ ] **Step 2: Run the daemon briefly to verify schema migration + no crashes**

```bash
timeout 15 python main.py 2>&1 | head -50
```

Expected output should include:
- `[DB] Migrating: creating failed_tasks table...`
- `[DB] Migration complete: failed_tasks table created.`
- `[Daemon] Web server ready`

- [ ] **Step 3: Verify `failed_tasks` table exists in PostgreSQL**

```bash
docker compose exec postgres psql -U newsradar -d newsradar -c "\d failed_tasks"
```

Expected: table schema with all columns and indexes.

- [ ] **Step 4: Commit (if any config changes)**

```bash
git add -A
git commit -m "chore: final verification — failed_tasks migration works"
```
