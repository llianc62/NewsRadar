"""Tests for FastbullParser."""
import pytest
from pathlib import Path
from news.parser.sites.fastbull import FastbullParser


FIXTURES = Path(__file__).parent / "fixtures"


class TestFastbullParser:
    def test_parse_empty_returns_none(self):
        parser = FastbullParser()
        assert parser.parse("") is None

    def test_parse_trivial_html(self):
        html = "<html><head><title>FastBull</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = FastbullParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50


class TestFastbullParserFixture:
    """Tests with real fastbull.com HTML fixture."""

    def test_extracts_content_from_real_fixture(self):
        html = (FIXTURES / "fastbull.html").read_text(encoding="utf-8")
        parser = FastbullParser()
        result = parser.parse(html, url="https://www.fastbull.com/cn/news-detail/4380496_1")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]

    def test_images_are_not_placeholders(self):
        """Output must have real image URLs, not base64 placeholders."""
        html = (FIXTURES / "fastbull.html").read_text(encoding="utf-8")
        parser = FastbullParser()
        result = parser.parse(html, url="https://www.fastbull.com/")
        if result and "!" in result["markdown"]:
            assert "data:image" not in result["markdown"]
