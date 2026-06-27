"""Tests for XinhuaParser — credits stripping + image-only detection."""

import re
from pathlib import Path

import pytest

from news.parser.sites.xinhua import XinhuaParser

FIXTURES = Path(__file__).parent / "fixtures"


class TestXinhuaCreditsStripping:
    """_preprocess removes production-team <p> tags from the end of articles."""

    def test_strips_role_credits_from_html(self):
        parser = XinhuaParser()
        html = (
            "<div id='detail'>"
            "<p>正文第一段</p>"
            "<p>正文第二段</p>"
            "<p>　　策划：孙承斌 李拯宇</p>"
            "<p>　　监制：孙志平</p>"
            "<p>　　制片：张平锋</p>"
            "<p>　　统筹：李杰</p>"
            "<p>　　编导：李佳琳 牛小溪</p>"
            "<p>　　记者：何山 张梦洁</p>"
            "<p>　　配音：王帅龙</p>"
            "<p>　　新华社音视频部制作</p>"
            "<p>　　新华通讯社出品</p>"
            "</div>"
        )
        cleaned = parser._preprocess(html, "http://example.com/")
        # Body paragraphs preserved
        assert "正文第一段" in cleaned
        assert "正文第二段" in cleaned
        # Credits stripped
        assert "策划" not in cleaned
        assert "监制" not in cleaned
        assert "新华社音视频部制作" not in cleaned
        assert "新华通讯社出品" not in cleaned

    def test_strips_variant_credit_formats(self):
        """Test various credit role formats: 总策划, 出品人, 制片人, etc."""
        parser = XinhuaParser()
        html = (
            "<div>"
            "<p>正文内容</p>"
            "<p>　　总策划：袁炳忠</p>"
            "<p>　　出品人：孙志平、张浩</p>"
            "<p>　　制片人：米立公、吴中敏</p>"
            "<p>　　报道员：古鲁 瓦扬</p>"
            "<p>　　鸣谢：中共丽江市委宣传部</p>"
            "<p>　　新华社音视频部制作</p>"
            "<p>　　新华通讯社出品</p>"
            "</div>"
        )
        cleaned = parser._preprocess(html, "http://example.com/")
        assert "正文内容" in cleaned
        assert "总策划" not in cleaned
        assert "出品人" not in cleaned
        assert "制片人" not in cleaned
        assert "报道员" not in cleaned
        assert "鸣谢" not in cleaned
        assert "新华社音视频部制作" not in cleaned
        assert "新华通讯社出品" not in cleaned

    def test_does_not_strip_legitimate_content(self):
        """The regex must NOT match body paragraphs that mention credits roles
        in passing (e.g. 记者了解到…)."""
        parser = XinhuaParser()
        html = (
            "<div>"
            "<p>据记者了解，事发后相关部门已介入调查</p>"
            "<p>本次策划活动取得了圆满成功</p>"
            "<p>监制环节需要严格把关</p>"
            "</div>"
        )
        cleaned = parser._preprocess(html, "http://example.com/")
        # These contain credit keywords but in narrative context — keep them
        assert "据记者了解" in cleaned
        assert "本次策划活动" in cleaned
        assert "监制环节" in cleaned

    def test_no_credits_html_unchanged_except_fjtignoreurl(self):
        parser = XinhuaParser()
        html = (
            "<div id='detail'>"
            "<p>普通文章段落一</p>"
            "<p>普通文章段落二</p>"
            "</div>"
        )
        cleaned = parser._preprocess(html, "http://example.com/")
        # fjtignoreurl stripping still works (no-op here)
        assert "普通文章段落一" in cleaned
        assert "普通文章段落二" in cleaned

    def test_strips_fjtignoreurl_and_credits_together(self):
        parser = XinhuaParser()
        html = (
            "<fjtignoreurl>"
            "<div id='detail'>"
            "<p>正文内容</p>"
            "<p>策划：孙承斌</p>"
            "<p>新华通讯社出品</p>"
            "</div>"
            "</fjtignoreurl>"
        )
        cleaned = parser._preprocess(html, "http://example.com/")
        assert "正文内容" in cleaned
        assert "fjtignoreurl" not in cleaned
        assert "策划" not in cleaned
        assert "新华通讯社出品" not in cleaned

    def test_strips_new_role_keywords(self):
        """编辑, 摄影, 剪辑, 文字, 视频 — added after article 11298."""
        parser = XinhuaParser()
        html = (
            "<div>"
            "<p>正文内容</p>"
            "<p>　　编辑：沈浩洋 陈杉</p>"
            "<p>　　摄影：王海洲</p>"
            "<p>　　剪辑：张三</p>"
            "<p>　　文字：李四</p>"
            "<p>　　视频：王五</p>"
            "<p>　　新华社国内部出品</p>"
            "</div>"
        )
        cleaned = parser._preprocess(html, "http://example.com/")
        assert "正文内容" in cleaned
        assert "编辑" not in cleaned
        assert "摄影" not in cleaned
        assert "剪辑" not in cleaned
        assert "文字" not in cleaned
        assert "视频" not in cleaned
        assert "新华社国内部出品" not in cleaned

    def test_strips_varied_production_signatures(self):
        """Various 新华社…出品/制作 patterns including full-name form."""
        parser = XinhuaParser()
        html = (
            "<div>"
            "<p>正文内容</p>"
            "<p>　　新华社音视频部制作</p>"
            "<p>　　新华社国际传播融合平台出品</p>"
            "<p>　　新华社国内部、国际部联合制作</p>"
            "<p>　　新华社第一工作室出品</p>"
            "<p>　　新华通讯社出品</p>"
            "</div>"
        )
        cleaned = parser._preprocess(html, "http://example.com/")
        assert "正文内容" in cleaned
        for sig in ["新华社音视频部制作", "新华社国际传播融合平台出品",
                     "新华社国内部、国际部联合制作", "新华社第一工作室出品",
                     "新华通讯社出品"]:
            assert sig not in cleaned, f"'{sig}' should be stripped"

    def test_does_not_strip_narrative_xinhua_paragraphs(self):
        """Long 新华社…制作 paragraphs are NOT credits — must be preserved."""
        parser = XinhuaParser()
        html = (
            "<div>"
            "<p>正文内容</p>"
            "<p>新华社记者走访了这家中华老字号，了解其茉莉花茶制作工艺与传承</p>"
            "</div>"
        )
        cleaned = parser._preprocess(html, "http://example.com/")
        assert "新华社记者走访" in cleaned
        assert "茉莉花茶制作" in cleaned

    def test_strips_wxsharepic_image(self):
        parser = XinhuaParser()
        html = (
            "<div>"
            "<p>正文内容</p>"
            '<img id="wxsharepic" title="微信分享图" style="DISPLAY: none" src="share.jpg" />'
            "</div>"
        )
        cleaned = parser._preprocess(html, "http://example.com/")
        assert "wxsharepic" not in cleaned
        assert "微信分享图" not in cleaned
        assert "正文内容" in cleaned


class TestXinhuaParserEndToEnd:
    """End-to-end parse() tests using a real HTML fixture."""

    def test_parse_fixture_no_credits_in_output(self):
        html = (FIXTURES / "xinhua.html").read_text(encoding="utf-8")
        parser = XinhuaParser()
        result = parser.parse(html, url="http://www.news.cn/politics/2022-12/14/c_1129207254.htm")
        assert result is not None, "parse() should return a result"
        content = result["markdown"]
        # Credits must be absent
        assert "策划：" not in content
        assert "新华通讯社出品" not in content
        assert "新华社音视频部制作" not in content
        # Body content is preserved
        assert "新时代的中国" in content
        assert "快递业务量" in content

    def test_parse_fixture_preserves_title(self):
        html = (FIXTURES / "xinhua.html").read_text(encoding="utf-8")
        parser = XinhuaParser()
        result = parser.parse(html, url="http://www.news.cn/politics/2022-12/14/c_1129207254.htm")
        assert result is not None
        assert result["title"] == "微视频｜“新”在中国" or "微视频" in result["title"]

    def test_parse_empty_html_returns_none(self):
        parser = XinhuaParser()
        assert parser.parse("") is None
