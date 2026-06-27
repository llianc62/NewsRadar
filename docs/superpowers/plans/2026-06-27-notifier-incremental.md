# Notifier 增量通知 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** notifier 支持按 `created_at` 时间范围增量发送，CI 通过 GitHub API 自动获取上次运行时间。

**Architecture:** `get_all()` 扩展为通用时间范围过滤接口（`start_time`/`end_time`），`run_notifier()` 接收 ISO 8601 参数并转换为 DB 格式，CLI 透传，workflow 通过 `gh run list` 获取上次成功运行时间。

**Tech Stack:** Python 3.12, SQLite3, Typer, GitHub Actions (`gh` CLI)

## Global Constraints

- `get_all()` 过滤参数为 keyword-only，不传 = 全量，向后兼容
- 时间格式转换失败 → 日志警告 + 对应参数置 None，不丢新闻
- `start_time`/`end_time` 在 CLI、`run_notifier()`、`get_all()` 三层命名一致
- CI 场景只传 `--start-time`，不传 `--end-time`
- 不修改 DB 文件内容，只读查询

---

### Task 1: `storage/sqlite.py` — `get_all()` 扩展时间范围过滤

**Files:**
- Modify: `storage/sqlite.py:187-195`
- Create: `tests/test_sqlite.py`

**Interfaces:**
- Consumes: `sqlite3.connect`, existing `_get_connection()` method
- Produces: `get_all(self, date: str, *, start_time: str | None = None, end_time: str | None = None) -> List[sqlite3.Row]`

- [ ] **Step 1: 创建测试文件并写失败测试**

```python
# tests/test_sqlite.py
# coding=utf-8
"""Tests for :mod:`storage.sqlite` — time-range filtering."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from storage.sqlite import Sqlite


@pytest.fixture
def db(tmp_path):
    """Sqlite instance with an in-memory connection for a fixed date."""
    db = Sqlite(data_dir=str(tmp_path), timezone="Asia/Shanghai")

    # Replace _get_connection to return an in-memory DB
    def _make_conn(date: str):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Create table without partial indexes (simpler for unit test)
        conn.execute("""
            CREATE TABLE news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                tier INTEGER NOT NULL DEFAULT 4,
                priority INTEGER NOT NULL DEFAULT 0,
                url TEXT DEFAULT '',
                mobile_url TEXT DEFAULT '',
                rank INTEGER,
                heat_score INTEGER DEFAULT NULL,
                guid TEXT,
                published_at TEXT,
                summary TEXT,
                author TEXT,
                category TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.row_factory = sqlite3.Row
        return conn

    db._get_connection = _make_conn
    return db


def _insert_row(conn, title, created_at, tier=3, priority=0, source_id="test", source_name="test", source_type="hotlist"):
    conn.execute(
        """INSERT INTO news_items (title, source_id, source_name, source_type, tier, priority, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (title, source_id, source_name, source_type, tier, priority, created_at),
    )
    conn.commit()


class TestGetAllTimeRange:
    """Time-range filtering via start_time / end_time."""

    def test_no_filter_returns_all(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "A", "2026-06-27 08:00:00")
        _insert_row(conn, "B", "2026-06-27 09:00:00")
        _insert_row(conn, "C", "2026-06-27 10:00:00")

        rows = db.get_all("2026-06-27")
        assert len(rows) == 3

    def test_start_time_filters_after(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "A", "2026-06-27 08:00:00")
        _insert_row(conn, "B", "2026-06-27 09:00:00")
        _insert_row(conn, "C", "2026-06-27 10:00:00")

        rows = db.get_all("2026-06-27", start_time="2026-06-27 08:30:00")
        titles = [r["title"] for r in rows]
        assert titles == ["B", "C"]

    def test_end_time_filters_before(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "A", "2026-06-27 08:00:00")
        _insert_row(conn, "B", "2026-06-27 09:00:00")
        _insert_row(conn, "C", "2026-06-27 10:00:00")

        rows = db.get_all("2026-06-27", end_time="2026-06-27 09:30:00")
        titles = [r["title"] for r in rows]
        assert titles == ["A", "B"]

    def test_both_bounds(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "A", "2026-06-27 08:00:00")
        _insert_row(conn, "B", "2026-06-27 09:00:00")
        _insert_row(conn, "C", "2026-06-27 10:00:00")
        _insert_row(conn, "D", "2026-06-27 11:00:00")

        rows = db.get_all("2026-06-27", start_time="2026-06-27 08:30:00", end_time="2026-06-27 10:30:00")
        titles = [r["title"] for r in rows]
        assert titles == ["B", "C"]

    def test_ordering_is_preserved(self, db):
        conn = db._get_connection("2026-06-27")
        _insert_row(conn, "Low", "2026-06-27 09:00:00", tier=4, priority=0)
        _insert_row(conn, "High", "2026-06-27 09:00:01", tier=1, priority=10)
        _insert_row(conn, "Mid", "2026-06-27 09:00:02", tier=2, priority=5)

        rows = db.get_all("2026-06-27", start_time="2026-06-27 08:00:00")
        tiers = [r["tier"] for r in rows]
        assert tiers == [1, 2, 4]  # tier ASC, priority DESC
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_sqlite.py -v
```
预期：`TypeError: get_all() got an unexpected keyword argument 'start_time'`

