"""FastbullParser — 法布财经 (fastbull.com) HTML → Markdown 解析."""

from __future__ import annotations

from news.parser.parser import HtmlParser


class FastbullParser(HtmlParser):
    """法布财经解析器 — 标准 HTML 页面，readability 降级链直接覆盖。"""
