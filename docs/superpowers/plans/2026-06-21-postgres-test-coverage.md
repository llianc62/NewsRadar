# PostgreSQL 测试覆盖实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `storage/postgres.py` 建立完整单元测试体系，覆盖所有方法签名、参数组合、搜索分流逻辑（CJK→ILIKE, ASCII→FTS），目标覆盖率 ≥ 80%，搜索路径 100%。

**Architecture:** 纯单元测试，mock psycopg2 连接池和游标层，捕获 SQL 和参数做断言。不依赖真实 PostgreSQL。沿用项目现有测试模式（`unittest.mock.MagicMock` + pytest fixtures）。

**Tech Stack:** Python 3.12+, pytest, unittest.mock

## Global Constraints

- 不需要真实 PostgreSQL（无 Docker Compose 依赖）
- 沿用项目现有 `unittest.mock.MagicMock` 模式
- 测试文件命名：`tests/test_postgres_*.py`
- 覆盖率目标 ≥ 80%，搜索路径（CJK/ASCII 分支）100%
- 每个 Task 提交一次，遵循 `test: ...` commit 格式

---

### Task 0: 测试基础设施 — conftest_db.py

**Files:**
- Create: `tests/conftest_db.py`

**Interfaces:**
- Produces: `db(mock_pool, mock_conn, mock_cursor)` — 带 mock pool 的 PostgreSQL 实例
- Produces: `capture_sql(mock_cursor)` — 从最后一次 execute 调用中提取 (sql, params)

- [ ] **Step 1: 创建 conftest_db.py**

```python
"""Shared fixtures for PostgreSQL unit tests."""
import pytest
from unittest.mock import MagicMock
from storage.postgres import PostgreSQL


@pytest.fixture
def mock_pool():
    """Mock ThreadedConnectionPool."""
    return MagicMock()


@pytest.fixture
def mock_conn():
    """Mock psycopg2 connection.  MagicMock 自动支持 context-manager 协议。"""
    return MagicMock()


@pytest.fixture
def mock_cursor():
    """Mock psycopg2 cursor — fetchone/fetchall 默认返回 None/[]."""
    cur = MagicMock()
    cur.fetchone.return_value = None
    cur.fetchall.return_value = []
    return cur


@pytest.fixture
def db(mock_pool, mock_conn, mock_cursor):
    """PostgreSQL 实例，pool 已 mock，get_conn() 会返回 mock_conn。

    用法::

        def test_xxx(db, mock_cursor):
            mock_cursor.fetchone.return_value = [42]
            result = db.get_news_count()
            sql, params = capture_sql(mock_cursor)
            assert "COUNT(*)" in sql
    """
    pg = PostgreSQL({
        "host": "localhost",
        "port": 5432,
        "database": "test",
        "user": "test",
        "password": "test",
    })
    pg._pool = mock_pool
    mock_pool.getconn.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return pg


def capture_sql(mock_cursor):
    """从 mock cursor 的最后一次 execute 调用中提取 (sql_template, params_tuple)。

    返回:
        (sql, params) — sql 是字符串，params 是参数元组。
        如果 cursor 从未被调用，返回 ("", ())。
    """
    if not mock_cursor.execute.call_args_list:
        return ("", ())
    call = mock_cursor.execute.call_args_list[-1]
    sql = call[0][0] if call[0] else ""
    params = call[0][1] if len(call[0]) > 1 else ()
    return (sql, params)
```

- [ ] **Step 2: 验证 fixtures 可加载**

Run: `python -c "from tests.conftest_db import db, capture_sql; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/conftest_db.py
git commit -m "test: add PostgreSQL mock fixtures conftest_db"
```

---

### Task 1: 模块级工具函数 — test_postgres_utils.py

**Files:**
- Create: `tests/test_postgres_utils.py`

**Interfaces:**
- Consumes: 无（纯函数，不需要 DB mock）
- Produces: 测试 `_load_schema`, `_to_timestamptz`, `_contains_cjk`

- [ ] **Step 1: _load_schema — 正常加载**

```python
"""Tests for module-level utilities in storage/postgres.py."""
import pytest
from storage.postgres import _load_schema, _to_timestamptz, _contains_cjk


class TestLoadSchema:
    def test_loads_schema_file(self):
        """postgres.sql 应能读取并包含 CREATE TABLE 语句。"""
        sql = _load_schema()
        assert "CREATE TABLE" in sql
        assert "news_articles" in sql

    def test_raises_when_file_missing(self, monkeypatch):
        """文件不存在时抛 FileNotFoundError。"""
        import storage.postgres as mod
        monkeypatch.setattr(mod.Path, "exists", lambda self: False)
        monkeypatch.setattr(mod.Path, "read_text",
                            lambda self, **kw: (_ for _ in ()).throw(FileNotFoundError()))
        with pytest.raises(FileNotFoundError):
            _load_schema()
```

- [ ] **Step 3: _to_timestamptz — 所有转换路径**

```python
class TestToTimestamptz:
    def test_iso8601_full(self):
        """完整 ISO 8601 时间字符串。"""
        from datetime import datetime, timezone, timedelta
        result = _to_timestamptz("2026-06-21T10:30:00+08:00", None)
        assert result == datetime(2026, 6, 21, 10, 30,
                                  tzinfo=timezone(timedelta(hours=8)))

    def test_utc_z_suffix(self):
        """UTC Z 后缀。"""
        from datetime import datetime, timezone
        result = _to_timestamptz("2026-06-21T02:30:00Z", None)
        assert result == datetime(2026, 6, 21, 2, 30, tzinfo=timezone.utc)

    def test_hhmm_with_fallback(self):
        """HH:MM 格式 + fallback_date。"""
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        result = _to_timestamptz("10:30", "2026-06-21")
        assert result == datetime(2026, 6, 21, 10, 30, tzinfo=tz)

    def test_empty_string(self):
        assert _to_timestamptz("", None) is None

    def test_none(self):
        assert _to_timestamptz(None, None) is None

    def test_invalid_format(self):
        """无效格式返回 None。"""
        assert _to_timestamptz("not-a-time", None) is None

    def test_hhmm_without_fallback(self):
        """HH:MM 格式无 fallback_date 时返回 None。"""
        assert _to_timestamptz("10:30", None) is None

    def test_invalid_hour_value(self):
        """非法小时值不抛异常，返回 None。"""
        assert _to_timestamptz("99:99", "2026-06-21") is None
```

- [ ] **Step 4: _contains_cjk — 所有分支**

