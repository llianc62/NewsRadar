"""Tests for SspaiParser."""
import pytest
from pathlib import Path
from news.parser.sites.sspai import SspaiParser


FIXTURES = Path(__file__).parent / "fixtures"


class TestSspaiParser:
    def test_parse_empty_returns_none(self):
        parser = SspaiParser()
        assert parser.parse("") is None

    def test_parse_trivial_html(self):
        html = "<html><head><title>少数派</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = SspaiParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50


class TestSspaiParserFixture:
    """Tests with real sspai.com HTML fixture."""

    def test_extracts_content_from_real_fixture(self):
        html = (FIXTURES / "sspai.html").read_text(encoding="utf-8")
        parser = SspaiParser()
        result = parser.parse(html, url="https://sspai.com/post/111216")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]

    def test_images_are_not_placeholders(self):
        """Output must have real image URLs, not base64 placeholders."""
        html = (FIXTURES / "sspai.html").read_text(encoding="utf-8")
        parser = SspaiParser()
        result = parser.parse(html, url="https://sspai.com/")
        if result and "!" in result["markdown"]:
            assert "data:image" not in result["markdown"]
