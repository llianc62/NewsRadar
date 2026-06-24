"""Tests for SspaiParser."""
import pytest
from news.parser.sites.sspai import SspaiParser


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
