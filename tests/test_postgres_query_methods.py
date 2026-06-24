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
        assert "created_at DESC NULLS LAST" in sql
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
        assert "created_at >= %s::date" in sql

    def test_no_current_date_with_date_from_only(self, db, mock_cursor):
        """只传 date_from 也禁用 CURRENT_DATE。"""
        mock_cursor.fetchone.return_value = [0]
        db.get_high_impact_count(date_from="2026-06-19")
        sql, _ = capture_sql(mock_cursor)
        assert "CURRENT_DATE" not in sql


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
