"""IfengParser — 凤凰网 (ifeng.com) HTML → Markdown 解析."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from lxml import html as lxml_html

from news.parser.parser import HtmlParser


class IfengParser(HtmlParser):
    """凤凰网解析器 — DOM 预处理后走 readability 降级链。

    凤凰网文章页在正文前后插入了大量 UI 噪声：
    - #lowBrowerBoxFixed（浏览器升级提示）
    - .index_info_*（头像、来源名、日期、分享按钮）
    - .index_devide_*（分隔线）
    - .index_copyRight_*（版权信息/页脚）

    正文图片使用 ``data-lazyload`` 懒加载，
    ``src`` 为 1×1 base64 占位符，必须在 readability 之前替换。
    """

    _LAZY_IMAGE_ATTRS = ("data-lazyload", "data-src", "data-original")

    # XPath 噪声移除列表 — (xpath, 说明)
    _NOISE_XPATHS = (
        ("//*[@id='lowBrowerBoxFixed']", "browser upgrade prompt"),
        ("//div[contains(@class, 'index_info_')]", "meta info bar"),
        ("//div[contains(@class, 'index_devide_')]", "divider"),
        ("//div[contains(@class, 'index_copyRight_')]", "copyright footer"),
    )

    # ── _preprocess ─────────────────────────────────────────────────

    def _preprocess(self, html: str, url: str) -> str:
        """Remove ifeng-specific template noise and fix lazy images.

        先删除 ``<meta name="keywords">``（40+ 个泛化标签每篇几乎相同），
        再走 lxml DOM 清理流程。
        """
        # 移除站点全局 meta keywords 标签，让 Jieba 从正文提取
        html = re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
        try:
            tree = lxml_html.fromstring(html)
        except Exception:
            return html

        removed = self._fix_lazy_images(tree)
        for xpath, _desc in self._NOISE_XPATHS:
            removed += self._remove_elements(tree, xpath)

        return (
            lxml_html.tostring(tree, encoding="unicode")
            if removed > 0
            else html
        )

    # ── helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _remove_elements(tree: lxml_html.HtmlElement, xpath: str) -> int:
        """删除匹配 *xpath* 的所有元素，返回删除数量。"""
        count = 0
        for el in tree.xpath(xpath):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                count += 1
        return count

    @classmethod
    def _fix_lazy_images(cls, tree: lxml_html.HtmlElement) -> int:
        """将懒加载属性转为 ``src``，返回修改的 ``<img>`` 数量。"""
        count = 0
        for img in tree.xpath("//img"):
            for attr in cls._LAZY_IMAGE_ATTRS:
                real_src = img.get(attr, "")
                if real_src and not real_src.startswith("data:"):
                    img.set("src", real_src)
                    count += 1
                    break
        return count
