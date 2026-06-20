"""Tests for HtmlParser._trim_noise — head/tail noise trimming."""

import pytest
from tests.helpers import make_html
from news.parser import HtmlParser


class TestTrimNoise:
    """Tests for _trim_noise boundary detection."""

    def test_keeps_paragraph_body(self):
        """Body paragraphs should be fully retained."""
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        html = make_html(body)
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
        html = make_html(body, tail_noise=tail)
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
        html = make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "面包屑" not in result
        assert "新闻正文内容" in result

    def test_trims_share_buttons_before_body(self):
        """Share button text before body should be trimmed."""
        head = '<p>分享到：<a href="#">微信</a> <a href="#">微博</a></p>'
        body = "<p>" + "新闻正文内容。" * 20 + "</p>"
        html = make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "分享到" not in result

    def test_short_page_degrades_to_none(self):
        """Page with too few blocks should return None."""
        html = make_html("<p>短。</p>")
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
        html = make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "重要标题" in result

    def test_h2_fallback_when_no_paragraph(self):
        """h2 should be used as start signal when no h1 or long paragraph exists."""
        head = "<p>短导航</p>"
        body = "<h2>重要标题</h2><p>短正文。</p>"
        html = make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "重要标题" in result

    def test_link_density_detects_noise(self):
        """A block with high link density should be treated as noise."""
        head = '<p><a href="/a">链接1</a> <a href="/b">链接2</a> <a href="/c">链接3</a></p>'
        body = "<p>" + "正文内容。" * 20 + "</p>"
        html = make_html(body, head_noise=head)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "链接1" not in result

    def test_preserves_figure_with_image(self):
        """<figure> containing an <img> should be preserved in the output."""
        body = "<p>" + "正文内容。" * 20 + "</p>"
        body += '<figure><img src="https://example.com/photo.jpg" alt="配图"></figure>'
        body += "<p>" + "更多内容。" * 20 + "</p>"
        html = make_html(body)
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
        html = make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "chart.png" in result

    def test_short_page_with_h1_not_degraded(self):
        """A page with h1 heading but short body should not degrade to None."""
        body = "<h1>文章标题</h1><p>短正文。</p>"
        html = make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "标题" in result

    def test_h4_h5_h6_skipped_as_footer_headings(self):
        """h4/h5/h6 are almost always footer headings like '扫码下载APP'
        and should be skipped when searching for the end boundary."""
        body = "<h1>文章标题</h1><p>" + "正文内容。" * 20 + "</p>"
        tail = "<h4>扫码下载APP</h4><p>© 2024</p>"
        html = make_html(body, tail_noise=tail)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "扫码下载" not in result

    def test_start_gt_end_degrades_to_none(self):
        """When start > end (overlap), _trim_noise returns None."""
        # This is challenging to trigger manually — test the degenerate case
        # where boundaries can't be established
        html = make_html("")
        parser = HtmlParser()
        # No blocks at all — should degrade
        result = parser._trim_noise(html)
        assert result is None

    def test_no_blocks_degrades_to_none(self):
        """HTML with no block-level elements should return None."""
        html = "<html><body>裸文本，没有块级标签。</body></html>"
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is None

    def test_preserves_nested_div_between_boundaries(self):
        """Content inside nested <div> between start and end is preserved
        after DOM pruning (not lost like with the old block reassembly)."""
        body = "<h1>标题</h1>"
        body += "<div><p>" + "嵌套段落内容。" * 20 + "</p></div>"
        body += "<p>" + "更多内容。" * 10 + "</p>"
        html = make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "嵌套段落内容" in result

    def test_removes_meta_wrapper_different_parents(self):
        """When h1 and the content div are siblings, noise wrappers
        between them (author/date/share divs) should be removed."""
        body = "<h1>文章标题</h1>"
        # Metadata wrapper — author, date, share buttons
        body += '<div class="meta"><span>作者：张三</span><span>2026-06-15</span></div>'
        body += '<div class="content"><p>' + "正文内容。" * 20 + "</p></div>"
        html = make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        # Metadata wrapper should be removed
        assert "作者" not in result
        assert "正文内容" in result

    def test_short_copyright_line_trimmed(self):
        """Short <p> at the end like '© 2024 某某网' (< 30 chars) is
        treated as tail noise."""
        body = "<h1>标题</h1><p>" + "正文内容。" * 20 + "</p>"
        tail = "<p>© 2024 某某网</p>"
        html = make_html(body, tail_noise=tail)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "©" not in result
        assert "正文内容" in result

    def test_long_paragraph_as_end_signal(self):
        """A paragraph >= 50 chars with low link density should serve
        as a reliable end boundary."""
        body = "<h1>标题</h1><p>" + "正文内容。" * 20 + "</p>"
        tail = "<p>" + "尾部无关链接。" * 5 + "</p>"
        html = make_html(body, tail_noise=tail)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "正文内容" in result

    def test_output_wrapped_in_article(self):
        """_trim_noise output is wrapped in <html><body><article> for
        trafilatura heading recognition."""
        body = "<h1>文章标题</h1><p>" + "正文内容。" * 20 + "</p>"
        html = make_html(body)
        parser = HtmlParser()
        result = parser._trim_noise(html)
        assert result is not None
        assert "<article>" in result
        assert "文章标题" in result
