"""Tests for JuejinParser."""
import pytest
from news.parser.sites.juejin import JuejinParser


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
