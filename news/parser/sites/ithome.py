"""IthomeParser — IT之家 (ithome.com) HTML → Markdown 解析."""

from __future__ import annotations

from news.parser.parser import HtmlParser


class IthomeParser(HtmlParser):
    """IT之家解析器 — 标准 HTML 页面，readability 降级链直接覆盖。"""
