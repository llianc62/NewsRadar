"""Tests for HtmlParser._beautify_markdown_formatting."""

from news.parser import HtmlParser


class TestBeautifyMarkdown:
    """_beautify_markdown_formatting cleans up formatting noise."""

    def test_normalizes_stray_spaces_in_bold_markers(self):
        markdown = "这是 ** text** 和 **text ** 测试"
        result = HtmlParser._beautify_markdown_formatting(markdown)
        assert "** text**" not in result
        assert "**text **" not in result

    def test_adds_space_around_bold_adjacent_text(self):
        markdown = "是**text**普"
        result = HtmlParser._beautify_markdown_formatting(markdown)
        assert "是 **text** 普" in result

    def test_removes_praise_button(self):
        markdown = "- +1\n\n# 标题\n\n正文内容"
        result = HtmlParser._beautify_markdown_formatting(markdown)
        assert "- +1" not in result
        assert "# 标题" in result

    def test_idempotent_on_clean_markdown(self):
        markdown = "# 标题\n\n**加粗文字** 普通文字"
        result = HtmlParser._beautify_markdown_formatting(markdown)
        assert result == markdown
