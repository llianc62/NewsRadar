"""Tests for SamrdprcParser — meta keywords removal."""
import pytest
from news.parser.sites.samrdprc import SamrdprcParser


class TestSamrdprcParserPreprocess:
    def test_removes_meta_keywords_tag(self):
        html = (
            '<html><head>'
            '<meta name="keywords" content="【广东】广州市江科电子有限公司召回部分江科牌电动车充电器,国内消费品召回新闻">'
            '</head><body><p>正文内容 here</p></body></html>'
        )
        parser = SamrdprcParser()
        result = parser._preprocess(html, "")
        assert 'name="keywords"' not in result
        assert '正文内容' in result

    def test_removes_keywords_with_single_quotes(self):
        html = (
            "<html><head>"
            "<meta name='keywords' content='召回公告,消费品'>"
            "</head><body><p>正文</p></body></html>"
        )
        parser = SamrdprcParser()
        result = parser._preprocess(html, "")
        assert "name='keywords'" not in result

    def test_html_without_keywords_unchanged(self):
        html = '<html><head><title>Test</title></head><body><p>正文</p></body></html>'
        parser = SamrdprcParser()
        result = parser._preprocess(html, "")
        assert result == html


class TestSamrdprcParserParse:
    def test_parse_empty_returns_none(self):
        parser = SamrdprcParser()
        assert parser.parse("") is None

    def test_parse_trivial_html(self):
        html = "<html><head><title>召回公告</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = SamrdprcParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50

    def test_tags_empty_after_preprocess(self):
        """With meta keywords removed and no other tags, parse result tags should be empty."""
        html = (
            '<html><head>'
            '<meta name="keywords" content="【广东】召回公告,国内消费品召回新闻">'
            '</head><body>' + "<p>test content " * 30 + "</p></body></html>"
        )
        parser = SamrdprcParser()
        result = parser.parse(html)
        assert result is not None
        # After _preprocess removes meta keywords, trafilatura should not extract tags
        assert result["tags"] == []
