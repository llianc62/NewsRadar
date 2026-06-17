"""Tests for HtmlParser._trim_noise — head/tail noise trimming."""

import pytest
from news.parser import HtmlParser


def _make_html(body: str, head_noise: str = "", tail_noise: str = "") -> str:
    """Build a minimal HTML page with optional head/tail noise around body."""
    return f"""<!DOCTYPE html>
<html>
<head><title>Test Article</title></head>
<body>
<article>
{head_noise}
{body}
{tail_noise}
</article>
</body>
</html>"""


class TestTrimNoise:
    """Tests for _trim_noise boundary detection."""

    def test_keeps_paragraph_body(self):
        """Body paragraphs should be fully retained."""
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        html = _make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "新闻正文内容" in result
        # Should not lose body content
        assert result.count("新闻正文内容") == 20

    def test_trims_footer_copyright(self):
        """Short link-heavy footer after body should be trimmed."""
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        tail = """<footer>
<p>版权所有 © 2024 某某网</p>
<p><a href="/about">关于我们</a> | <a href="/contact">联系我们</a></p>
</footer>"""
        html = _make_html(body, tail_noise=tail)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "版权所有" not in result
        assert "新闻正文内容" in result

    def test_trims_head_navigation(self):
        """Short link-heavy nav before body should be trimmed."""
        head = """<nav>
<a href="/">首页</a> | <a href="/news">新闻</a> | <a href="/about">关于</a>
</nav>
<p>面包屑：首页 &gt; 新闻</p>"""
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        html = _make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "面包屑" not in result
        assert "新闻正文内容" in result

    def test_trims_share_buttons_before_body(self):
        """Share button text before body should be trimmed."""
        head = '<p>分享到：<a href="#">微信</a> <a href="#">微博</a></p>'
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        html = _make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "分享到" not in result

    def test_short_page_degrades_to_none(self):
        """Page with too few blocks should return None."""
        html = _make_html("<p>短。</p>")
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is None

    def test_malformed_html_degrades_to_none(self):
        """Malformed HTML should not crash, return None."""
        parser = HtmlParser()
        result = parser._trim_noise("not even html")
        assert result is None

    def test_body_with_h1_heading_kept(self):
        """Body with an h1 heading should be kept — h1 has highest priority."""
        head = "<p>短导航</p>"
        body = "<h1>重要标题</h1><p>" + "正文内容。" * 20 + "</p>"
        html = _make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "重要标题" in result

    def test_h2_fallback_when_no_paragraph(self):
        """h2 should be used as start signal when no h1 or long paragraph exists."""
        head = "<p>短导航</p>"
        body = "<h2>重要标题</h2><p>短正文。</p>"
        html = _make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "重要标题" in result

    def test_link_density_detects_noise(self):
        """A block with high link density should be treated as noise."""
        head = '<p><a href="/a">链接1</a> <a href="/b">链接2</a> <a href="/c">链接3</a></p>'
        body = "<p>" + "正文内容。" * 20 + "</p>"
        html = _make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "链接1" not in result

    def test_preserves_figure_with_image(self):
        """<figure> containing an <img> should be preserved in the output."""
        body = "<p>" + "正文内容。" * 20 + "</p>"
        body += '<figure><img src="https://example.com/photo.jpg" alt="配图"></figure>'
        body += "<p>" + "更多内容。" * 20 + "</p>"
        html = _make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "photo.jpg" in result
        assert "<figure>" in result

    def test_preserves_image_inside_paragraph(self):
        """<p> containing only an <img> should not be discarded as empty."""
        body = "<p>" + "正文内容。" * 20 + "</p>"
        body += '<p><img src="https://example.com/chart.png" alt="图表"></p>'
        body += "<p>" + "更多内容。" * 20 + "</p>"
        html = _make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "chart.png" in result