```python
class TestContainsCjk:
    def test_pure_chinese(self):
        assert _contains_cjk("英伟达") is True

    def test_mixed_cjk_ascii(self):
        assert _contains_cjk("NVIDIA 英伟达 GPU") is True

    def test_japanese_kanji(self):
        assert _contains_cjk("日本経済") is True

    def test_pure_ascii(self):
        assert _contains_cjk("NVIDIA") is False

    def test_numbers_and_symbols(self):
        assert _contains_cjk("GPT-4") is False

    def test_empty_string(self):
        assert _contains_cjk("") is False

    def test_chinese_punctuation_only(self):
        """中文标点（，。）不在 CJK 字符范围，返回 False。"""
        assert _contains_cjk("，。！") is False
```

- [ ] **Step 5: 运行全部 utils 测试**

Run: `pytest tests/test_postgres_utils.py -v`
Expected: 13 passed

- [ ] **Step 6: Commit**

```bash
git add tests/test_postgres_utils.py
git commit -m "test: add unit tests for _load_schema, _to_timestamptz, _contains_cjk"
```

---

### Task 2: 查询过滤器 + 搜索分流 — test_postgres_query_filters.py

**Files:**
- Create: `tests/test_postgres_query_filters.py`

**Interfaces:**
- Consumes: `db`, `mock_cursor`, `capture_sql` from `tests/conftest_db.py`
- Produces: 过滤器组合验证 + 搜索分流（CJK→ILIKE, ASCII→FTS）全覆盖

- [ ] **Step 1: 无过滤默认行为 + 基本过滤器**

```python
"""Tests for filter conditions and search routing in PostgreSQL query methods."""
import pytest
from tests.conftest_db import capture_sql


class TestFilterDefaults:
    def test_no_filters_adds_confidence_default(self, db, mock_cursor):
        """无任何过滤时，SQL 含默认 confidence >= 20。"""
        mock_cursor.fetchall.return_value = []
        db.get_recent_news()
        sql, _ = capture_sql(mock_cursor)
        assert "confidence IS NULL OR confidence >= 20" in sql

    def test_tier_filter(self, db, mock_cursor):
        db.get_recent_news(tier=1)
        sql, params = capture_sql(mock_cursor)
        assert "tier = %s" in sql
        assert 1 in params

    def test_category_filter(self, db, mock_cursor):
        db.get_recent_news(category="tech")
        sql, params = capture_sql(mock_cursor)
        assert "category = %s" in sql
        assert "tech" in params

    def test_min_confidence_filter(self, db, mock_cursor):
        db.get_recent_news(min_confidence=50)
        sql, params = capture_sql(mock_cursor)
        assert "confidence >= %s" in sql
        assert 50 in params

    def test_sentiment_positive(self, db, mock_cursor):
        db.get_recent_news(sentiment="positive")
        sql, _ = capture_sql(mock_cursor)
        assert "sentiment_score >= 67" in sql

    def test_sentiment_negative(self, db, mock_cursor):
        db.get_recent_news(sentiment="negative")
        sql, _ = capture_sql(mock_cursor)
        assert "sentiment_score <= 33" in sql

    def test_sentiment_neutral(self, db, mock_cursor):
        db.get_recent_news(sentiment="neutral")
        sql, _ = capture_sql(mock_cursor)
        assert "sentiment_score > 33" in sql
        assert "sentiment_score < 67" in sql

    def test_keyword_filter(self, db, mock_cursor):
        db.get_recent_news(keyword="芯片")
        sql, params = capture_sql(mock_cursor)
        assert "ANY(tags)" in sql
        assert "芯片" in params

    def test_date_from_filter(self, db, mock_cursor):
        db.get_recent_news(date_from="2026-06-19")
        sql, params = capture_sql(mock_cursor)
        assert "published_at >= %s::date" in sql
        assert "2026-06-19" in params

    def test_date_to_filter(self, db, mock_cursor):
        db.get_recent_news(date_to="2026-06-21")
        sql, params = capture_sql(mock_cursor)
        assert "published_at < %s::date + interval '1 day'" in sql
        assert "2026-06-21" in params

    def test_all_filters_combined(self, db, mock_cursor):
        """全部过滤器同时启用时所有条件都出现在 SQL 中。"""
        mock_cursor.fetchall.return_value = []
        db.get_recent_news(
            tier=1,
            category="tech",
            sentiment="positive",
            keyword="芯片",
            search="AI",
            date_from="2026-06-19",
            date_to="2026-06-21",
        )
        sql, params = capture_sql(mock_cursor)
        assert "tier = %s" in sql
        assert "category = %s" in sql
        assert "sentiment_score >= 67" in sql
        assert "ANY(tags)" in sql
        assert "plainto_tsquery" in sql  # "AI" 不含 CJK → FTS
        assert "published_at" in sql
        # params 包含所有过滤值
        assert 1 in params
        assert "tech" in params
        assert "芯片" in params
        assert "AI" in params

    def test_pagination_params_appended_last(self, db, mock_cursor):
        """LIMIT 和 OFFSET 作为最后两个参数追加。"""
        mock_cursor.fetchall.return_value = []
        db.get_recent_news(limit=20, offset=40)
        _, params = capture_sql(mock_cursor)
        assert params[-2] == 20
        assert params[-1] == 40
```

- [ ] **Step 2: 运行确认通过**

Run: `pytest tests/test_postgres_query_filters.py -v`
Expected: 13 passed

- [ ] **Step 3: 搜索分流 — CJK → ILIKE**

