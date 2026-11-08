"""Unit tests for agent/mcp/news_server.py - analyze_sentiment 路由真实分析器。"""

import pytest

import agent.mcp.news_server as srv
from utils import format_date_today


# ── _sentiment_label 阈值 ──────────────────────────────────────────


class TestSentimentLabel:
    def test_positive_threshold(self):
        assert srv._sentiment_label(67) == "正面"
        assert srv._sentiment_label(100) == "正面"

    def test_negative_threshold(self):
        assert srv._sentiment_label(33) == "负面"
        assert srv._sentiment_label(0) == "负面"

    def test_neutral_band(self):
        assert srv._sentiment_label(34) == "中性"
        assert srv._sentiment_label(50) == "中性"
        assert srv._sentiment_label(66) == "中性"


# ── analyze_sentiment ──────────────────────────────────────────────


class _FakeAnalyzer:
    """可配置的假分析器：设置固定 sentiment_score，或抛异常。"""

    def __init__(self, score=None, raises=False):
        self._score = score
        self._raises = raises

    def analyze_sentiment(self, items):
        if self._raises:
            raise RuntimeError("boom")
        for it in items:
            it["sentiment_score"] = self._score


def _set_analyzer(analyzer):
    """注入并返回还原闭包。"""
    saved = srv._analyzer
    srv._analyzer = analyzer

    def restore():
        srv._analyzer = saved

    return restore


class TestAnalyzeSentimentRealAnalyzer:
    """_analyzer 已注入时走真实分析器路径。"""

    def test_uses_analyzer_score(self):
        restore = _set_analyzer(_FakeAnalyzer(score=80))
        try:
            result = srv.analyze_sentiment("任意文本")
        finally:
            restore()
        assert result["score"] == 80
        assert result["label"] == "正面"
        assert "jieba" in result["detail"]

    def test_analyzer_raises_falls_back_to_default(self):
        # 分析器抛异常被吞掉，item 保留初始 50 分
        restore = _set_analyzer(_FakeAnalyzer(raises=True))
        try:
            result = srv.analyze_sentiment("文本")
        finally:
            restore()
        assert result["score"] == 50
        assert result["label"] == "中性"


class TestAnalyzeSentimentFallback:
    """_analyzer 为 None 时走极简兜底词典。"""

    def test_positive_words_yield_high_score(self):
        restore = _set_analyzer(None)
        try:
            result = srv.analyze_sentiment("好优秀成功增长利好")
        finally:
            restore()
        assert result["score"] == 100
        assert result["label"] == "正面"
        assert "兜底" in result["detail"]

    def test_negative_words_yield_low_score(self):
        restore = _set_analyzer(None)
        try:
            result = srv.analyze_sentiment("失败下跌危机风险利空")
        finally:
            restore()
        assert result["score"] == 0
        assert result["label"] == "负面"

    def test_no_keyword_matches_is_neutral(self):
        restore = _set_analyzer(None)
        try:
            result = srv.analyze_sentiment("今天天气不错适合出门")
        finally:
            restore()
        assert result["score"] == 50
        assert result["label"] == "中性"

    def test_mixed_words_balance(self):
        restore = _set_analyzer(None)
        try:
            # 1 正 1 负 -> 50
            result = srv.analyze_sentiment("好失败")
        finally:
            restore()
        assert result["score"] == 50


class TestAnalyzeSentimentEmpty:
    def test_empty_string(self):
        result = srv.analyze_sentiment("")
        assert result == {"score": 50, "label": "中性", "detail": "空文本"}

    def test_whitespace_only(self):
        result = srv.analyze_sentiment("   \t  ")
        assert result["score"] == 50
        assert result["label"] == "中性"
        assert result["detail"] == "空文本"


# ── get_hot_topics ────────────────────────────────────────────────


class _FakeDB:
    """记录 get_recent_news 调用参数的假 DB。"""

    is_connected = True

    def __init__(self, articles=None, total=None):
        self._articles = articles or []
        self._total = total if total is not None else len(self._articles)
        self.get_recent_news_kwargs = None
        self.get_news_count_kwargs = None

    def get_recent_news(self, **kwargs):
        self.get_recent_news_kwargs = kwargs
        return self._articles

    def get_news_count(self, **kwargs):
        self.get_news_count_kwargs = kwargs
        return self._total


def _set_db(db):
    """注入并返回还原闭包。"""
    saved = srv._db
    srv._db = db

    def restore():
        srv._db = saved

    return restore


