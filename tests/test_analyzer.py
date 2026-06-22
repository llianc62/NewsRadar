# coding=utf-8
"""Tests for JiebaAnalyzer sentiment analysis."""

import pytest
from news.analyzer.jieba import JiebaAnalyzer


@pytest.fixture
def analyzer():
    """JiebaAnalyzer without DB."""
    cfg = {"analyzer": {"enabled": True, "backend": "jieba"}}
    return JiebaAnalyzer(cfg)


class TestAnalyzeSentiment:
    """Unit tests for analyze_sentiment."""

    def test_positive_text(self, analyzer):
        """利好文本 → score > 60."""
        items = [{"title": "业绩暴涨超预期", "content": "公司净利润大幅增长，股东分红创新高"}]
        analyzer.analyze_sentiment(items)
        assert items[0]["sentiment_score"] > 60

    def test_negative_text(self, analyzer):
        """利空文本 → score < 40."""
        items = [{"title": "股价暴跌", "content": "公司业绩下滑，亏损严重，面临退市风险"}]
        analyzer.analyze_sentiment(items)
        assert items[0]["sentiment_score"] < 40

    def test_neutral_text(self, analyzer):
        """无情感词 → score ≈ 50."""
        items = [{"title": "公司发布公告", "content": "公司今日发布公告，涉及日常经营事务"}]
        analyzer.analyze_sentiment(items)
        assert 40 <= items[0]["sentiment_score"] <= 60

    def test_negation_flips_polarity(self, analyzer):
        """"不会亏损" → 正面（不是负面）。"""
        items = [{"title": "不会亏损", "content": "公司表示今年不会出现亏损，预计盈利能力将改善"}]
        analyzer.analyze_sentiment(items)
        assert items[0]["sentiment_score"] > 50

    def test_degree_amplify(self, analyzer):
        """"极其利好" 得分 > "略微利好"。"""
        items_strong = [{"title": "极其利好", "content": ""}]
        items_weak = [{"title": "略微利好", "content": ""}]
        analyzer.analyze_sentiment(items_strong)
        analyzer.analyze_sentiment(items_weak)
        assert items_strong[0]["sentiment_score"] > items_weak[0]["sentiment_score"]

    def test_empty_content_title_only(self, analyzer):
        """空 content 仅 title → 不崩溃，返回有效值。"""
        items = [{"title": "利好政策出台", "content": ""}]
        analyzer.analyze_sentiment(items)
        assert 0 <= items[0]["sentiment_score"] <= 100

    def test_empty_all_text(self, analyzer):
        """全空 → score = 50（中性）。"""
        items = [{"title": "", "content": ""}]
        analyzer.analyze_sentiment(items)
        assert items[0]["sentiment_score"] == 50


class TestCreateAnalyzer:
    """Tests for create_analyzer factory."""

    def test_creates_jieba_analyzer(self):
        """backend=jieba → JiebaAnalyzer."""
        from news.analyzer import create_analyzer
        from news.analyzer.jieba import JiebaAnalyzer

        cfg = {"analyzer": {"enabled": True, "backend": "jieba"}}
        a = create_analyzer(cfg)
        assert isinstance(a, JiebaAnalyzer)

    def test_disabled_returns_none(self):
        """enabled=false → None."""
        from news.analyzer import create_analyzer

        cfg = {"analyzer": {"enabled": False, "backend": "jieba"}}
        a = create_analyzer(cfg)
        assert a is None

    def test_missing_config_returns_jieba_by_default(self):
        """缺失 analyzer config → 默认返回 JiebaAnalyzer（enabled=True）。"""
        from news.analyzer import create_analyzer
        from news.analyzer.jieba import JiebaAnalyzer

        cfg = {}
        a = create_analyzer(cfg)
        assert isinstance(a, JiebaAnalyzer)
