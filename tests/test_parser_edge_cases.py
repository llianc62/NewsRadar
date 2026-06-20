"""Tests for parser edge cases: truncation, heading extraction,
result building, and empty input."""

from news.parser import HtmlParser


class TestExtractMarkdownHeading:
    """_extract_markdown_heading extracts the first H1 from markdown."""

    def test_returns_first_h1(self):
        markdown = "# 文章标题\n\n## 副标题\n\n正文内容"
        result = HtmlParser._extract_markdown_heading(markdown)
        assert result == "文章标题"

    def test_returns_empty_when_no_h1(self):
        markdown = "## 只有副标题\n\n正文内容"
        result = HtmlParser._extract_markdown_heading(markdown)
        assert result == ""


class TestBuildResult:
    """_build_result builds the unified result dict."""

    def test_strips_hash_prefix_from_tags(self):
        result = HtmlParser._build_result(
            "content", tags=["#tag1", "#tag2"]
        )
        assert result["tags"] == ["tag1", "tag2"]

    def test_removes_empty_tags_after_stripping(self):
        result = HtmlParser._build_result(
            "content", tags=["#", "tag"]
        )
        assert result["tags"] == ["tag"]


class TestParseEdgeCases:
    """parse() edge cases."""

    def test_parse_returns_none_for_empty_html(self):
        parser = HtmlParser()
        result = parser.parse("")
        assert result is None

    def test_truncates_content_over_max_length(self):
        parser = HtmlParser()
        parser.max_content_length = 200
        long_text = "正文内容。" * 100  # ~600 chars
        html = f"<html><body><article><p>{long_text}</p></article></body></html>"
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) <= parser.max_content_length + len("\n\n... (truncated)")
        assert "... (truncated)" in result["markdown"]
