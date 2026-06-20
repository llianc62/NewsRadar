"""Tests for HtmlParser._extract_bracketed_json and _extract_json_ld."""

from news.parser import HtmlParser


class TestExtractBracketedJson:
    """_extract_bracketed_json finds and parses JSON by bracket-matching."""

    def test_bracket_match_simple(self):
        html = 'var data = {"key":"value"};'
        results = HtmlParser._extract_bracketed_json(html, r'data\s*=\s*(\{)')
        assert len(results) == 1
        assert results[0] == {"key": "value"}

    def test_bracket_match_nested(self):
        html = '{"outer":{"inner":{"deep":1}}}'
        results = HtmlParser._extract_bracketed_json(html, r'=\s*(\{)')
        # Pattern doesn't match — test direct nested bracket matching
        results = HtmlParser._extract_bracketed_json(html, r'^(\{)')
        assert len(results) == 1
        assert results[0] == {"outer": {"inner": {"deep": 1}}}

    def test_bracket_match_unclosed_returns_empty(self):
        html = 'var data = {"key":"value"'
        results = HtmlParser._extract_bracketed_json(html, r'data\s*=\s*(\{)')
        assert results == []


class TestExtractJsonLd:
    """_extract_json_ld extracts JSON-LD from script tags."""

    def test_extracts_multiple_script_tags(self):
        html = """<script type="application/ld+json">{"@type":"Article","headline":"A"}</script>
<script type="application/ld+json">{"@type":"WebSite","name":"S"}</script>"""
        results = HtmlParser._extract_json_ld(html)
        assert len(results) == 2
        assert results[0]["@type"] == "Article"
        assert results[1]["@type"] == "WebSite"

    def test_skips_invalid_json(self):
        html = """<script type="application/ld+json">{invalid json}</script>
<script type="application/ld+json">{"@type":"Article"}</script>"""
        results = HtmlParser._extract_json_ld(html)
        assert len(results) == 1
        assert results[0]["@type"] == "Article"
