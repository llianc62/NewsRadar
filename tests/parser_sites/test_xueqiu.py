"""Tests for XueqiuParser — meta keywords removal."""
import pytest
from news.parser.sites.xueqiu import XueqiuParser


class TestXueqiuParserPreprocess:
    def test_removes_meta_keywords_tag(self):
        html = (
            '<html><head>'
            '<meta name="keywords" content="这篇文章分析了当前A股市场的走势和板块轮动情况">'
            '</head><body><p>正文内容 here</p></body></html>'
        )
        parser = XueqiuParser()
        result = parser._preprocess(html, "")
        assert 'name="keywords"' not in result
        assert '正文内容' in result

    def test_removes_keywords_with_single_quotes(self):
        html = (
            "<html><head>"
            "<meta name='keywords' content='雪球,股票,投资'>"
            "</head><body><p>正文</p></body></html>"
        )
        parser = XueqiuParser()
        result = parser._preprocess(html, "")
        assert "name='keywords'" not in result

    def test_html_without_keywords_unchanged(self):
        html = '<html><head><title>Test</title></head><body><p>正文</p></body></html>'
        parser = XueqiuParser()
        result = parser._preprocess(html, "")
        assert result == html


class TestXueqiuParserParse:
    def test_parse_empty_returns_none(self):
        parser = XueqiuParser()
        assert parser.parse("") is None

    def test_parse_trivial_html(self):
        html = "<html><head><title>雪球</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = XueqiuParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50

    def test_tags_empty_after_preprocess(self):
        """With meta keywords removed, parse result tags should be empty."""
        html = (
            '<html><head>'
            '<meta name="keywords" content="A股市场走势分析板块轮动">'
            '</head><body>' + "<p>test content " * 30 + "</p></body></html>"
        )
        parser = XueqiuParser()
        result = parser.parse(html)
        assert result is not None
        assert result["tags"] == []
