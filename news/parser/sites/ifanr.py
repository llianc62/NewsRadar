"""IfanrParser — 爱范儿 (ifanr.com) HTML → Markdown 解析.

ifanr 页面结构清晰，正文统一在 ``<article>`` 容器内，其余为站点导航、
评论、页脚等噪音。预处理阶段直接提取 article 内容，消除噪音后再走
通用 readability 管线。
"""

from __future__ import annotations

from lxml import html as lxml_html

from news.parser.parser import HtmlParser

# 爱范儿正文容器 — 所有文章类型（早报、视频、普通）共用
_ARTICLE_SELECTOR = (
    "article.o-single-content__body__content."
    "c-article-content.s-single-article.js-article"
)


class IfanrParser(HtmlParser):
    """爱范儿解析器 — 预处理提取 article 正文，走通用降级链。"""

    def _preprocess(self, html: str, url: str) -> str:
        """提取 ``<article>`` 正文容器，剔除站点导航 / 页脚 / 评论等噪音。

        若找不到匹配的 article 元素，返回原始 HTML 让 readability 自行判断。
        """
        try:
            tree = lxml_html.fromstring(html)
        except Exception:
            return html
        articles = tree.cssselect(_ARTICLE_SELECTOR)
        if not articles:
            return html
        return lxml_html.tostring(articles[0], encoding="unicode")
