"""IthomeParser — IT之家 (ithome.com) HTML → Markdown 解析."""

from __future__ import annotations

from lxml import html as lxml_html

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

    def _extract(self, html: str, url: str = "") -> tuple[str, dict]:
        """从 #paragraph 容器提取正文 HTML。"""
        try:
            tree = lxml_html.fromstring(html)
        except Exception:
            return html, {}

        p_divs = tree.cssselect("#paragraph")
        if not p_divs:
            return html, {}

        content_html = lxml_html.tostring(
            p_divs[0], encoding="unicode", method="html"
        )
        return content_html, {}
