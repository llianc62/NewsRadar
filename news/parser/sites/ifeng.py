"""IfengParser — 凤凰网 (ifeng.com) HTML → Markdown 解析."""

from __future__ import annotations

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

    这些 DOM 元素必须在 readability 提取之前删除。
    """

    def _preprocess(self, html: str, url: str) -> str:
        """Remove ifeng-specific template noise from HTML before extraction."""
        try:
            tree = lxml_html.fromstring(html)
        except Exception:
            return html

        removed = False

        # Browser upgrade prompt at page bottom
        for el in tree.xpath("//*[@id='lowBrowerBoxFixed']"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed = True

        # Meta info bar: avatar, source name, "独家抢先看", date, share btns
        for el in tree.xpath("//div[contains(@class, 'index_info_')]"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed = True

        # Divider between meta bar and article body
        for el in tree.xpath("//div[contains(@class, 'index_devide_')]"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed = True

        # Copyright / footer at bottom of article
        for el in tree.xpath("//div[contains(@class, 'index_copyRight_')]"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed = True

        if removed:
            return lxml_html.tostring(tree, encoding="unicode")
        return html
