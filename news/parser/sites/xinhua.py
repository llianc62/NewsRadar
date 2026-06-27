"""XinhuaParser — 新华网 HTML → Markdown 解析。

新华网部分文章模板使用 ``<fjtignoreurl>`` 自定义标签包裹正文，
readability-lxml 不认识该标签，会将正文整块丢弃。另有部分文章
以纯图片形式发布（海报/长图），正文区域无文字。

视频/图文稿末尾常附带制作团队信息（策划/监制/记者等角色署名 +
"新华社音视频部制作" / "新华通讯社出品"），在预处理阶段剥离。
"""

from __future__ import annotations

import re

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

    # Credits/production-team <p> tags at the end of video/political
    # articles.  Matches both role-with-colon lines
    # （策划：… / 总策划：… / 记者：… / 编辑：… / 摄影：… etc.）and
    # terminal production lines（新华社音视频部制作 / 新华社国际传播
    # 融合平台出品 / 新华通讯社出品 …）.
    #
    # NOTE: 新华社 (简称) ≠ 新华通讯社 (全称)，两者都需要单独匹配.
    _CREDITS_RE = re.compile(
        r"<p[^>]*>\s*(?:"
        # Role credits: 角色：名字
        r"(?:总)?(?:策划|监制|出品人?|制片人?|统筹|编导|"
        r"记者|配音|摄制|包装|海报|报道员|鸣谢|"
        r"编辑|摄影|剪辑|文字|视频)"
        r"\s*[：:]\s*[^<]+"
        r"|"
        # Production signatures — abbreviated form: 新华社…制作 / 新华社…出品
        # (limit content between to ≤ 50 chars to avoid matching
        #  narrative sentences like "新华社记者走访…了解其茉莉花茶制作")
        r"新华社.{1,50}?(?:出品|制作)"
        r"|"
        # Production signatures — full name form: 新华通讯社出品
        r"新华通讯社出品"
        r")</p>",
        re.IGNORECASE,
    )

    # Trailing "微信分享图" image — a hidden <img id="wxsharepic">
    # appended at the very end of many Xinhua articles.
    _WXSHAREPIC_RE = re.compile(
        r'<img[^>]*\bid\s*=\s*["\']wxsharepic["\'][^>]*/?>',
        re.IGNORECASE,
    )

    def _preprocess(self, html: str, url: str) -> str:
        html = self._FJTIGNOREURL_RE.sub("", html)
        html = self._CREDITS_RE.sub("", html)
        html = self._WXSHAREPIC_RE.sub("", html)
        return html

    def _extract(self, html: str, url: str = "") -> tuple[str, dict]:
        """检测纯图片文章，提取内容图片；正常文章返回原始 HTML 走通用流程。"""
        m = self._CONTENT_IMG_RE.search(html)
        if not m:
            return html, {}

        # Check if div#detail has meaningful text or is just an image
        detail_start = html.find('id="detail"')
        detail_end = html.find('id="articleEdit"', detail_start) if detail_start >= 0 else -1
        if detail_start < 0 or detail_end < 0:
            return html, {}

        detail_html = html[detail_start:detail_end]
        text = re.sub(r"<[^>]+>", "", detail_html).strip()
        if len(text) >= 50:
            return html, {}  # Has text — let readability handle it

        # Image-only article (poster / long image) — return the image as HTML
        img_src = m.group(1)
        if not img_src.startswith("http"):
            from urllib.parse import urljoin
            img_src = urljoin(url, img_src) if url else img_src

        title = self._extract_title_from_html(html)
        return f'<p><img src="{img_src}"/></p>', {"title": title}