- [ ] **Step 3: 实现 `get_all()` 时间过滤**

```python
# storage/sqlite.py — 替换现有 get_all 方法

def get_all(
    self,
    date: str,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
) -> List[sqlite3.Row]:
    """Return rows for *date*, optionally filtered by created_at range.

    Args:
        date: Date string (YYYY-MM-DD), used to locate the DB file.
        start_time: If set, only rows with ``created_at > this``
            (format: ``YYYY-MM-DD HH:MM:SS``).
        end_time: If set, only rows with ``created_at < this``
            (format: ``YYYY-MM-DD HH:MM:SS``).

    Returns:
        Matching rows ordered by tier ASC, priority DESC.
    """
    conn = self._get_connection(date)
    conn.row_factory = sqlite3.Row

    sql = "SELECT * FROM news_items WHERE 1=1"
    params: list = []

    if start_time:
        sql += " AND created_at > ?"
        params.append(start_time)
    if end_time:
        sql += " AND created_at < ?"
        params.append(end_time)

    sql += " ORDER BY tier ASC, priority DESC"
    return conn.execute(sql, params).fetchall()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
pytest tests/test_sqlite.py -v
```
预期：全部 PASS

- [ ] **Step 5: 验证现有调用方兼容**

```bash
pytest tests/test_frequency_words.py -v
```
预期：全部 PASS（`run_notifier` 中 `db.get_all(date)` 不传过滤参数行为不变）

- [ ] **Step 6: Commit**

```bash
git add storage/sqlite.py tests/test_sqlite.py
git commit -m "feat: add start_time/end_time filtering to sqlite get_all"
```

---

### Task 2: `news/notifier.py` — `run_notifier()` 支持时间过滤 + ISO 8601 转换

**Files:**
- Modify: `news/notifier.py:147-242`
- Create: `tests/test_notifier_run.py`

**Interfaces:**
- Consumes: `db.get_all(date, start_time=..., end_time=...)` from Task 1, `load_frequency_words`, `match_and_group`
- Produces: `run_notifier(config, dry_run=False, start_time=None, end_time=None) -> None`

- [ ] **Step 1: 创建测试文件并写失败测试**

