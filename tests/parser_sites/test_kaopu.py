"""Tests for KaopuParser."""
import pytest
from news.parser.sites.kaopu import KaopuParser


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
