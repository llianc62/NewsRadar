"""Tests for HtmlParser._fallback — HTML tag stripping path."""

from tests.helpers import make_html
from news.parser import HtmlParser


class TestFallback:
    """_fallback strips HTML tags when the primary extractor is unavailable."""

    def test_strips_non_content_tags(self):
        parser = HtmlParser()
        html = """<html><head><title>Test</title></head><body>
<script>console.log('x')</script>
<style>.a{color:red}</style>
<nav><a href="/">Home</a></nav>
<header>Header content</header>
<footer>Footer content</footer>
<aside>Sidebar</aside>
<p>""" + "正文段落内容。" * 20 + """</p>
</body></html>"""
        result = parser._fallback(html, "http://example.com")
        assert result is not None
        assert "正文段落内容" in result["markdown"]
        assert "console.log" not in result["markdown"]
        assert "Home" not in result["markdown"]

    def test_extracts_meta_fields(self):
        parser = HtmlParser()
        html = """<html><head>
<title>Test</title>
<meta name="author" content="张三">
<meta name="description" content="这是一篇测试文章">
<meta property="article:published_time" content="2026-01-01T10:00:00+08:00">
</head><body><p>""" + "正文段落内容。" * 20 + """</p></body></html>"""
        result = parser._fallback(html)
        assert result is not None
        assert result["author"] == "张三"
        assert "测试文章" in result["summary"]

    def test_filters_short_paragraphs(self):
        parser = HtmlParser()
        # 多个短段落 + 1 个长段落
        body = "<p>短。</p>\n<p>也很短。</p>\n<p>" + "长段落内容。" * 20 + "</p>"
        html = make_html(body)
        result = parser._fallback(html)
        assert result is not None
        assert "长段落内容" in result["markdown"]

    def test_returns_none_for_short_content(self):
        parser = HtmlParser()
        html = make_html("<p>短内容。</p>")
        result = parser._fallback(html)
        assert result is None

    def test_extracts_title(self):
        parser = HtmlParser()
        html = """<html><head>
<title>文章标题 - 网站名</title>
<meta property="og:title" content="OG 文章标题">
</head><body><p>""" + "正文段落内容。" * 20 + """</p></body></html>"""
        result = parser._fallback(html)
        assert result is not None
        # _fallback uses _extract_title_from_html which prefers og:title over <title>
        assert "文章标题" in result["title"]
