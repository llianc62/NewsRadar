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
        db.get_recent_news(keywords=["芯片"])
        sql, params = capture_sql(mock_cursor)
        assert "ILIKE" in sql
        assert "array_to_string(tags, ' ')" in sql
        assert "%芯片%" in params

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
            keywords=["芯片"],
            search="AI",
            date_from="2026-06-19",
            date_to="2026-06-21",
        )
        sql, params = capture_sql(mock_cursor)
        assert "tier = %s" in sql
        assert "category = %s" in sql
        assert "sentiment_score >= 67" in sql
        assert "ILIKE" in sql
        assert "array_to_string(tags, ' ')" in sql
        assert "plainto_tsquery" in sql  # "AI" 不含 CJK → FTS
        assert "published_at" in sql
        # params 包含所有过滤值
        assert 1 in params
        assert "tech" in params
        assert "%芯片%" in params
        assert "AI" in params

    def test_pagination_params_appended_last(self, db, mock_cursor):
        """LIMIT 和 OFFSET 作为最后两个参数追加。"""
        mock_cursor.fetchall.return_value = []
        db.get_recent_news(limit=20, offset=40)
        _, params = capture_sql(mock_cursor)
        assert params[-2] == 20
        assert params[-1] == 40


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
