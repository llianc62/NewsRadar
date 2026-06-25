"""HuanqiuParser — 环球网 HTML → Markdown 解析。

环球网文章页面将正文放置在 ``<textarea class="article-content">`` 中，
readability-lxml 无法解析 textarea 内的 HTML，返回空文档后落入 fallback
管线，导致页面所有导航、推荐、广告等噪音混入输出。

本 Parser 在预处理阶段提取 textarea 内正文，构建干净的 HTML 文档，
之后走通用 readability 管线即可获得干净的 Markdown 输出。
"""

from __future__ import annotations

import html as _html
import re


from news.parser.parser import HtmlParser


class HuanqiuParser(HtmlParser):
    """环球网解析器 — 提取 textarea 内正文后走通用 readability 管线。

    环球网文章使用 ``<textarea class="article-content">`` 存放正文 HTML，
    ``<textarea class="article-title">`` 存放标题。通用 readability-lxml
    将 textarea 视为纯文本节点，无法解析其内的 HTML 标签，导致提取失败。

    _preprocess 提取 textarea 内容构建干净的 HTML 文档，_extract 负责
    处理 content 提取失败的边界情况。
    """

    _CONTENT_RE = re.compile(
        r'<textarea\s+class="article-content">(.*?)</textarea>',
        re.DOTALL,
    )

    _TITLE_RE = re.compile(
        r'<textarea\s+class="article-title">(.*?)</textarea>',
        re.DOTALL,
    )

    def _preprocess(self, html: str, url: str) -> str:
        """提取 textarea 内正文，构建干净 HTML 文档。"""
        content = ""
        m = self._CONTENT_RE.search(html)
        if m:
            content = m.group(1)

        title = ""
        m = self._TITLE_RE.search(html)
        if m:
            title = _html.unescape(m.group(1).strip())

        # Build a clean document for readability
        if title:
            return f"<html><head><title>{title}</title></head><body>{content}</body></html>"
        return f"<html><body>{content}</body></html>"