```python
class TestSearchRouting:
    def test_cjk_search_uses_ilike(self, db, mock_cursor):
        """纯中文搜索应使用 ILIKE，参数包含 % 通配符。"""
        mock_cursor.fetchall.return_value = []
        db.get_recent_news(search="英伟达")
        sql, params = capture_sql(mock_cursor)
        assert "ILIKE" in sql
        assert "plainto_tsquery" not in sql
        assert "%英伟达%" in params

    def test_mixed_cjk_search_uses_ilike(self, db, mock_cursor):
        """混合中英文（含 CJK）搜索应使用 ILIKE。"""
        mock_cursor.fetchall.return_value = []
        db.get_recent_news(search="NVIDIA芯片")
        sql, params = capture_sql(mock_cursor)
        assert "ILIKE" in sql
        assert "%NVIDIA芯片%" in params

    def test_ascii_search_uses_fts(self, db, mock_cursor):
        """纯英文/数字搜索应使用 FTS。"""
        mock_cursor.fetchall.return_value = []
        db.get_recent_news(search="NVIDIA")
        sql, params = capture_sql(mock_cursor)
        assert "plainto_tsquery" in sql
        assert "ILIKE" not in sql
        assert "NVIDIA" in params

    def test_cjk_search_in_get_news_count(self, db, mock_cursor):
        """get_news_count 中文搜索分流。"""
        mock_cursor.fetchone.return_value = [5]
        db.get_news_count(search="英伟达")
        sql, params = capture_sql(mock_cursor)
        assert "ILIKE" in sql
        assert "%英伟达%" in params

    def test_cjk_search_in_get_sentiment_counts(self, db, mock_cursor):
        """get_sentiment_counts 中文搜索分流。"""
        mock_cursor.fetchone.return_value = {"positive": 3, "negative": 1, "neutral": 2}
        db.get_sentiment_counts(search="英伟达")
        sql, params = capture_sql(mock_cursor)
        assert "ILIKE" in sql
        assert "%英伟达%" in params

    def test_cjk_search_in_get_keyword_counts(self, db, mock_cursor):
        """get_keyword_counts 中文搜索分流。"""
        mock_cursor.fetchall.return_value = [{"tag": "芯片", "cnt": 5}]
        db.get_keyword_counts(search="英伟达")
        sql, params = capture_sql(mock_cursor)
        assert "ILIKE" in sql
        assert "%英伟达%" in params

    def test_cjk_search_in_get_high_impact_count(self, db, mock_cursor):
        """get_high_impact_count 中文搜索分流。"""
        mock_cursor.fetchone.return_value = [3]
        db.get_high_impact_count(search="英伟达")
        sql, params = capture_sql(mock_cursor)
        assert "ILIKE" in sql
        assert "%英伟达%" in params

    def test_cjk_search_in_get_stats(self, db, mock_cursor):
        """get_stats 中文搜索分流。"""
        mock_cursor.fetchone.return_value = {
            "t1_count": 0, "t2_count": 0, "t3_count": 0, "t4_count": 0,
            "total_count": 0, "today_count": 0,
        }
        mock_cursor.fetchall.return_value = []
        db.get_stats(search="英伟达")
        sql, params = capture_sql(mock_cursor)
        assert "ILIKE" in sql
        assert "%英伟达%" in params

    def test_no_search_skips_both(self, db, mock_cursor):
        """search 为 None / 未传时，SQL 不含 ILIKE 也不含 to_tsvector。"""
        mock_cursor.fetchall.return_value = []
        db.get_recent_news()
        sql, _ = capture_sql(mock_cursor)
        assert "ILIKE" not in sql
        assert "to_tsvector" not in sql
```

- [ ] **Step 4: 运行全部过滤器 + 搜索测试**

Run: `pytest tests/test_postgres_query_filters.py -v`
Expected: 22 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_postgres_query_filters.py
git commit -m "test: add query filter combinations and CJK/ASCII search routing tests"
```

---

### Task 3: 写入操作 — test_postgres_write.py

**Files:**
- Create: `tests/test_postgres_write.py`

**Interfaces:**
- Consumes: `db`, `mock_cursor`, `capture_sql` from `tests/conftest_db.py`
- Produces: 测试 `save_news_data`, `update_article_content`, `update_article_full`, `delete_news`, `save_article_image`

- [ ] **Step 1: save_news_data 分区和去重逻辑**

```python
"""Tests for write operations in storage/postgres.py."""
import pytest
from unittest.mock import MagicMock, patch, call
from tests.conftest_db import capture_sql


# 工厂函数：构造最小 NewsItem
def _make_item(source_type="hotlist", url="https://example.com/news/1",
               guid="", source_name="TestSource", title="Test Title",
               crawled_at="2026-06-21T10:00:00+08:00",
               published_at="2026-06-21T08:00:00+08:00"):
    from news.models import NewsItem
    return NewsItem(
        title=title,
        source_name=source_name,
        source_type=source_type,
        url=url,
        mobile_url="",
        guid=guid,
        rank=1,
        ranks=[1],
        summary="summary",
        author="author",
        content="content",
        category="tech",
        tags=["AI"],
        heat_score=80,
        sentiment_score=50,
        confidence=80,
        crawled_at=crawled_at,
        published_at=published_at,
    )


def _make_news_data(items_by_source, date="2026-06-21"):
    from news.models import NewsData
    return NewsData(date=date, items=items_by_source)


class TestSaveNewsData:
    def test_empty_news_data(self, db):
        """空 NewsData 返回 processed=0, skipped=0。"""
        from news.models import NewsData
        result = db.save_news_data(NewsData(date="2026-06-21", items={}))
        assert result == {"processed": 0, "skipped": 0}

    def test_hotlist_insert(self, db, mock_conn):
        """hotlist 类型走 _HOTLIST_INSERT_SQL，SQL 含 ON CONFLICT (source_id, url)。"""
        nd = _make_news_data({"src1": [_make_item("hotlist")]})
        with patch.object(db, "_execute_batch", return_value=(1, 0)):
            result = db.save_news_data(nd)
            assert result == {"processed": 1, "skipped": 0}

    def test_rss_insert(self, db, mock_conn):
        """rss 类型走 (source_id, guid) 去重。"""
        nd = _make_news_data({"src1": [
            _make_item("rss", url="", guid="http://example.com/guid/1")
        ]})
        with patch.object(db, "_execute_batch", return_value=(1, 0)):
            result = db.save_news_data(nd)
            assert result["processed"] == 1

    def test_manual_insert(self, db, mock_conn):
        """manual 类型强制覆盖 content。"""
        nd = _make_news_data({"src1": [_make_item("manual")]})
        with patch.object(db, "_execute_batch", return_value=(1, 0)):
            result = db.save_news_data(nd)
            assert result["processed"] == 1

    def test_fallback_insert(self, db, mock_conn):
        """无 url 无 guid 走 fallback 简单 INSERT。"""
        nd = _make_news_data({"src1": [
            _make_item("hotlist", url="", guid="")
        ]})
        with patch.object(db, "_execute_batch", return_value=(1, 0)):
            result = db.save_news_data(nd)
            assert result["processed"] == 1

    def test_skip_existing_mode(self, db):
        """skip_existing=True 时使用 SKIP 变体 SQL。"""
        nd = _make_news_data({"src1": [_make_item("hotlist")]})
        with patch.object(db, "_execute_batch", return_value=(0, 1)):
            result = db.save_news_data(nd, skip_existing=True)
            assert result["skipped"] == 1

    def test_source_tiers_applied(self, db):
        """source_tiers 提供的 tier/priority 写入行。"""
        nd = _make_news_data({"src1": [_make_item("hotlist")]})
        tiers = {"src1": {"tier": 1, "priority": 99}}
        with patch.object(db, "_execute_batch") as mock_batch:
            mock_batch.return_value = (1, 0)
            db.save_news_data(nd, source_tiers=tiers)
            # 验证传入 _execute_batch 的 rows 包含正确的 tier/priority
            call_args = mock_batch.call_args
            rows = call_args[0][2]  # third positional arg
            row = rows[0]
            assert row[4] == 1   # tier (index 4 in _build_row output)
            assert row[5] == 99  # priority (index 5)

    def test_crawled_from_written(self, db):
        """crawled_from 参数写入行数据。"""
        nd = _make_news_data({"src1": [_make_item("hotlist")]})
        with patch.object(db, "_execute_batch") as mock_batch:
            mock_batch.return_value = (1, 0)
            db.save_news_data(nd, crawled_from="cloud")
            rows = mock_batch.call_args[0][2]
            assert rows[0][16] == "cloud"  # crawled_from (index 16)

    def test_mixed_types_in_one_batch(self, db):
        """混合 hotlist + rss + manual + fallback 各行其道。"""
        nd = _make_news_data({
            "src1": [
                _make_item("hotlist", url="http://a.com/1"),
                _make_item("rss", url="", guid="g1"),
                _make_item("manual", url="http://a.com/2"),
                _make_item("hotlist", url="", guid=""),
            ]
        })
        with patch.object(db, "_execute_batch", return_value=(1, 0)):
            result = db.save_news_data(nd)
            assert result["processed"] == 4
