"""Tests for HtmlParser._build_image_markdown — image-heavy content fallback."""

from news.parser import HtmlParser


class TestBuildImageMarkdown:
    """_build_image_markdown extracts img tags into markdown for
    image-heavy / low-text articles."""

    def test_extracts_img_to_markdown_syntax(self):
        html = '<img src="https://x.com/photo.jpg" alt="配图">'
        result = HtmlParser._build_image_markdown(html)
        assert "![](https://x.com/photo.jpg)" in result

    def test_preserves_remaining_text(self):
        html = '<img src="https://x.com/chart.png"><p>图表说明文字</p>'
        result = HtmlParser._build_image_markdown(html)
        assert "![](https://x.com/chart.png)" in result
        assert "图表说明文字" in result

    def test_handles_multiple_images(self):
        html = '<img src="https://x.com/a.jpg"><img src="https://x.com/b.jpg">'
        result = HtmlParser._build_image_markdown(html)
        assert "![](https://x.com/a.jpg)" in result
        assert "![](https://x.com/b.jpg)" in result

    def test_returns_empty_for_no_images_or_text(self):
        html = "<div></div>"
        result = HtmlParser._build_image_markdown(html)
        assert result == ""
