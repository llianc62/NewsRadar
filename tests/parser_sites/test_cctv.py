"""Tests for CctvParser."""

from pathlib import Path

from news.parser.sites.cctv import CctvParser

FIXTURES = Path(__file__).parent


class TestCctvParser:
    def test_parse_empty_returns_none(self):
        parser = CctvParser()
        assert parser.parse("") is None

    def test_parse_no_contentdate_returns_none(self):
        html = "<html><head><title>Test</title></head><body><p>Test</p></body></html>"
        parser = CctvParser()
        result = parser.parse(html)
        assert result is None

    def test_extract_contentdate_from_script(self):
        """Content in var contentdate = '...' should be extracted."""
        content_parts = "".join(
            f"<p>第{i}段测试内容，用于验证央视网文章正文提取功能是否正常工作。</p>"
            for i in range(1, 6)
        )
        html = (
            "<html><head><title>测试标题</title></head><body>"
            "<div id='content_area'></div>"
            f"<script>var contentdate = '{content_parts}'</script>"
            "</body></html>"
        )
        parser = CctvParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50
        assert "央视网文章正文提取" in result["markdown"]
        assert result["title"] == "测试标题"


class TestCctvParserFixture:
    """Tests with real cctv.com HTML fixture."""

    def test_extracts_content_from_real_fixture(self):
        html = (FIXTURES / "cctv.html").read_text(encoding="utf-8")
        parser = CctvParser()
        result = parser.parse(
            html,
            url="https://news.cctv.com/2026/06/25/ARTIFiTBDMPpFiGg80NaU8s0260625.shtml",
        )
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]
        assert "联合国" in result["title"]

    def test_title_strips_site_suffix(self):
        html = (FIXTURES / "cctv.html").read_text(encoding="utf-8")
        parser = CctvParser()
        result = parser.parse(
            html,
            url="https://news.cctv.com/2026/06/25/ARTIFiTBDMPpFiGg80NaU8s0260625.shtml",
        )
        assert result is not None
        title = result["title"]
        # Should strip CCTV site suffix
        assert "央视网" not in title
        assert "cctv.com" not in title.lower()
        assert "新闻频道" not in title

    def test_extracts_author(self):
        html = (FIXTURES / "cctv.html").read_text(encoding="utf-8")
        parser = CctvParser()
        result = parser.parse(
            html,
            url="https://news.cctv.com/2026/06/25/ARTIFiTBDMPpFiGg80NaU8s0260625.shtml",
        )
        assert result is not None
        # Author should be extracted from meta
        assert result.get("author") is not None
