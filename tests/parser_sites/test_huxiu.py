"""Tests for HuxiuParser — meta keywords removal."""
import pytest
from news.parser.sites.huxiu import HuxiuParser


class TestHuxiuParserPreprocess:
    def test_removes_meta_keywords_tag(self):
        html = (
            '<html><head>'
            '<meta name="keywords" content="虎嗅网,科技,商业">'
            '</head><body><p>正文内容 here</p></body></html>'
        )
        parser = HuxiuParser()
        result = parser._preprocess(html, "")
        assert 'name="keywords"' not in result
        assert '虎嗅网' not in result
        assert '正文内容' in result

    def test_removes_keywords_with_single_quotes(self):
        html = (
            "<html><head>"
            "<meta name='keywords' content='huxiu,tech'>"
            "</head><body><p>正文</p></body></html>"
        )
        parser = HuxiuParser()
        result = parser._preprocess(html, "")
        assert "name='keywords'" not in result

    def test_html_without_keywords_unchanged(self):
        html = '<html><head><title>Test</title></head><body><p>正文</p></body></html>'
        parser = HuxiuParser()
        result = parser._preprocess(html, "")
        assert result == html


class TestHuxiuParserCanonical:
    """article__canonical 是虎嗅文末的"文章标题/文章链接/阅读原文"
    跳转卡片,不属于正文,应在 _preprocess 中移除。"""

    def test_removes_article_canonical_card(self):
        html = (
            '<html><body>'
            '<div class="article-wrap">'
            '<div class="article__content"><p>正文第一段。</p></div>'
            '<div class="article__canonical">'
            '<p>文章标题：测试文章</p>'
            '<p>文章链接：https://www.huxiu.com/article/1.html</p>'
            '<a href="https://www.huxiu.com/article/1.html">阅读原文：测试文章_虎嗅网</a>'
            '</div>'
            '</div>'
            '</body></html>'
        )
        parser = HuxiuParser()
        result = parser._preprocess(html, "")
        assert "article__canonical" not in result
        assert "阅读原文" not in result
        assert "正文第一段。" in result

    def test_canonical_with_extra_classes_removed(self):
        html = (
            '<div class="article__canonical extra-class">'
            '<p>文章标题：测试</p>'
            '</div>'
            '<p>正文</p>'
        )
        parser = HuxiuParser()
        result = parser._preprocess(html, "")
        assert "article__canonical" not in result
        assert "正文" in result

    def test_html_without_canonical_keeps_body(self):
        html = '<html><body><div class="article__content"><p>正文</p></div></body></html>'
        parser = HuxiuParser()
        result = parser._preprocess(html, "")
        assert "正文" in result


class TestHuxiuParserParse:
    def test_parse_empty_returns_none(self):
        parser = HuxiuParser()
        assert parser.parse("") is None

    def test_parse_trivial_html(self):
        html = "<html><head><title>虎嗅</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = HuxiuParser()
        result = parser.parse(html)
        assert result is not None
        assert len(result["markdown"]) > 50

    def test_tags_empty_after_preprocess(self):
        """With meta keywords removed, parse result tags should be empty."""
        html = (
            '<html><head>'
            '<meta name="keywords" content="虎嗅网,科技,商业">'
            '</head><body>' + "<p>test content " * 30 + "</p></body></html>"
        )
        parser = HuxiuParser()
        result = parser.parse(html)
        assert result is not None
        assert result["tags"] == []
