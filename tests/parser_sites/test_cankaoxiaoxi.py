"""Tests for CkxxappParser — JS variable content extraction."""
import pytest
from pathlib import Path
from news.parser.sites.cankaoxiaoxi import CkxxappParser


FIXTURES = Path(__file__).parent / "fixtures"


class TestCkxxappParserJsExtraction:
    """Test _extract_js_content_vars method directly."""

    def test_extracts_content_from_content_txt(self):
        """Load real fixture and verify content is extracted."""
        html = (FIXTURES / "cankaoxiaoxi.html").read_text(encoding="utf-8")
        parser = CkxxappParser()
        result = parser.parse(html, url="https://ckxxapp.ckxx.net/pages/2026/06/24/3c51c84696714b3c83ecb16ac8414d79.html")
        assert result is not None
        assert len(result["markdown"]) > 100
        assert "共同社" in result["markdown"]

    def test_returns_none_when_no_var_present(self):
        """HTML without contentTxt variable should return None."""
        html = "<html><body><p>No script variable here.</p></body></html>"
        parser = CkxxappParser()
        result = parser.parse(html)
        assert result is None

    def test_returns_none_for_short_content(self):
        """Content less than 50 chars should return None."""
        html = '<script>var contentTxt ="<p>Short</p>";</script>'
        parser = CkxxappParser()
        result = parser.parse(html)
        assert result is None

    def test_unescapes_js_string(self):
        """Verify JS-escaped double quotes and slashes are properly unescaped."""
        html = (
            '<script>var contentTxt ="'
            '<p>He said \\"Hello, World!\\" to me.<\\/p>'
            '<p>Another paragraph that is definitely longer than fifty characters for your threshold test.<\\/p>'
            '";</script>'
        )
        parser = CkxxappParser()
        result = parser.parse(html)
        assert result is not None
        assert 'He said "Hello, World!" to me.' in result["markdown"]
        # No escaped quotes should remain
        assert '\\"' not in result["markdown"]

    def test_full_extraction_pipeline(self):
        """Test full parse method with real fixture."""
        html = (FIXTURES / "cankaoxiaoxi.html").read_text(encoding="utf-8")
        parser = CkxxappParser()
        result = parser.parse(html, url="https://ckxxapp.ckxx.net/pages/2026/06/24/3c51c84696714b3c83ecb16ac8414d79.html")
        assert result is not None
        # Check metadata
        assert result["title"] == '外媒：金正恩批评日本谋求“军事大国化”'
        assert result["published_at"] == "2026-06-24"
        # Author is intentionally empty in real pages
        assert "分享来自参考消息" in result["summary"]
        # Check content
        assert len(result["markdown"]) > 100
        # Verify no JS escape remnants
        assert '\\"' not in result["markdown"] or '\\\\"' not in result["markdown"]
        assert '\\/' not in result["markdown"]