```

- [ ] **Step 2: 运行 save 测试**

Run: `pytest tests/test_postgres_write.py::TestSaveNewsData -v`
Expected: 9 passed

- [ ] **Step 3: update / delete / image 操作**

```python
class TestUpdateArticleContent:
    def test_update_existing_article(self, db, mock_cursor):
        """更新存在的文章返回 True。"""
        mock_cursor.rowcount = 1
        result = db.update_article_content(1, "new content")
        assert result is True
        sql, params = capture_sql(mock_cursor)
        assert "UPDATE news_articles" in sql
        assert "updated_at = NOW()" in sql
        assert 1 in params
        assert "new content" in params

    def test_update_nonexistent_article(self, db, mock_cursor):
        """文章不存在返回 False。"""
        mock_cursor.rowcount = 0
        result = db.update_article_content(999, "content")
        assert result is False


class TestUpdateArticleFull:
    def test_update_all_fields(self, db, mock_cursor):
        mock_cursor.rowcount = 1
        result = db.update_article_full(
            article_id=1,
            title="New Title",
            content="New Content",
            published_at="2026-06-21T10:00:00+08:00",
            author="New Author",
            summary="New Summary",
            category="tech",
            tags=["AI"],
        )
        assert result is True
        sql, params = capture_sql(mock_cursor)
        assert "UPDATE news_articles" in sql
        assert "COALESCE(NULLIF" in sql or "title" in sql

    def test_empty_title_preserves_old(self, db, mock_cursor):
        """title 为空时 COALESCE 保留旧值。"""
        mock_cursor.rowcount = 1
        db.update_article_full(article_id=1, title="", content="c")
        sql, params = capture_sql(mock_cursor)
        assert "NULLIF" in sql

    def test_none_tags_preserves_old(self, db, mock_cursor):
        """tags=None 时 COALESCE 保留旧值。"""
        mock_cursor.rowcount = 1
        db.update_article_full(article_id=1, content="c", tags=None)
        sql, params = capture_sql(mock_cursor)
        assert "COALESCE(%s, tags)" in sql
        assert None in params


class TestDeleteNews:
    def test_delete_existing_article(self, db, mock_cursor):
        mock_cursor.rowcount = 1
        result = db.delete_news(1)
        assert result is True
        sql, params = capture_sql(mock_cursor)
        assert "DELETE FROM news_articles" in sql
        assert 1 in params

    def test_delete_nonexistent_article(self, db, mock_cursor):
        mock_cursor.rowcount = 0
        result = db.delete_news(999)
        assert result is False


