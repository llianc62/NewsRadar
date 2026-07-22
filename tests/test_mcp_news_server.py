"""Unit tests for agent/mcp/news_server.py - analyze_sentiment 路由真实分析器。"""

import agent.mcp.news_server as srv


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