```python
# tests/test_notifier_run.py
# coding=utf-8
"""Tests for run_notifier time-range filtering and ISO 8601 conversion."""

from unittest.mock import MagicMock, patch

import pytest

from news.notifier import _iso_to_db_format, run_notifier


class TestIsoToDbFormat:
    def test_converts_utc_z_suffix(self):
        assert _iso_to_db_format("2026-06-27T08:00:00Z") == "2026-06-27 08:00:00"

    def test_converts_with_offset(self):
        assert _iso_to_db_format("2026-06-27T08:00:00+00:00") == "2026-06-27 08:00:00"

    def test_converts_microseconds(self):
        assert _iso_to_db_format("2026-06-27T08:00:00.123456Z") == "2026-06-27 08:00:00"

    def test_empty_string_returns_none(self):
        assert _iso_to_db_format("") is None

    def test_none_returns_none(self):
        assert _iso_to_db_format(None) is None

    def test_invalid_returns_none(self):
        assert _iso_to_db_format("not-a-date") is None

    def test_unparseable_garbage_returns_none(self):
        assert _iso_to_db_format("abc123") is None


class TestRunNotifierTimeFiltering:
    """Integration-level: run_notifier passes start_time/end_time to db.get_all."""

    def test_passes_start_time_to_db(self):
        """start_time is converted and passed to db.get_all."""
        mock_db = MagicMock()
        mock_db.get_all.return_value = []

        with patch("news.notifier.Sqlite", return_value=mock_db), \
             patch("news.notifier.S3Client") as mock_s3_cls, \
             patch("news.notifier.load_frequency_words", return_value=([], [], [])), \
             patch("news.notifier.match_and_group", return_value={}), \
             patch("news.notifier.build_html_report", return_value="<html></html>"), \
             patch("news.notifier.save_html_report"), \
             patch("news.notifier.send_email"), \
             patch("news.notifier.format_date_today", return_value="2026-06-27"), \
             patch("news.notifier.format_time_now", return_value="08:30"), \
             patch("os.path.exists", return_value=False):

            mock_s3 = mock_s3_cls.init_by_config.return_value
            mock_s3.object_exists.return_value = False

            config = {
                "app": {"timezone": "Asia/Shanghai"},
                "storage": {"local": {"data_dir": "/tmp"}, "cloud": {}},
                "notification": {"email": {}},
            }

            run_notifier(config, start_time="2026-06-27T07:00:00Z")

            mock_db.get_all.assert_called_once_with(
                "2026-06-27", start_time="2026-06-27 07:00:00"
            )

    def test_passes_end_time_to_db(self):
        mock_db = MagicMock()
        mock_db.get_all.return_value = []

        with patch("news.notifier.Sqlite", return_value=mock_db), \
             patch("news.notifier.S3Client") as mock_s3_cls, \
             patch("news.notifier.load_frequency_words", return_value=([], [], [])), \
             patch("news.notifier.match_and_group", return_value={}), \
             patch("news.notifier.build_html_report", return_value="<html></html>"), \
             patch("news.notifier.save_html_report"), \
             patch("news.notifier.send_email"), \
             patch("news.notifier.format_date_today", return_value="2026-06-27"), \
             patch("news.notifier.format_time_now", return_value="08:30"), \
             patch("os.path.exists", return_value=False):

            mock_s3 = mock_s3_cls.init_by_config.return_value
            mock_s3.object_exists.return_value = False

            config = {
                "app": {"timezone": "Asia/Shanghai"},
                "storage": {"local": {"data_dir": "/tmp"}, "cloud": {}},
                "notification": {"email": {}},
            }

            run_notifier(config, end_time="2026-06-27T18:00:00Z")

            mock_db.get_all.assert_called_once_with(
                "2026-06-27", end_time="2026-06-27 18:00:00"
            )

    def test_no_time_params_calls_get_all_without_filters(self):
        """Backward-compatible: no start_time/end_time → get_all(date) with no filters."""
        mock_db = MagicMock()
        mock_db.get_all.return_value = []

        with patch("news.notifier.Sqlite", return_value=mock_db), \
             patch("news.notifier.S3Client") as mock_s3_cls, \
             patch("news.notifier.load_frequency_words", return_value=([], [], [])), \
             patch("news.notifier.match_and_group", return_value={}), \
             patch("news.notifier.build_html_report", return_value="<html></html>"), \
             patch("news.notifier.save_html_report"), \
             patch("news.notifier.send_email"), \
             patch("news.notifier.format_date_today", return_value="2026-06-27"), \
             patch("news.notifier.format_time_now", return_value="08:30"), \
             patch("os.path.exists", return_value=False):

            mock_s3 = mock_s3_cls.init_by_config.return_value
            mock_s3.object_exists.return_value = False

            config = {
                "app": {"timezone": "Asia/Shanghai"},
                "storage": {"local": {"data_dir": "/tmp"}, "cloud": {}},
                "notification": {"email": {}},
            }

            run_notifier(config)

            mock_db.get_all.assert_called_once_with("2026-06-27")

    def test_invalid_start_time_falls_back_to_none(self):
        """Invalid ISO 8601 → log warning, pass None as start_time."""
        mock_db = MagicMock()
        mock_db.get_all.return_value = []

        with patch("news.notifier.Sqlite", return_value=mock_db), \
             patch("news.notifier.S3Client") as mock_s3_cls, \
             patch("news.notifier.load_frequency_words", return_value=([], [], [])), \
             patch("news.notifier.match_and_group", return_value={}), \
             patch("news.notifier.build_html_report", return_value="<html></html>"), \
             patch("news.notifier.save_html_report"), \
             patch("news.notifier.send_email"), \
             patch("news.notifier.format_date_today", return_value="2026-06-27"), \
             patch("news.notifier.format_time_now", return_value="08:30"), \
             patch("os.path.exists", return_value=False):

            mock_s3 = mock_s3_cls.init_by_config.return_value
            mock_s3.object_exists.return_value = False

            config = {
                "app": {"timezone": "Asia/Shanghai"},
                "storage": {"local": {"data_dir": "/tmp"}, "cloud": {}},
                "notification": {"email": {}},
            }

            run_notifier(config, start_time="garbage")

            mock_db.get_all.assert_called_once_with("2026-06-27")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/test_notifier_run.py::TestIsoToDbFormat -v
```
预期：`ImportError: cannot import name '_iso_to_db_format'`