class TestGetHotTopics:
    def _call(self, **kwargs):
        fake = _FakeDB(articles=[])
        restore = _set_db(fake)
        try:
            srv.get_hot_topics(**kwargs)
        finally:
            restore()
        return fake.get_recent_news_kwargs

    def test_default_today_window_and_heat_order(self):
        """零参数调用 = 当天 + 热度排序 + T1 + 30 条。"""
        today = format_date_today()  # 先取期望值，避免跨午夜竞态
        kwargs = self._call()
        assert kwargs["date_from"] == today
        assert kwargs["date_to"] == today
        assert kwargs["order_by"] == "heat"
        assert kwargs["tier"] == 1
        assert kwargs["limit"] == 30

    def test_custom_date_window(self):
        kwargs = self._call(date="2026-10-01")
        assert kwargs["date_from"] == "2026-10-01"
        assert kwargs["date_to"] == "2026-10-01"

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError):
            self._call(date="2026/10/01")

    def test_sort_by_sentiment(self):
        kwargs = self._call(sort_by="sentiment")
        assert kwargs["order_by"] == "sentiment"

    def test_sort_by_time(self):
        """time 显式映射到存储层 created 键，而非依赖 None 兜底。"""
        kwargs = self._call(sort_by="time")
        assert kwargs["order_by"] == "created"

    def test_invalid_sort_by_raises(self):
        with pytest.raises(ValueError):
            self._call(sort_by="hot")

    def test_tier_all_skips_tier_filter(self):
        kwargs = self._call(tier="all")
        assert kwargs["tier"] is None

    def test_invalid_tier_raises(self):
        with pytest.raises(ValueError):
            self._call(tier="T9")

    def test_returns_full_fields_for_agent_summary(self):
        """返回需含 summary/tags/url/published_at，agent 才能完整总结热点。"""
        fake = _FakeDB(articles=[{
            "id": 1, "title": "标题", "summary": "摘要", "source_name": "来源",
            "tier": 1, "tags": ["AI"], "heat_score": 95, "sentiment_score": 80,
            "url": "https://example.com/a", "published_at": "2026-10-09 08:00:00",
        }])
        restore = _set_db(fake)
        try:
            result = srv.get_hot_topics()
        finally:
            restore()
        items = result["items"]
        assert len(items) == 1
        for field in ("summary", "tags", "url", "published_at", "tier"):
            assert field in items[0]
        assert items[0]["summary"] == "摘要"

    def test_none_fields_render_as_empty_string(self):
        """RealDictCursor 对 NULL 列返回 None（.get 默认值不生效），需兜底为空串。"""
        fake = _FakeDB(articles=[{
            "id": 2, "title": None, "summary": None, "source_name": None,
            "tier": None, "tags": None, "heat_score": None,
            "sentiment_score": None, "url": None, "published_at": None,
        }])
        restore = _set_db(fake)
        try:
            result = srv.get_hot_topics()
        finally:
            restore()
        row = result["items"][0]
        assert row["title"] == ""
        assert row["summary"] == ""
        assert row["published_at"] == ""  # 而不是垃圾串 "None"
        assert row["tags"] == []

    def test_custom_limit_and_offset_passthrough(self):
        kwargs = self._call(limit=15, offset=30)
        assert kwargs["limit"] == 15
        assert kwargs["offset"] == 30

    def test_default_offset_is_zero(self):
        kwargs = self._call()
        assert kwargs["offset"] == 0

    def test_limit_capped_at_50(self):
        kwargs = self._call(limit=100)
        assert kwargs["limit"] == 50

    def test_limit_below_one_clamped(self):
        kwargs = self._call(limit=0)
        assert kwargs["limit"] == 1

    def test_negative_offset_clamped(self):
        kwargs = self._call(offset=-5)
        assert kwargs["offset"] == 0

    # ── 分页元数据（items / total / next_offset）──────────────────

    def test_returns_pagination_metadata(self):
        """返回值应含 items / total / next_offset 三字段，且无下一页时 next_offset 为 None。"""
        fake = _FakeDB(articles=[{"id": 1}], total=1)
        restore = _set_db(fake)
        try:
            result = srv.get_hot_topics()
        finally:
            restore()
        assert set(result) == {"items", "total", "next_offset"}
        assert result["total"] == 1
        assert result["next_offset"] is None  # 0 + 30 >= 1，无下一页

    def test_next_offset_steps_by_limit(self):
        """数据充足时 next_offset 固定按 limit 步进（默认 +30），与返回条数无关。"""
        fake = _FakeDB(articles=[], total=100)
        restore = _set_db(fake)
        try:
            first = srv.get_hot_topics()
            second = srv.get_hot_topics(offset=30)
            third = srv.get_hot_topics(offset=60)
        finally:
            restore()
        assert first["next_offset"] == 30
        assert second["next_offset"] == 60
        assert third["next_offset"] == 90

    def test_next_offset_none_at_last_page(self):
        """offset + limit >= total 时 next_offset 为 None。"""
        fake = _FakeDB(articles=[], total=45)
        restore = _set_db(fake)
        try:
            result = srv.get_hot_topics(offset=30)  # 30 + 30 = 60 >= 45
        finally:
            restore()
        assert result["next_offset"] is None

    def test_total_uses_same_filter_scope(self):
        """total 应通过 get_news_count 用相同 tier / date 口径计算。"""
        fake = _FakeDB(articles=[], total=7)
        restore = _set_db(fake)
        try:
            srv.get_hot_topics(tier="T2", date="2026-10-01")
        finally:
            restore()
        assert fake.get_news_count_kwargs["tier"] == 2
        assert fake.get_news_count_kwargs["date_from"] == "2026-10-01"
        assert fake.get_news_count_kwargs["date_to"] == "2026-10-01"
