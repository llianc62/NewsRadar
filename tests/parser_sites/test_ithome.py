"""Tests for IthomeParser."""
from pathlib import Path

import pytest
from lxml import html as lxml_html

from news.parser.sites.ithome import IthomeParser

FIXTURES = Path(__file__).parent / "fixtures"


class TestIthomeParser:
    def test_parse_empty_returns_none(self):
        parser = IthomeParser()
        assert parser.parse("") is None

    def test_parse_trivial_html(self):
        html = "<html><head><title>IT之家</title></head><body>" + "<p>test " * 30 + "</p></body></html>"
        parser = IthomeParser()
        result = parser.parse(html)
        # readability fallback should extract content
        assert result is not None
        assert len(result["markdown"]) > 50


class TestIthomeParserFixture:
    """Tests with real ithome.com HTML fixture."""

    def test_extracts_full_content_from_real_fixture(self):
        """Real fixture — nested <div>s inside #paragraph must not truncate."""
        html = (FIXTURES / "ithome.html").read_text(encoding="utf-8")
        parser = IthomeParser()
        result = parser.parse(html, url="https://www.ithome.com/0/968/171.htm")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]
        # 图片应该是真实 URL，不是透明占位符
        assert "img.ithome.com/images/v2/t.png" not in result["markdown"]

    def test_no_placeholder_image_in_output(self):
        """After _preprocess fixes lazy images, output must not have
        the IT之家 transparent placeholder."""
        html = (FIXTURES / "ithome.html").read_text(encoding="utf-8")
        parser = IthomeParser()
        html_after = parser._preprocess(html, "")
        # 预处理后的 HTML 不应再包含透明占位符作为 src
        assert 'src="//img.ithome.com/images/v2/t.png"' not in html_after

    def test_handles_nested_div_inside_paragraph(self):
        """Regression test: nested <div> inside #paragraph must not
        cause content truncation (the bug fixed 2026-06-24)."""
        html = (
            '<div id="paragraph">'
            '<p>第一段内容。</p>'
            '<div class="img-container"><img src="real.jpg"></div>'
            '<p>第二段内容。' + "更多测试文本。" * 20 + "</p>"
            "</div>"
        )
        parser = IthomeParser()
        result = parser.parse(html, url="https://www.ithome.com/")
        assert result is not None
        assert "第一段" in result["markdown"]
        assert "第二段" in result["markdown"]
        assert "real.jpg" in result["markdown"]


class TestFixLazyImages:
    """Unit tests for _fix_lazy_images classmethod."""

    def test_converts_srcset_to_src(self):
        html = '<img srcset="https://img.example.com/photo.jpg 2x" src="placeholder.png">'
        tree = lxml_html.fromstring(html)
        count = IthomeParser._fix_lazy_images(tree)
        assert count == 1
        result = lxml_html.tostring(tree, encoding="unicode")
        assert 'src="https://img.example.com/photo.jpg"' in result

    def test_converts_data_original_to_src(self):
        html = '<img data-original="https://img.example.com/photo.jpg" src="placeholder.png">'
        tree = lxml_html.fromstring(html)
        count = IthomeParser._fix_lazy_images(tree)
        assert count == 1
        result = lxml_html.tostring(tree, encoding="unicode")
        assert 'src="https://img.example.com/photo.jpg"' in result

    def test_skips_data_uri_srcset(self):
        html = '<img srcset="data:image/gif;base64,abc" src="placeholder.png">'
        tree = lxml_html.fromstring(html)
        count = IthomeParser._fix_lazy_images(tree)
        assert count == 0

    def test_srcset_takes_priority_over_data_original(self):
        html = (
            '<img srcset="https://example.com/large.jpg 2x" '
            'data-original="https://example.com/small.jpg" '
            'src="placeholder.png">'
        )
        tree = lxml_html.fromstring(html)
        count = IthomeParser._fix_lazy_images(tree)
        assert count == 1
        result = lxml_html.tostring(tree, encoding="unicode")
        # srcset should win (higher priority in _LAZY_IMAGE_ATTRS)
        assert 'src="https://example.com/large.jpg"' in result
