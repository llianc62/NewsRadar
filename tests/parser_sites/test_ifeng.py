"""Tests for IfengParser — DOM noise removal + readability fallback."""
import pytest
from pathlib import Path
from news.parser.sites.ifeng import IfengParser


FIXTURES = Path(__file__).parent / "fixtures"


class TestIfengParserPreprocess:
    """Test the _preprocess method directly."""

    def test_removes_low_brower_box(self):
        html = '<div id="lowBrowerBoxFixed"><p>upgrade browser</p></div><div id="content"><p>正文</p></div>'
        parser = IfengParser()
        result = parser._preprocess(html, "")
        assert 'lowBrowerBoxFixed' not in result
        assert '正文' in result

    def test_removes_index_info_divs(self):
        html = '<div class="index_info_article"><p>meta info</p></div><div id="content"><p>正文</p></div>'
        parser = IfengParser()
        result = parser._preprocess(html, "")
        assert 'index_info_' not in result
        assert '正文' in result

    def test_removes_index_devide_divs(self):
        html = '<div class="index_devide_line"><p>---</p></div><div id="content"><p>正文</p></div>'
        parser = IfengParser()
        result = parser._preprocess(html, "")
        assert 'index_devide_' not in result
        assert '正文' in result

    def test_removes_index_copyright_divs(self):
        html = '<div class="index_copyRight_text"><p>copyright</p></div><div id="content"><p>正文</p></div>'
        parser = IfengParser()
        result = parser._preprocess(html, "")
        assert 'index_copyRight_' not in result
        assert '正文' in result

    def test_removes_all_noise_together(self):
        html = (
            '<html><body>'
            '<div id="lowBrowerBoxFixed">upgrade</div>'
            '<div class="index_info_meta">avatar source date</div>'
            '<div class="index_devide_bar">---</div>'
            '<div class="index_copyRight_footer">copyright 2026</div>'
            '<div id="article"><p>正文内容 here</p></div>'
            '</body></html>'
        )
        parser = IfengParser()
        result = parser._preprocess(html, "")
        assert 'lowBrowerBoxFixed' not in result
        assert 'index_info_' not in result
        assert 'index_devide_' not in result
        assert 'index_copyRight_' not in result
        assert '正文内容' in result

    def test_returns_html_unchanged_when_no_ifeng_noise(self):
        html = '<html><body><div><p>普通内容</p></div></body></html>'
        parser = IfengParser()
        result = parser._preprocess(html, "")
        assert result is html  # same object — no copy

    def test_returns_html_unchanged_on_malformed_input(self):
        parser = IfengParser()
        result = parser._preprocess("not valid html at all <<<>>>", "")
        assert result == "not valid html at all <<<>>>"


class TestIfengParserFixtures:
    """Test IfengParser against a real ifeng.com article fixture."""

    def test_extracts_content_from_real_fixture(self):
        html = (FIXTURES / "ifeng.html").read_text(encoding="utf-8")
        parser = IfengParser()
        result = parser.parse(html, url="https://finance.ifeng.com/")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]
        # Noise elements should be removed by _preprocess
        assert 'lowBrowerBoxFixed' not in result["markdown"]

    def test_returns_none_for_unrelated_html(self):
        parser = IfengParser()
        result = parser.parse("<html><body><p>no article here</p></body></html>")
        assert result is None