- [ ] **Step 3: 实现 `_iso_to_db_format()` 辅助函数**

在 `news/notifier.py` 顶部 import 区域添加 `from datetime import datetime`，然后在 `send_email` 函数之后、`run_notifier` 函数之前添加 `_iso_to_db_format`：

```python
def _iso_to_db_format(iso_str: str | None) -> str | None:
    """Convert ISO 8601 string to ``YYYY-MM-DD HH:MM:SS`` for SQLite comparison.

    Returns ``None`` if *iso_str* is empty or unparseable — callers should
    treat ``None`` as "no filter".
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        print(f"[Notifier] Failed to parse time: {iso_str!r}, ignoring filter")
        return None
```

需要在文件顶部添加 `from datetime import datetime`。

- [ ] **Step 4: 运行测试确认 `_iso_to_db_format` 通过**

```bash
pytest tests/test_notifier_run.py::TestIsoToDbFormat -v
```
预期：全部 PASS

- [ ] **Step 5: 修改 `run_notifier()` 函数签名和调用**

修改 `run_notifier()` 签名：

```python
def run_notifier(
    config: dict,
    dry_run: bool = False,
    start_time: str | None = None,
    end_time: str | None = None,
) -> None:
```

在 `rows = db.get_all(date)` 这一行（约第 194 行）替换为：

```python
    db_start = _iso_to_db_format(start_time)
    db_end = _iso_to_db_format(end_time)
    rows = db.get_all(date, start_time=db_start, end_time=db_end)
```

- [ ] **Step 6: 运行全部 notifier 测试**

```bash
pytest tests/test_notifier_run.py -v
```
预期：全部 PASS

- [ ] **Step 7: Commit**

```bash
git add news/notifier.py tests/test_notifier_run.py
git commit -m "feat: add start_time/end_time support to run_notifier with ISO 8601 conversion"
```

---

### Task 3: `cli/notify.py` — 添加 `--start-time` / `--end-time` 参数

**Files:**
- Modify: `cli/notify.py`

**Interfaces:**
- Consumes: `run_notifier(config, dry_run, start_time, end_time)` from Task 2
- Produces: `notify` CLI command with `--start-time` and `--end-time` options

- [ ] **Step 1: 修改 `cli/notify.py`**

