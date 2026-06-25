"""GelonghuiParser — 格隆汇 HTML → Markdown 解析。

格隆汇文章 HTML 存在以下问题导致 markdown 格式异常：

1. **加粗/斜体标签碎片化** — 每个词甚至每个字单独用 ``<b>``/``<i>`` 包裹，
   相邻标签之间无间距，markdownify 为每个标签独立输出 ``**...**``，
   产生 ``**6** **-** **13岁**`` 式碎片。

2. **标题编号与正文分离** — 章节编号 ``01`` 在 ``<h3>`` 中，
   标题文字在后续 ``<p>`` 中，且中间夹有空 ``<strong><br>`` 节点。

3. **嵌套 ``<b><i>`` 标签** — markdownify 对 ``<b><i>text</i></b>``
   的 ``***`` 处理有缺陷，相邻 ``<i>`` 标签加重问题。

预处理阶段合并相邻标签、清理空节点、合并标题，
之后走通用 readability 管线即可获得格式正确的 Markdown。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from news.parser.parser import HtmlParser


# Regex for empty <strong><span><font><br>... blocks
_EMPTY_STRONG_BR = (
    r'<strong[^>]*>\s*<span[^>]*>\s*<font[^>]*>\s*<br\s*/?>\s*'
    r'</font>\s*</span>\s*</strong>'
)


class GelonghuiParser(HtmlParser):
    """格隆汇解析器 — 修复碎片化加粗/斜体标签后走通用 readability 管线。"""

    def _preprocess(self, html: str, url: str) -> str:
        """清理 HTML：合并相邻标签，移除空节点，合并分离的标题。"""
        # 1. 合并相邻的同类型标签（字符级碎片化的根源）
        html = re.sub(r'</b>\s*<b[^>]*>', '', html)
        html = re.sub(r'</strong>\s*<strong[^>]*>', '', html)
        html = re.sub(r'</i>\s*<i[^>]*>', '', html)

        # 2. 移除空 <p></p>
        html = re.sub(r'<p[^>]*>\s*</p>', '', html)

        # 3. 移除仅含 <strong><span><font><br>... 的空标题和空段落
        html = re.sub(
            r'<h3[^>]*>\s*' + _EMPTY_STRONG_BR + r'\s*</h3>',
            '', html, flags=re.DOTALL,
        )
        html = re.sub(
            r'<p[^>]*>\s*' + _EMPTY_STRONG_BR + r'\s*</p>',
            '', html, flags=re.DOTALL,
        )

        # 4. 解包 <h3> 内的 <strong>（避免 markdownify 输出 ###**01**）
        html = re.sub(
            r'(<h3[^>]*>)\s*<strong[^>]*>(.*?)</strong>\s*</h3>',
            r'\1\2</h3>', html, flags=re.DOTALL,
        )

        # 5. 合并分离的标题编号和文字：<h3>01</h3><p>标题文字</p> → <h3>01 标题文字</h3>
        def _merge_heading(m: re.Match) -> str:
            num = m.group(1).strip()
            text = m.group(2).strip()
            return f'<h3>{num} {text}</h3>'

        html = re.sub(
            r'<h3[^>]*>(.*?)</h3>\s*<p[^>]*>(.*?)</p>',
            _merge_heading, html, flags=re.DOTALL,
        )

        return html

    def _extract(self, html: str, url: str = "") -> Optional[Dict[str, Any]]:
        """走通用 readability 管线，并对 markdown 做后处理。"""
        result = self._extract_with_readability(html, url)
        if result is None:
            return None

        md = result["markdown"]

        # 6. 合并被拆分的标题行：### NN\n\n标题文字 → ### NN 标题文字
        md = re.sub(
            r'^(###\s+\d{1,2})\s*\n+\s*(?!#)(.+?)$',
            r'\1 \2',
            md,
            flags=re.MULTILINE,
        )

        # 7. 修复 _handle_markdown_bold 对 *** 标记的误加空格
        #    * **text → ***text（粗斜体开启）
        #    text** * → text***（粗斜体关闭）
        md = re.sub(r'\*\s+\*\*(?!\*)', '***', md)
        md = re.sub(r'\*\*\s+\*(?!\*)', '***', md)

        result["markdown"] = md
        return result
