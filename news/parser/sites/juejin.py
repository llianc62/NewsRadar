"""JuejinParser — 稀土掘金 (juejin.cn) HTML → Markdown 解析."""

from __future__ import annotations

import re

from news.parser.parser import HtmlParser


class JuejinParser(HtmlParser):
    """稀土掘金解析器 — _preprocess 中移除 meta keywords 脏数据。

    掘金每篇文章的 ``<meta name="keywords">`` 都包含 13 个站点全局标签
    （"前端开发社区,JavaScript,CSS,HTML5,..."），每篇完全相同。
    删除该 meta 让 Jieba 从正文提取。
    """

    def _preprocess(self, html: str, url: str) -> str:
        return re.sub(
            r'<meta[^>]+name=["\']keywords["\'][^>]*/?>',
            '',
            html,
            flags=re.I,
        )
