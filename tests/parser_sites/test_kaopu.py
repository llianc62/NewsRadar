"""Tests for KaopuParser."""
import pytest
from pathlib import Path
from news.parser.sites.kaopu import KaopuParser


FIXTURES = Path(__file__).parent / "fixtures"


class TestKaopuParser:
    def test_parse_empty_returns_none(self):
        parser = KaopuParser()
        assert parser.parse("") is None

    def test_parse_trivial_html(self):
        html = "<html><head><title>靠谱新闻</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = KaopuParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50


class TestKaopuParserFixture:
    """Tests with real kaopu.news HTML fixture."""

    def test_extracts_content_from_real_fixture(self):
        html = (FIXTURES / "kaopu.html").read_text(encoding="utf-8")
        parser = KaopuParser()
        result = parser.parse(html, url="https://kaopu.news/story/e38906")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]

    def test_images_are_not_placeholders(self):
        """Output must have real image URLs, not base64 placeholders."""
        html = (FIXTURES / "kaopu.html").read_text(encoding="utf-8")
        parser = KaopuParser()
        result = parser.parse(html, url="https://kaopu.news/")
        if result and "!" in result["markdown"]:
            assert "data:image" not in result["markdown"]
