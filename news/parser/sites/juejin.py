"""JuejinParser — 稀土掘金 (juejin.cn) HTML → Markdown 解析."""

from __future__ import annotations

from news.parser.parser import HtmlParser


class JuejinParser(HtmlParser):
    """稀土掘金解析器 — 标准 HTML 页面，readability 降级链直接覆盖。"""
