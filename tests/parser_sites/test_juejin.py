"""Tests for JuejinParser."""
import pytest
from pathlib import Path
from news.parser.sites.juejin import JuejinParser


FIXTURES = Path(__file__).parent / "fixtures"


class TestJuejinParserPreprocess:
    def test_removes_meta_keywords_tag(self):
        html = (
            '<html><head>'
            '<meta name="keywords" content="前端开发社区,JavaScript,CSS,HTML5">'
            '</head><body><p>正文内容 here</p></body></html>'
        )
        parser = JuejinParser()
        result = parser._preprocess(html, "")
        assert 'name="keywords"' not in result
        assert '前端开发社区' not in result
        assert '正文内容' in result

    def test_removes_keywords_with_single_quotes(self):
        html = (
            "<html><head>"
            "<meta name='keywords' content='前端,后端,全栈'>"
            "</head><body><p>正文</p></body></html>"
        )
        parser = JuejinParser()
        result = parser._preprocess(html, "")
        assert "name='keywords'" not in result

    def test_html_without_keywords_unchanged(self):
        html = '<html><head><title>Test</title></head><body><p>正文</p></body></html>'
        parser = JuejinParser()
        result = parser._preprocess(html, "")
        assert result == html


class TestJuejinParser:
    def test_parse_empty_returns_none(self):
        parser = JuejinParser()
        assert parser.parse("") is None

    def test_parse_trivial_html(self):
        html = "<html><head><title>掘金</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = JuejinParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50

    def test_tags_empty_after_preprocess(self):
        """With meta keywords removed, parse result tags should be empty."""
        html = (
            '<html><head>'
            '<meta name="keywords" content="前端开发社区,JavaScript,CSS,HTML5">'
            '</head><body>' + "<p>test content " * 30 + "</p></body></html>"
        )
        parser = JuejinParser()
        result = parser.parse(html)
        assert result is not None
        assert result["tags"] == []


class TestJuejinParserFixture:
    """Tests with real juejin.cn HTML fixture."""

    def test_extracts_content_from_real_fixture(self):
        html = (FIXTURES / "juejin.html").read_text(encoding="utf-8")
        parser = JuejinParser()
        result = parser.parse(html, url="https://juejin.cn/post/7654102171461402662")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]

    def test_images_are_not_placeholders(self):
        """Output must have real image URLs, not base64 placeholders."""
        html = (FIXTURES / "juejin.html").read_text(encoding="utf-8")
        parser = JuejinParser()
        result = parser.parse(html, url="https://juejin.cn/")
        if result and "!" in result["markdown"]:
            assert "data:image" not in result["markdown"]
