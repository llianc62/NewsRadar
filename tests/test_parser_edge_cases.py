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


class TestFormatMarkdownH1Trim:
    """_format_markdown trims page-header noise before the first H1,
    but must not destroy the article when the first H1 appears at the
    end (e.g. huxiu authors paste a '# 推广文案' line after the body)."""

    def test_trims_header_noise_before_h1(self):
        markdown = "站点噪声\n\n导航栏\n\n# 文章标题\n\n正文第一段。\n\n正文第二段。"
        result = HtmlParser()._format_markdown(markdown)
        assert result.startswith("# 文章标题")
        assert "站点噪声" not in result
        assert "正文第二段。" in result

    def test_keeps_article_when_h1_is_trailing_noise(self):
        """H1 前是整篇正文、H1 后是少数推广文案时,不得截断正文。"""
        body = "\n\n".join(f"正文第{i}段,内容足够长以构成主体。" for i in range(20))
        markdown = f"{body}\n\n# 作者推广文案加微信\n\n如对本稿件有异议请联系。"
        result = HtmlParser()._format_markdown(markdown)
        assert "正文第0段" in result
        assert "正文第19段" in result
        # 尾部推广文案保留为正文一部分（它是作者贴的内容，不是页头噪声）
        assert "作者推广文案" in result

    def test_no_h1_unchanged(self):
        markdown = "## 副标题\n\n正文内容"
        result = HtmlParser()._format_markdown(markdown)
        assert result == markdown.strip()

    def test_h1_at_start_unchanged(self):
        markdown = "# 文章标题\n\n正文"
        result = HtmlParser()._format_markdown(markdown)
        assert result == markdown


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
