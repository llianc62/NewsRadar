"""IthomeParser — IT之家 (ithome.com) HTML → Markdown 解析."""

from __future__ import annotations

import re
import html as _html

from typing import Any, Dict, Optional

from lxml import html as lxml_html
from markdownify import markdownify as _md

from news.parser.parser import HtmlParser


class IthomeParser(HtmlParser):
    """IT之家解析器 — 从 #paragraph 容器提取正文后转 Markdown。

    IT之家文章页结构简洁：标题在 H1/h2 tag，正文在
    ``<div id="paragraph">`` 内由 ``<p>`` 标签包裹，
    评论区和侧边栏在正文容器之后。直接提取 #paragraph
    比走 readability 更干净。

    图片使用 ``data-original`` / ``srcset`` 懒加载，
    ``src`` 为透明占位符，在 _preprocess 中修复。
    """

    # 懒加载图片属性优先级：srcset(2x) > data-original
    _LAZY_IMAGE_ATTRS = ("srcset", "data-original")

    def _preprocess(self, html: str, url: str) -> str:
        """修复懒加载图片：将 data-original/srcset 写入 src。"""
        try:
            tree = lxml_html.fromstring(html)
        except Exception:
            return html

        fixed = self._fix_lazy_images(tree)
        return (
            lxml_html.tostring(tree, encoding="unicode")
            if fixed > 0
            else html
        )

    @classmethod
    def _fix_lazy_images(cls, tree: lxml_html.HtmlElement) -> int:
        """将懒加载属性转为 ``src``，返回修改的 ``<img>`` 数量。"""
        count = 0
        for img in tree.xpath("//img"):
            real_src = ""
            for attr in cls._LAZY_IMAGE_ATTRS:
                raw = img.get(attr, "")
                if raw:
                    # srcset: "https://img.example.com/foo.jpg 2x" → URL
                    real_src = raw.split()[0] if attr == "srcset" else raw
                    break
            if real_src and not real_src.startswith("data:"):
                img.set("src", real_src)
                count += 1
        return count

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        # 1. Extract content from #paragraph div — use lxml cssselect
        #    (regex with non-greedy (.*?)</div> truncates at nested <div>s)
        try:
            tree = lxml_html.fromstring(html)
        except Exception:
            return None

        p_divs = tree.cssselect("#paragraph")
        if not p_divs:
            return None

        content_html = lxml_html.tostring(
            p_divs[0], encoding="unicode", method="html"
        )

        # 2. Convert to markdown
        markdown = _md(
            content_html,
            heading_style="ATX",
            strip=["script", "style"],
            escape_asterisks=False,
            escape_underscores=False,
        )

        if not markdown or len(markdown.strip()) <= 50:
            return None

        markdown = self._beautify_markdown_formatting(markdown).strip()

        # 3. Title: prefer og:title → <title>
        title = self._extract_title_from_html(html)
        if not title:
            title_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
            if title_match:
                title = _html.unescape(
                    re.sub(r'<[^>]+>', '', title_match.group(1))
                ).strip()

        # 4. Metadata from page
        author = self._extract_meta(html, r'name=["\']author["\']')
        summary = self._extract_meta(
            html, r'name=["\']description["\']'
        ) or self._extract_meta(
            html, r'property=["\']og:description["\']'
        )
        published_at = self._extract_meta(
            html, r'property=["\']article:published_time["\']'
        )

        return self._build_result(
            markdown=markdown,
            title=title,
            author=author,
            published_at=published_at,
            summary=summary,
        )
