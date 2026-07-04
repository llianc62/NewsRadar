"""Tests for StcnParser — meta keywords removal."""
import pytest
from news.parser.sites.stcn import StcnParser


class TestStcnParserPreprocess:
    def test_removes_meta_keywords_tag(self):
        html = (
            '<html><head>'
            '<meta name="keywords" content="人工智能,半导体,机器人,新能源,数字经济">'
            '</head><body><p>正文内容 here</p></body></html>'
        )
        parser = StcnParser()
        result = parser._preprocess(html, "")
        assert 'name="keywords"' not in result
        assert '人工智能' not in result
        assert '正文内容' in result

    def test_removes_keywords_with_single_quotes(self):
        html = (
            "<html><head>"
            "<meta name='keywords' content='证券,时报,股票'>"
            "</head><body><p>正文</p></body></html>"
        )
        parser = StcnParser()
        result = parser._preprocess(html, "")
        assert "name='keywords'" not in result

    def test_html_without_keywords_unchanged(self):
        html = '<html><head><title>Test</title></head><body><p>正文</p></body></html>'
        parser = StcnParser()
        result = parser._preprocess(html, "")
        assert result == html


class TestStcnParserParse:
    def test_parse_empty_returns_none(self):
        parser = StcnParser()
        assert parser.parse("") is None

    def test_parse_trivial_html(self):
        html = "<html><head><title>证券时报</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = StcnParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50

    def test_tags_empty_after_preprocess(self):
        """With meta keywords removed, parse result tags should be empty."""
        html = (
            '<html><head>'
            '<meta name="keywords" content="人工智能,半导体,机器人,新能源">'
            '</head><body>' + "<p>test content " * 30 + "</p></body></html>"
        )
        parser = StcnParser()
        result = parser.parse(html)
        assert result is not None
        assert result["tags"] == []
