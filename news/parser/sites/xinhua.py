"""XinhuaParser — 新华网 HTML → Markdown 解析。

新华网部分文章模板使用 ``<fjtignoreurl>`` 自定义标签包裹正文，
readability-lxml 不认识该标签，会将正文整块丢弃。另有部分文章
以纯图片形式发布（海报/长图），正文区域无文字。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from news.parser.parser import HtmlParser


class XinhuaParser(HtmlParser):
    """新华网解析器 — 剥离 CMS 包装标签后走通用 readability 管线。

    新华网 CMS 会输出 ``<fjtignoreurl>`` 标签包裹 ``<div id="detail">``
    正文区域。该标签语义不明，readability-lxml 视为无效节点，导致正文
    丢失，仅提取到跟踪像素。本 Parser 在预处理阶段剥离该标签。

    对纯图片文章（``<div id="detail">`` 内仅含 ``<img>`` 无文字），
    readability 无法提取文字内容，Parser 将内容图转为 Markdown 图片输出。
    """

    _FJTIGNOREURL_RE = re.compile(
        r"</?fjtignoreurl\b[^>]*>",
        re.IGNORECASE,
    )

    # Matches the main content image inside div#detail
    _CONTENT_IMG_RE = re.compile(
        r'<div\s+id="detail"[^>]*>.*?'
        r'<img[^>]*\bsrc\s*=\s*["\']([^"\']+\.(?:jpg|jpeg|png|gif|webp))["\']',
        re.IGNORECASE | re.DOTALL,
    )

    # Garbage patterns — if readability output matches these, it failed
    _GARBAGE_RE = re.compile(
        r"^(?:\d+\s*)+$",  # pure numbers with whitespace
        re.MULTILINE,
    )

    def _preprocess(self, html: str, url: str) -> str:
        html = self._FJTIGNOREURL_RE.sub("", html)
        return html

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        """检测纯图片文章，提取内容图片；正常文章返回 None 走降级链。"""
        m = self._CONTENT_IMG_RE.search(html)
        if not m:
            return None

        # Check if div#detail has meaningful text or is just an image
        detail_start = html.find('id="detail"')
        detail_end = html.find('id="articleEdit"', detail_start) if detail_start >= 0 else -1
        if detail_start < 0 or detail_end < 0:
            return None

        detail_html = html[detail_start:detail_end]
        # Strip all HTML tags, count remaining text
        text = re.sub(r"<[^>]+>", "", detail_html).strip()
        if len(text) >= 50:
            return None  # Has text — let readability handle it

        # Image-only article (poster / long image) — return the image as content
        img_src = m.group(1)
        # Resolve relative URL
        if not img_src.startswith("http"):
            from urllib.parse import urljoin
            img_src = urljoin(url, img_src) if url else img_src

        title = self._extract_title_from_html(html)
        return self._build_result(
            markdown=f"![]({img_src})",
            title=title,
        )
