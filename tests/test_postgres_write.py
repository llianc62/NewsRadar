"""Tests for write operations in storage/postgres.py."""
import pytest
from unittest.mock import MagicMock, patch, call
from tests.conftest_db import capture_sql


# 工厂函数：构造最小 NewsItem
def _make_item(source_type="hotlist", url="https://example.com/news/1",
               source_id="src1", guid="", source_name="TestSource", title="Test Title",
               crawled_at="2026-06-21T10:00:00+08:00",
               published_at="2026-06-21T08:00:00+08:00"):
    from news.models import NewsItem
    return NewsItem(
        title=title,
        source_id=source_id,
        source_name=source_name,
        source_type=source_type,
        url=url,
        mobile_url="",
        guid=guid,
        rank=1,
        ranks=[[1, 20]],
        summary="summary",
        author="author",
        content="content",
        category="tech",
        tags=["AI"],
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