```python
# cli/notify.py — 完整替换后的文件
# coding=utf-8
"""Notify command."""

import typer

from cli import app
from config.loader import load_config
from news.notifier import run_notifier


@app.command()
def notify(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Render and save the HTML report but do not send email",
    ),
    start_time: str | None = typer.Option(
        None, "--start-time",
        help="仅通知此时间（ISO 8601）之后创建的新闻",
    ),
    end_time: str | None = typer.Option(
        None, "--end-time",
        help="仅通知此时间（ISO 8601）之前创建的新闻",
    ),
):
    """Generate keyword-matched HTML report and send via email.

    The report is always saved to ``output/html/<date>/<time>.html``.
    Use ``--dry-run`` to preview the report without sending.
    Use ``--start-time`` / ``--end-time`` for incremental notifications.
    """
    config = load_config("config.yaml")
    run_notifier(
        config,
        dry_run=dry_run,
        start_time=start_time,
        end_time=end_time,
    )
```

- [ ] **Step 2: 验证 CLI 帮助输出**

```bash
python -m cli notify --help
```
预期：可以看到 `--start-time` 和 `--end-time` 选项

- [ ] **Step 3: Commit**

```bash
git add cli/notify.py
git commit -m "feat: add --start-time and --end-time options to cli notify"
```

---

### Task 4: `.github/workflows/notifier.yml` — 通过 `gh run list` 获取上次运行时间

**Files:**
- Modify: `.github/workflows/notifier.yml`

**Interfaces:**
- Consumes: `python -m cli notify --start-time` from Task 3
- Produces: CI workflow that auto-passes last successful run time

- [ ] **Step 1: 修改 workflow 文件**

在 "Install dependencies" 步骤之后、"Run notifier" 步骤之前插入新步骤，并修改 "Run notifier" 步骤：

```yaml
# .github/workflows/notifier.yml — 修改 steps 部分
      - name: Install dependencies
        run: uv sync --no-dev

      - name: Get last notify time
        id: last_run
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          SINCE=$(gh run list \
            --workflow notifier.yml \
            --branch master \
            --status success \
            --limit 1 \
            --json createdAt \
            --jq '.[0].createdAt // ""')
          echo "since=$SINCE" >> $GITHUB_OUTPUT

      - name: Run notifier
        env:
          CLOUD_S3_ENDPOINT_URL: ${{ secrets.CLOUD_S3_ENDPOINT_URL }}
          CLOUD_S3_BUCKET_NAME: ${{ secrets.CLOUD_S3_BUCKET_NAME }}
          CLOUD_S3_ACCESS_KEY_ID: ${{ secrets.CLOUD_S3_ACCESS_KEY_ID }}
          CLOUD_S3_SECRET_ACCESS_KEY: ${{ secrets.CLOUD_S3_SECRET_ACCESS_KEY }}
          CLOUD_S3_REGION: ${{ secrets.CLOUD_S3_REGION }}
          EMAIL_SMTP_SERVER: ${{ secrets.EMAIL_SMTP_SERVER }}
          EMAIL_SMTP_PORT: ${{ secrets.EMAIL_SMTP_PORT }}
          EMAIL_FROM_ADDR: ${{ secrets.EMAIL_FROM_ADDR }}
          EMAIL_TO_ADDR: ${{ secrets.EMAIL_TO_ADDR }}
          EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
        run: python -m cli notify --start-time "${{ steps.last_run.outputs.since }}"
```

- [ ] **Step 2: 检查 YAML 语法**

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/notifier.yml'))"
```
预期：无报错（如果 PyYAML 已安装）

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/notifier.yml
git commit -m "ci: auto-pass last successful run time to notifier via gh run list"
```

---

### Task 5: 集成验证

- [ ] **Step 1: 运行全部相关测试**

```bash
pytest tests/test_sqlite.py tests/test_notifier_run.py tests/test_notifier_template.py tests/test_frequency_words.py -v
```
预期：全部 PASS

- [ ] **Step 2: 本地 dry-run 验证（不传时间参数）**

```bash
python -m cli notify --dry-run
```
预期：正常执行，无报错（DB 不存在则输出 "No items to notify"）

- [ ] **Step 3: 本地 dry-run 验证（传入时间参数）**

```bash
python -m cli notify --dry-run --start-time "2026-06-27T00:00:00Z"
```
预期：正常执行，无报错

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test: integration verification for incremental notifier"
```
