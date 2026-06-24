"""Tests for FastbullParser."""
import pytest
from news.parser.sites.fastbull import FastbullParser


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