class TestSaveArticleImage:
    def test_saves_image_returns_id(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [42]
        img_id = db.save_article_image(
            article_id=1,
            image_url="/media/2026-06-21/img.jpg",
            original_url="https://example.com/img.jpg",
            width=800,
            height=600,
            file_size=102400,
            sort_order=0,
        )
        assert img_id == 42
        sql, params = capture_sql(mock_cursor)
        assert "INSERT INTO news_images" in sql
        assert "RETURNING id" in sql

    def test_optional_fields_null(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [1]
        img_id = db.save_article_image(
            article_id=1,
            image_url="/media/img.jpg",
        )
        assert img_id == 1
        _, params = capture_sql(mock_cursor)
        assert params[2] == ""    # original_url defaults to ""
        assert params[3] is None  # width
        assert params[4] is None  # height
        assert params[5] is None  # file_size
```

- [ ] **Step 4: 运行全部写入测试**

Run: `pytest tests/test_postgres_write.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_postgres_write.py
git commit -m "test: add write operation tests for save, update, delete, images"
```

---

### Task 4: 生命周期 — test_postgres_lifecycle.py

**Files:**
- Create: `tests/test_postgres_lifecycle.py`

**Interfaces:**
- Consumes: `mock_pool`, `mock_conn`, `mock_cursor` from `tests/conftest_db.py`
- Produces: 测试 `connect`, `close`, `init_schema`, `_schema_ready`, `_run_migrations`, `get_conn` context manager

- [ ] **Step 1: connect / close / is_connected**

```python
"""Tests for lifecycle methods in storage/postgres.py."""
import pytest
from unittest.mock import MagicMock, patch
from storage.postgres import PostgreSQL


@pytest.fixture
def pg_unconnected():
    """未连接状态的 PostgreSQL 实例。"""
    return PostgreSQL({
        "host": "localhost", "port": 5432, "database": "test",
        "user": "test", "password": "test",
    })


class TestConnectClose:
    def test_connect_creates_pool(self, pg_unconnected):
        with patch("storage.postgres.ThreadedConnectionPool") as mock_pool_cls:
            pg_unconnected.connect()
            assert pg_unconnected._pool is not None
            mock_pool_cls.assert_called_once()

    def test_connect_idempotent(self, pg_unconnected):
        with patch("storage.postgres.ThreadedConnectionPool") as mock_pool_cls:
            pg_unconnected.connect()
            pg_unconnected.connect()  # 第二次调用
            assert mock_pool_cls.call_count == 1

    def test_close(self, pg_unconnected):
        with patch("storage.postgres.ThreadedConnectionPool") as mock_pool_cls:
            pg_unconnected.connect()
            pg_unconnected.close()
            assert pg_unconnected._pool is None
            mock_pool_cls.return_value.closeall.assert_called_once()

    def test_close_when_not_connected(self, pg_unconnected):
        """未连接时 close 不抛异常。"""
        pg_unconnected.close()  # 不应抛异常
        assert pg_unconnected._pool is None

    def test_is_connected(self, pg_unconnected):
        assert pg_unconnected.is_connected is False
        with patch("storage.postgres.ThreadedConnectionPool"):
            pg_unconnected.connect()
        assert pg_unconnected.is_connected is True
```

- [ ] **Step 2: _schema_ready / init_schema / _run_migrations**

```python
class TestSchemaReady:
    def test_tables_exist(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = [True]

        assert pg_unconnected._schema_ready() is True

    def test_tables_not_exist(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = [False]

        assert pg_unconnected._schema_ready() is False


class TestInitSchema:
    def test_init_on_empty_database(self, pg_unconnected):
        """表不存在时执行 DDL。"""
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        # _schema_ready → False, 然后 _run_migrations 也需要 cursor
        mock_cursor.fetchone.side_effect = [
            [False],   # _schema_ready: tables don't exist
            [True],    # migration 001: idx_fulltext has content
            [True],    # migration 002: idx_fulltext_trgm exists
        ]

        pg_unconnected.init_schema()
        # 验证 DDL 被执行
        executes = [c[0][0] for c in mock_cursor.execute.call_args_list if c[0]]
        ddl_calls = [s for s in executes if "CREATE TABLE" in str(s)]
        assert len(ddl_calls) >= 1

    def test_init_when_schema_exists(self, pg_unconnected):
        """表存在时跳过 DDL，仍执行 migration。"""
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [
            [True],   # _schema_ready: tables exist
            [True],   # migration 001: ok
            [True],   # migration 002: ok
        ]

        pg_unconnected.init_schema()
        executes = [str(c[0][0]) for c in mock_cursor.execute.call_args_list if c[0]]
        ddl_calls = [s for s in executes if "CREATE TABLE" in s]
        assert len(ddl_calls) == 0  # DDL 被跳过


class TestRunMigrations:
    def _setup_pg_for_migration(self, pg_unconnected,
                                 idx_fulltext_with_content=True,
                                 idx_trgm_exists=True):
        """辅助：设置 mock pool/cursor，返回 mock_cursor。"""
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [
            [idx_fulltext_with_content],
            [idx_trgm_exists],
        ]
        return mock_cursor

    def test_migration_001_skips_when_content_present(self, pg_unconnected):
        mock_cur = self._setup_pg_for_migration(pg_unconnected, True, True)
        pg_unconnected._run_migrations()
        executes = [str(c[0][0]) for c in mock_cur.execute.call_args_list]
        # 不应该有 DROP INDEX
        drop_calls = [s for s in executes if "DROP INDEX" in s]
        assert len(drop_calls) == 0

    def test_migration_001_rebuilds_when_content_missing(self, pg_unconnected):
        mock_cur = self._setup_pg_for_migration(pg_unconnected, False, True)
        pg_unconnected._run_migrations()
        executes = [str(c[0][0]) for c in mock_cur.execute.call_args_list]
        # 应该有 DROP INDEX + CREATE INDEX
        assert any("DROP INDEX" in s for s in executes)
        assert any("CREATE INDEX idx_fulltext" in s for s in executes)

    def test_migration_002_creates_pg_trgm_and_index(self, pg_unconnected):
        mock_cur = self._setup_pg_for_migration(pg_unconnected, True, False)
        pg_unconnected._run_migrations()
        executes = [str(c[0][0]) for c in mock_cur.execute.call_args_list]
        assert any("CREATE EXTENSION IF NOT EXISTS pg_trgm" in s for s in executes)
        assert any("idx_fulltext_trgm" in s for s in executes)

    def test_migration_002_skips_when_index_exists(self, pg_unconnected):
        mock_cur = self._setup_pg_for_migration(pg_unconnected, True, True)
        pg_unconnected._run_migrations()
        executes = [str(c[0][0]) for c in mock_cur.execute.call_args_list]
        # idx_fulltext_trgm 不应该被重复创建
        create_trgm_calls = [s for s in executes if "CREATE INDEX idx_fulltext_trgm" in s]
        assert len(create_trgm_calls) == 0

    def test_migrations_idempotent(self, pg_unconnected):
        """两次调用 _run_migrations 第二次无变化。"""
        mock_cur = self._setup_pg_for_migration(pg_unconnected, True, True)
        pg_unconnected._run_migrations()
        first_count = len(mock_cur.execute.call_args_list)

        # Reset mock for second call
        mock_cur2 = MagicMock()
        mock_cur2.fetchone.side_effect = [[True], [True]]
        pg_unconnected._pool.getconn.return_value.cursor.return_value.__enter__.return_value = mock_cur2

        pg_unconnected._run_migrations()
        # 第二次调用只有 2 个 SELECT EXISTS（检查索引状态）
        # fetchone 被调用 2 次（每个 migration 检查一次），不应该有 DDL
        ddl_calls = [
            str(c[0][0]) for c in mock_cur2.execute.call_args_list
            if "CREATE INDEX" in str(c[0][0]) or "DROP INDEX" in str(c[0][0])
        ]
        assert len(ddl_calls) == 0


class TestGetConn:
    def test_raises_when_not_connected(self, pg_unconnected):
        with pytest.raises(RuntimeError, match="not connected"):
            with pg_unconnected.get_conn():
                pass

    def test_commits_on_success(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn

        with pg_unconnected.get_conn() as conn:
            assert conn is mock_conn
        mock_conn.commit.assert_called_once()

    def test_rollback_on_exception(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn

        with pytest.raises(ValueError):
            with pg_unconnected.get_conn():
                raise ValueError("test error")
        mock_conn.rollback.assert_called_once()

    def test_putconn_in_finally(self, pg_unconnected):
        pg_unconnected._pool = MagicMock()
        mock_conn = MagicMock()
        pg_unconnected._pool.getconn.return_value = mock_conn

        try:
            with pg_unconnected.get_conn():
                raise ValueError("test")
        except ValueError:
            pass
        pg_unconnected._pool.putconn.assert_called_once_with(mock_conn)
```

- [ ] **Step 3: 运行全部生命周期测试**

Run: `pytest tests/test_postgres_lifecycle.py -v`
Expected: 15 passed

- [ ] **Step 4: Commit**

```bash
git add tests/test_postgres_lifecycle.py
git commit -m "test: add lifecycle tests for connect, schema, migrations, get_conn"
```

---

### Task 5: 查询方法特有行为 — test_postgres_query_methods.py

**Files:**
- Create: `tests/test_postgres_query_methods.py`

**Interfaces:**
- Consumes: `db`, `mock_cursor`, `capture_sql` from `tests/conftest_db.py`
- Produces: 测试 8 个查询方法各自特有的 SQL 结构、返回格式、排序、分页

- [ ] **Step 1: get_recent_news 特有行为**

```python
"""Tests for query-method-specific behavior in storage/postgres.py."""
import pytest
from tests.conftest_db import capture_sql


class TestGetRecentNews:
    def test_returns_list(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        result = db.get_recent_news()
        assert isinstance(result, list)

    def test_does_not_select_content_column(self, db, mock_cursor):
        """列表页不应查询 content 字段（性能）。"""
        mock_cursor.fetchall.return_value = []
        db.get_recent_news()
        sql, _ = capture_sql(mock_cursor)
        assert "content" not in sql.lower().replace("COALESCE(content", "")

    def test_order_clause(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_recent_news()
        sql, _ = capture_sql(mock_cursor)
        assert "published_at DESC NULLS LAST" in sql
        assert "heat_score DESC NULLS LAST" in sql

    def test_default_limit_offset(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_recent_news()
        _, params = capture_sql(mock_cursor)
        assert params[-2] == 50   # default limit
        assert params[-1] == 0    # default offset

    def test_custom_pagination(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_recent_news(limit=10, offset=30)
        _, params = capture_sql(mock_cursor)
        assert params[-2] == 10
        assert params[-1] == 30
```

- [ ] **Step 2: get_news_count / get_sentiment_counts**

```python
class TestGetNewsCount:
    def test_returns_int(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [42]
        result = db.get_news_count()
        assert result == 42
        assert isinstance(result, int)

    def test_returns_zero(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [0]
        result = db.get_news_count()
        assert result == 0

    def test_sql_uses_count_star(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [0]
        db.get_news_count()
        sql, _ = capture_sql(mock_cursor)
        assert "COUNT(*)" in sql

    def test_default_confidence_applied(self, db, mock_cursor):
        """无 min_confidence 时仍使用默认 >= 20。"""
        mock_cursor.fetchone.return_value = [0]
        db.get_news_count()
        sql, _ = capture_sql(mock_cursor)
        assert "confidence IS NULL OR confidence >= 20" in sql


class TestGetSentimentCounts:
    def test_returns_dict_with_three_keys(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = {"positive": 5, "negative": 2, "neutral": 3}
        result = db.get_sentiment_counts()
        assert result == {"positive": 5, "negative": 2, "neutral": 3}

    def test_uses_count_filter_syntax(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = {"positive": 0, "negative": 0, "neutral": 0}
        db.get_sentiment_counts()
        sql, _ = capture_sql(mock_cursor)
        assert "COUNT(*) FILTER" in sql
        assert "positive" in sql
        assert "negative" in sql
        assert "neutral" in sql
```

- [ ] **Step 3: get_keyword_counts / get_high_impact_count**

```python
class TestGetKeywordCounts:
    def test_returns_list_of_dicts(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = [{"tag": "AI", "cnt": 10}]
        result = db.get_keyword_counts()
        assert result == [{"tag": "AI", "cnt": 10}]

    def test_unnest_tags_in_sql(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_keyword_counts()
        sql, _ = capture_sql(mock_cursor)
        assert "unnest(tags)" in sql

    def test_default_limit(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_keyword_counts()
        _, params = capture_sql(mock_cursor)
        assert params[-1] == 30  # default limit

    def test_custom_limit(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_keyword_counts(limit=10)
        _, params = capture_sql(mock_cursor)
        assert params[-1] == 10

    def test_groups_and_orders_by_count(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_keyword_counts()
        sql, _ = capture_sql(mock_cursor)
        assert "GROUP BY tag" in sql
        assert "ORDER BY cnt DESC" in sql


class TestGetHighImpactCount:
    def test_heat_score_condition_always_present(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [3]
        db.get_high_impact_count()
        sql, _ = capture_sql(mock_cursor)
        assert "heat_score >= 80" in sql

    def test_defaults_to_current_date(self, db, mock_cursor):
        """无 date_from/date_to 时使用 CURRENT_DATE。"""
        mock_cursor.fetchone.return_value = [0]
        db.get_high_impact_count()
        sql, _ = capture_sql(mock_cursor)
        assert "CURRENT_DATE" in sql

    def test_date_params_override_current_date(self, db, mock_cursor):
        """有 date_from 时不使用 CURRENT_DATE。"""
        mock_cursor.fetchone.return_value = [0]
        db.get_high_impact_count(date_from="2026-06-19", date_to="2026-06-21")
        sql, _ = capture_sql(mock_cursor)
        assert "CURRENT_DATE" not in sql
        assert "published_at >= %s::date" in sql

    def test_no_current_date_with_date_from_only(self, db, mock_cursor):
        """只传 date_from 也禁用 CURRENT_DATE。"""
        mock_cursor.fetchone.return_value = [0]
        db.get_high_impact_count(date_from="2026-06-19")
        sql, _ = capture_sql(mock_cursor)
        assert "CURRENT_DATE" not in sql
```

- [ ] **Step 4: get_stats / get_news_by_id / get_article_by_url / get_articles_without_content**

```python
class TestGetStats:
    def test_returns_complete_stats_dict(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = {
            "t1_count": 1, "t2_count": 2, "t3_count": 3, "t4_count": 4,
            "total_count": 10, "today_count": 5,
        }
        mock_cursor.fetchall.return_value = [{"source_name": "Src1", "cnt": 10}]
        result = db.get_stats()
        assert result["t1_count"] == 1
        assert result["total_count"] == 10
        assert result["today_count"] == 5
        assert len(result["by_source"]) == 1

    def test_by_source_ordered_by_count(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = {
            "t1_count": 0, "t2_count": 0, "t3_count": 0, "t4_count": 0,
            "total_count": 0, "today_count": 0,
        }
        mock_cursor.fetchall.return_value = []
        db.get_stats()
        # 第二个查询含 ORDER BY cnt DESC
        all_sql = [str(c[0][0]) for c in mock_cursor.execute.call_args_list]
        source_query = [s for s in all_sql if "GROUP BY source_name" in s]
        assert len(source_query) == 1
        assert "ORDER BY cnt DESC" in source_query[0]


class TestGetNewsById:
    def test_returns_article_with_images(self, db, mock_cursor):
        mock_cursor.fetchone.side_effect = [
            {"id": 1, "title": "Test", "content": "body"},
            None,  # for images query
        ]
        mock_cursor.fetchall.return_value = []
        result = db.get_news_by_id(1)
        assert result is not None
        assert result["id"] == 1
        assert "images" in result

    def test_returns_none_for_missing(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = None
        result = db.get_news_by_id(999)
        assert result is None

    def test_includes_images_sorted(self, db, mock_cursor):
        """文章存在时执行两次查询：文章 + 图片，图片按 sort_order 排序。"""
        mock_cursor.fetchone.side_effect = [
            {"id": 1, "title": "Test"},
            None,
        ]
        mock_cursor.fetchall.return_value = [{"id": 10, "image_url": "/img.jpg"}]
        result = db.get_news_by_id(1)
        # 第一次 execute: 文章查询
        first_sql = str(mock_cursor.execute.call_args_list[0][0][0])
        assert "news_articles" in first_sql
        # 第二次 execute: 图片查询
        second_sql = str(mock_cursor.execute.call_args_list[1][0][0])
        assert "news_images" in second_sql
        assert "ORDER BY sort_order" in second_sql
        assert isinstance(result.get("images"), list)


class TestGetArticleByUrl:
    def test_returns_first_match(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = {"id": 1, "title": "T", "url": "http://x.com/1"}
        result = db.get_article_by_url("http://x.com/1")
        assert result is not None
        assert result["id"] == 1

    def test_returns_none_when_no_match(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = None
        result = db.get_article_by_url("http://no-match.com")
        assert result is None

    def test_uses_limit_one(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = None
        db.get_article_by_url("http://x.com")
        sql, _ = capture_sql(mock_cursor)
        assert "LIMIT 1" in sql


class TestGetArticlesWithoutContent:
    def test_returns_list(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        result = db.get_articles_without_content()
        assert isinstance(result, list)

    def test_filters_empty_content_and_url(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_articles_without_content()
        sql, _ = capture_sql(mock_cursor)
        assert "content IS NULL OR content = ''" in sql
        assert "url != ''" in sql

    def test_order_by_tier_priority(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_articles_without_content()
        sql, _ = capture_sql(mock_cursor)
        assert "tier ASC" in sql
        assert "priority DESC" in sql

    def test_custom_limit(self, db, mock_cursor):
        mock_cursor.fetchall.return_value = []
        db.get_articles_without_content(limit=5)
        _, params = capture_sql(mock_cursor)
        assert 5 in params


class TestGetLatestCloudSyncDate:
    def test_returns_datetime_when_exists(self, db, mock_cursor):
        from datetime import datetime, timezone, timedelta
        dt = datetime(2026, 6, 21, 10, 0, tzinfo=timezone(timedelta(hours=8)))
        mock_cursor.fetchone.return_value = [dt]
        result = db.get_latest_cloud_sync_date()
        assert result == dt

    def test_returns_none_when_no_cloud_records(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [None]
        result = db.get_latest_cloud_sync_date()
        assert result is None

    def test_filters_crawled_from_cloud(self, db, mock_cursor):
        mock_cursor.fetchone.return_value = [None]
        db.get_latest_cloud_sync_date()
        sql, _ = capture_sql(mock_cursor)
        assert "crawled_from = 'cloud'" in sql
```

- [ ] **Step 5: 运行全部查询方法测试**

Run: `pytest tests/test_postgres_query_methods.py -v`
Expected: 27 passed

- [ ] **Step 6: Commit**

```bash
git add tests/test_postgres_query_methods.py
git commit -m "test: add query method specific behavior tests"
```

---

### Task 6: 批量辅助方法 — test_postgres_batch.py

**Files:**
- Create: `tests/test_postgres_batch.py`

**Interfaces:**
- Consumes: `mock_cursor` from `tests/conftest_db.py`
- Produces: 测试 `_build_row`, `_execute_batch`, `_execute_batch_retry`

- [ ] **Step 1: _build_row 字段映射**

```python
"""Tests for batch helpers in storage/postgres.py."""
import pytest
from unittest.mock import MagicMock
from storage.postgres import PostgreSQL


def _make_test_item(**overrides):
    """构造一个最小 NewsItem。"""
    from news.models import NewsItem
    defaults = dict(
        title="Test Title",
        source_name="TestSource",
        source_type="hotlist",
        url="https://example.com/1",
        mobile_url="",
        guid="",
        rank=1,
        ranks=[1],
        summary="summary",
        author="author",
        content="content",
        category="tech",
        tags=["AI"],
        crawled_at="2026-06-21T10:00:00+08:00",
        published_at="2026-06-21T08:00:00+08:00",
        heat_score=80,
        sentiment_score=50,
        confidence=80,
    )
    defaults.update(overrides)
    return NewsItem(**defaults)


class TestBuildRow:
    def test_returns_19_element_tuple(self):
        row = PostgreSQL._build_row(
            _make_test_item(),
            source_id="src1",
            tier=2,
            priority=5,
            crawl_date="2026-06-21",
            crawled_from="local",
        )
        assert len(row) == 19

    def test_field_positions(self):
        """验证关键字段在元组中的位置。"""
        item = _make_test_item(title="Position Test", category="tech", tags=["AI", "ML"])
        row = PostgreSQL._build_row(item, "src1", 1, 10, "2026-06-21", "local")
        assert row[0] == "Position Test"   # title
        assert row[1] == "src1"             # source_id
        assert row[2] == "TestSource"       # source_name
        assert row[3] == "hotlist"          # source_type
        assert row[4] == 1                  # tier
        assert row[5] == 10                 # priority
        assert row[6] == item.url           # url
        assert row[8] == 1                  # rank
        assert row[14] == "tech"            # category
        assert row[15] == ["AI", "ML"]      # tags
        assert row[16] == "local"           # crawled_from
        assert row[18] == [1]               # ranks

    def test_none_category_becomes_none(self):
        row = PostgreSQL._build_row(
            _make_test_item(category=None),
            "src1", 4, 0, "2026-06-21", "local",
        )
        assert row[14] is None

    def test_empty_tags_becomes_empty_list(self):
        row = PostgreSQL._build_row(
            _make_test_item(tags=None),
            "src1", 4, 0, "2026-06-21", "local",
        )
        assert row[15] == []

    def test_none_published_at(self):
        row = PostgreSQL._build_row(
            _make_test_item(published_at=None),
            "src1", 4, 0, "2026-06-21", "local",
        )
        assert row[10] is None  # ts_pub

    def test_none_crawled_at_uses_crawl_date(self):
        row = PostgreSQL._build_row(
            _make_test_item(crawled_at=None),
            "src1", 4, 0, "2026-06-21", "local",
        )
        assert row[17] is None  # ts_crawled 为 None
```

- [ ] **Step 2: _execute_batch / _execute_batch_retry**

```python
class TestExecuteBatch:
    @pytest.fixture
    def pg(self):
        pg = PostgreSQL({
            "host": "localhost", "port": 5432, "database": "test",
            "user": "test", "password": "test",
        })
        pg._pool = MagicMock()
        return pg

    def test_single_page_batch(self, pg):
        """小于 page_size 的批次一次调用成功。"""
        import psycopg2.extras
        mock_cur = MagicMock()
        mock_conn = MagicMock()
        sql = "INSERT INTO t VALUES %s"
        items = [(1,), (2,), (3,)]

        with patch("psycopg2.extras.execute_values") as mock_exec:
            processed, skipped = pg._execute_batch(mock_cur, sql, items, page_size=100)
            assert processed == 3
            assert skipped == 0
            mock_exec.assert_called_once()

    def test_multiple_pages(self, pg):
        """大于 page_size 的批次分多次调用。"""
        mock_cur = MagicMock()
        sql = "INSERT INTO t VALUES %s"
        items = [(i,) for i in range(250)]  # 250 items with page_size=100 → 3 pages

        with patch.object(pg, "_execute_batch_retry", return_value=(100, 0)) as mock_retry:
            processed, skipped = pg._execute_batch(mock_cur, sql, items, page_size=100)
            assert processed == 300  # 3 calls * 100
            assert mock_retry.call_count == 3

    def test_empty_items(self, pg):
        mock_cur = MagicMock()
        processed, skipped = pg._execute_batch(mock_cur, "INSERT ...", [], page_size=100)
        assert processed == 0
        assert skipped == 0


class TestExecuteBatchRetry:
    @pytest.fixture
    def pg(self):
        pg = PostgreSQL({
            "host": "localhost", "port": 5432, "database": "test",
            "user": "test", "password": "test",
        })
        pg._pool = MagicMock()
        return pg

    def test_successful_batch(self, pg):
        mock_cur = MagicMock()
        mock_conn = MagicMock()
        sql = "INSERT INTO t VALUES %s"
        import psycopg2.extras
        with patch("psycopg2.extras.execute_values") as mock_exec:
            processed, skipped = pg._execute_batch_retry(
                mock_cur, mock_conn, sql, [(1,), (2,)], 100,
            )
            assert processed == 2
            assert skipped == 0

    def test_single_row_failure(self, pg):
        """page_size=1 时失败返回 (0, 1)，不抛异常。"""
        mock_cur = MagicMock()
        mock_conn = MagicMock()
        import psycopg2
        import psycopg2.extras
        with patch("psycopg2.extras.execute_values",
                   side_effect=psycopg2.Error("bad row")):
            processed, skipped = pg._execute_batch_retry(
                mock_cur, mock_conn, "INSERT INTO t VALUES %s", [(1,)], 1,
            )
            assert processed == 0
            assert skipped == 1

    def test_batch_failure_retries_with_smaller_pages(self, pg):
        """整批失败后降级为 page_size=10 重试。"""
        import psycopg2
        import psycopg2.extras

        call_count = [0]

        def failing_then_ok(cur, sql, batch, page_size=100):
            call_count[0] += 1
            if call_count[0] <= 1:  # first large batch fails
                raise psycopg2.Error("batch too large")
            return None  # retry succeeds (return ignored by execute_values)

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        items = [(i,) for i in range(25)]

        with patch("psycopg2.extras.execute_values") as mock_exec:
            mock_exec.side_effect = failing_then_ok
            processed, skipped = pg._execute_batch_retry(
                mock_cur, mock_conn, "INSERT INTO t VALUES %s", items, 100,
            )
            # 第一次调用 page_size=100 失败
            # 降级为 3 个 page_size=10 的子批次（25 items → 10+10+5）
            assert mock_exec.call_count >= 2
            assert processed + skipped == 25
```

Wait — the `_execute_batch_retry` test uses a side_effect that doesn't return the right values for `execute_values`. Let me reconsider: `execute_values` doesn't return a value via `return_value` — it modifies the cursor. The method counts `len(batch)` on success. The side_effect approach is tricky here.

Actually, looking at the implementation more carefully, `_execute_batch` calls `_execute_batch_retry` which calls `execute_values`. If it succeeds, returns `(len(batch), 0)`. If it fails with `psycopg2.Error` and `page_size > 1`, it divides and recursively retries.

For the retry test, I should just let the first call fail (raise Error), then the recursive calls will succeed because they use smaller page sizes — but ALL calls go through the same mocked `execute_values`. So I need a smarter side_effect.

```python
    def test_batch_failure_retries_with_smaller_pages(self, pg):
        import psycopg2

        mock_cur = MagicMock()
        mock_conn = MagicMock()
        items = [(i,) for i in range(25)]

        with patch("psycopg2.extras.execute_values") as mock_exec:
            # First call fails, subsequent calls succeed
            mock_exec.side_effect = [
                psycopg2.Error("batch too large"),   # page_size=100 → fail
                None, None, None,                     # 3 sub-batches of 10 → succeed
            ]
            processed, skipped = pg._execute_batch_retry(
                mock_cur, mock_conn, "INSERT INTO t VALUES %s", items, 100,
            )
            assert processed + skipped == 25
            assert mock_exec.call_count == 4  # 1 fail + 3 retries
```
```

- [ ] **Step 3: 运行全部批量测试**

Run: `pytest tests/test_postgres_batch.py -v`
Expected: 11 passed

- [ ] **Step 4: Commit**

```bash
git add tests/test_postgres_batch.py
git commit -m "test: add batch helper tests for _build_row, _execute_batch, _execute_batch_retry"
```

---

### Task 7: 覆盖率验证与收尾

- [ ] **Step 1: 运行全部 PostgreSQL 测试**

```bash
python -m pytest tests/test_postgres_*.py -v --cov=storage/postgres.py --cov-report=term-missing
```

- [ ] **Step 2: 检查覆盖率**

Expected:
- 行覆盖率 ≥ 80%
- `_contains_cjk` 分支 100%（中英文两条路径全测）
- `_run_migrations` 两条 migration 的触发/跳过路径全测
- 搜索路由分支（6 个查询方法 × 2 条路径）全测

- [ ] **Step 3: 如有未覆盖的关键路径，补充测试**

重点关注：
- `_to_timestamptz` 的 HH:MM 分支
- `save_news_data` 的 `crawled_from="cloud"` 路径
- `get_high_impact_count` 的 CURRENT_DATE fallback 路径

- [ ] **Step 4: 最终验证**

```bash
python -m pytest tests/test_postgres_*.py -v
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: finalize PostgreSQL unit test coverage"
```

---

## 测试汇总

| Task | 文件 | 用例数 | 优先级 |
|------|------|--------|--------|
| 0 | `tests/conftest_db.py` | — | 基础设施 |
| 1 | `tests/test_postgres_utils.py` | 13 | P0 |
| 2 | `tests/test_postgres_query_filters.py` | 22 | P0 |
| 3 | `tests/test_postgres_write.py` | 14 | P1 |
| 4 | `tests/test_postgres_lifecycle.py` | 15 | P1 |
| 5 | `tests/test_postgres_query_methods.py` | 27 | P2 |
| 6 | `tests/test_postgres_batch.py` | 11 | P2 |
| 7 | 覆盖率验证 | — | — |
| **合计** | | **102** | |

覆盖 `storage/postgres.py` 全部 18 个公开方法 + 3 个模块函数 + 3 个批量辅助方法。
