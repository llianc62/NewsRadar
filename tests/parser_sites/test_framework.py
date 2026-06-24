"""Smoke tests for HtmlParser base class and ParserRegistry routing."""
import pytest
from news.parser.parser import HtmlParser, _split_keyword_tags
from news.parser.registry import parser_registry, ParserRegistry


class TestHtmlParserBase:
    """Base class default behavior — no site-specific hooks."""

    def test_parse_empty_html_returns_none(self):
        parser = HtmlParser()
        assert parser.parse("") is None
        assert parser.parse("   ") is None

    def test_parse_trivial_html_falls_back(self):
        html = "<html><head><title>Test</title></head><body>" + "<p>content " * 30 + "</p></body></html>"
        parser = HtmlParser()
        result = parser.parse(html, url="https://example.com")
        assert result is not None
        assert result["markdown"]
        assert "content" in result["markdown"]

    def test_extract_title_from_og_title(self):
        html = '<html><head><meta property="og:title" content="OG标题"></head><body></body></html>'
        title = HtmlParser._extract_title_from_html(html)
        assert title == "OG标题"

    def test_extract_title_from_title_tag(self):
        html = '<html><head><title>页面标题 - 网站名</title></head><body></body></html>'
        title = HtmlParser._extract_title_from_html(html)
        assert "页面标题" in title

    def test_extract_meta_author(self):
        html = '<html><head><meta name="author" content="张三"></head><body></body></html>'
        author = HtmlParser._extract_meta(html, r'name=["\']author["\']')
        assert author == "张三"

    def test_extract_markdown_heading(self):
        md = "# 文章标题\n\n正文内容"
        title = HtmlParser._extract_markdown_heading(md)
        assert title == "文章标题"

    def test_extract_markdown_heading_skips_code_fence(self):
        md = "```bash\n# 这是注释\n```\n# 真正的标题\n\n正文"
        title = HtmlParser._extract_markdown_heading(md)
        assert title == "真正的标题"

    def test_build_result(self):
        result = HtmlParser._build_result(
            markdown="测试正文",
            title="测试标题",
            author="作者",
            published_at="2026-06-24",
            summary="摘要",
            tags=["科技", "AI"],
        )
        assert result["markdown"] == "测试正文"
        assert result["title"] == "测试标题"
        assert result["author"] == "作者"
        assert result["tags"] == ["科技", "AI"]

    def test_build_result_strips_tag_hash_prefix(self):
        result = HtmlParser._build_result(
            markdown="x",
            tags=["#科技", "#经济", ""],
        )
        assert result["tags"] == ["科技", "经济"]

    def test_max_content_length_truncation(self):
        parser = HtmlParser({"crawler": {"max_content_length": 50}})
        result = parser.parse("x" * 100)
        # Empty HTML won't parse, so test via direct parse
        html = "<html><head><title>T</title></head><body>" + "<p>" + "word " * 200 + "</p></body></html>"
        result = parser.parse(html)
        if result:
            assert len(result["markdown"]) <= 50 + len("\n\n... (truncated)")


class TestSplitKeywordTags:
    def test_comma_separated(self):
        assert _split_keyword_tags(["科技,AI, 经济"]) == ["科技", "AI", "经济"]

    def test_space_separated(self):
        assert _split_keyword_tags(["科技 AI 经济"]) == ["科技", "AI", "经济"]

    def test_dedup_preserves_order(self):
        assert _split_keyword_tags(["科技,AI,科技"]) == ["科技", "AI"]


class TestParserRegistry:
    """Test routing behavior."""

    def test_registered_source_id_routes_to_correct_parser(self):
        reg = ParserRegistry()
        reg.set_default(HtmlParser())

        class DummyParser(HtmlParser):
            def _extract(self, html, url):
                return self._build_result(markdown="dummy result")

        reg.register("dummy", DummyParser())
        result = reg.parse("dummy", "<html></html>", "")
        assert result is not None
        assert result["markdown"] == "dummy result"

    def test_unregistered_source_id_falls_back_to_default(self):
        reg = ParserRegistry()
        reg.set_default(HtmlParser())
        html = "<html><head><title>Fallback</title></head><body>" + "<p>text " * 30 + "</p></body></html>"
        result = reg.parse("unknown-source", html, "")
        assert result is not None
        # Default HtmlParser uses readability → fallback
        assert len(result["markdown"]) > 50
