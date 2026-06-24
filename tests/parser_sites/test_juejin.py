"""Tests for JuejinParser."""
import pytest
from pathlib import Path
from news.parser.sites.juejin import JuejinParser


FIXTURES = Path(__file__).parent / "fixtures"


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
