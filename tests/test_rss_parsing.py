"""RSS 源文章正文解析测试 — 使用生产代码路径逐个站点验证。

与生产管线一致：
- 使用 ``registry.parse()`` 路由到站点专用 Parser
- 应用 ``_hook_response_encoding`` 修复编码检测

Usage:
    python -m pytest tests/test_rss_parsing.py -v -s
    python tests/test_rss_parsing.py              # 直接运行
    python tests/test_rss_parsing.py xinhua        # 测试指定源
"""

from __future__ import annotations

import re
import sys
import requests

from news.parser.registry import registry
from news.fetcher.rss import RSSParser


# ═══════════════════════════════════════════════════════════════════
# Markdown quality checker — detects common output defects
# ═══════════════════════════════════════════════════════════════════

class MarkdownQualityChecker:
    """Detect common defects in parser-generated markdown output."""

    # HTML tags that should never survive markdownify/readability extraction
    _HTML_TAG_RE = re.compile(
        r"</?(?:p|div|span|a|ul|ol|li|table|tr|td|th|br|hr|font"
        r"|b|i|u|strong|em|section|article|header|footer|nav"
        r"|aside|main|form|input|button|select|option|textarea"
        r"|iframe|video|audio|canvas|svg)\b[^>]*>",
        re.IGNORECASE,
    )

    # Patterns that indicate WAF/CAPTCHA page instead of real content
    _WAF_PATTERNS = [
        (re.compile(r"acw_tc", re.IGNORECASE), "阿里云 WAF cookie"),
        (re.compile(r"<script[^>]*>.*?var\s+\w+\s*=\s*\{"), "混淆 JS 挑战"),
        (re.compile(r"验证码|captcha|challenge", re.IGNORECASE), "验证码页面"),
        (re.compile(r"请启用\s*Javascript|please\s+enable\s+Javascript", re.IGNORECASE),
         "JS 拦截页"),
        (re.compile(r"Access\s*Denied|Request\s*Blocked|403\s*Forbidden",
                    re.IGNORECASE), "访问拒绝"),
    ]

    # Signs of encoding corruption (double-encoding, charset mismatch)
    _ENCODING_ARTIFACT_RE = re.compile(
        r"[�]"  # Unicode replacement character
        r"|Ã[\\xa0-\\xff]"  # Typical UTF-8→Latin-1 artifact (e.g. "Ã§")
        r"|Ã¯Â¿Â½"  # Triple-encoded BOM
        r"|ï¿½"  # Double-encoded BOM
        r"|â[¦]"  # Smart-quote / dash mojibake patterns
        r"|[äëïöüáéíóú]\\w*[äëïöüáéíóú]\\w*[äëïöüáéíóú]"  # Dense accented chars (CJK double-encoded)
    )

    # script / style tags that should never appear in markdown
    _SCRIPT_STYLE_RE = re.compile(
        r"<(script|style)\b[^>]*>.*?</\1>|<script\b[^>]*/>",
        re.IGNORECASE | re.DOTALL,
    )

    # Consecutive blank lines (wasteful)
    _EXCESSIVE_BLANK_RE = re.compile(r"\n{4,}")

    # Empty or broken markdown links  [text]()
    _BROKEN_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*\)")

    @staticmethod
    def _cjk_ratio(text: str) -> float:
        """Proportion of CJK characters in text."""
        if not text:
            return 0.0
        count = sum(1 for ch in text if "一" <= ch <= "鿿"
                    or "㐀" <= ch <= "䶿"
                    or "豈" <= ch <= "﫿")
        return count / len(text)

    @staticmethod
    def _latin1_ratio(text: str) -> float:
        """Proportion of Latin-1 Supplement chars (U+00C0–U+00FF).

        High values alongside low CJK ratio for a Chinese source
        strongly indicate UTF-8 bytes misinterpreted as Latin-1.
        """
        if not text:
            return 0.0
        count = sum(1 for ch in text if "À" <= ch <= "ÿ")
        return count / len(text)

    @classmethod
    def check(cls, markdown: str, source_id: str = "") -> dict:
        """Run all quality checks, return dict of issues found.

        Returns:
            ``{"issues": [...], "score": 0-100}``
            Empty issues list + score 100 = clean output.
        """
        issues: list[str] = []

        # ── HTML tag remnant ──────────────────────────────────────
        html_hits = cls._HTML_TAG_RE.findall(markdown)
        if html_hits:
            # Show up to 3 unique examples
            uniq = list(dict.fromkeys(h[:60] for h in html_hits))[:3]
            issues.append(f"HTML 标签残留: {uniq}")

        # ── WAF / CAPTCHA ─────────────────────────────────────────
        for pattern, label in cls._WAF_PATTERNS:
            if pattern.search(markdown):
                issues.append(f"疑似 {label}")

        # ── Script / style ───────────────────────────────────────
        if cls._SCRIPT_STYLE_RE.search(markdown):
            issues.append("残留 <script>/<style> 标签")

        # ── Encoding artifacts ───────────────────────────────────
        if cls._ENCODING_ARTIFACT_RE.search(markdown):
            issues.append("疑似编码异常（乱码/双重编码 artifact）")

        # ── CJK / garbled ratio ──────────────────────────────────
        cjk_ratio = cls._cjk_ratio(markdown)
        latin1_ratio = cls._latin1_ratio(markdown)
        # Latin-1 density > 5% with low CJK: strong mojibake signal
        # (Chinese UTF-8 bytes interpreted as Latin-1 produce dense
        # accented/symbol chars like ä½ å¥½ è¿\x99...)
        if latin1_ratio > 0.05 and cjk_ratio < 0.02:
            issues.append(
                f"疑似编码异常（Latin-1 密度 {latin1_ratio:.1%}，CJK {cjk_ratio:.1%}）"
            )

        # ── Excessive blank lines ────────────────────────────────
        blank_runs = cls._EXCESSIVE_BLANK_RE.findall(markdown)
        if len(blank_runs) > 5:
            issues.append(f"过多连续空行 ({len(blank_runs)} 处)")

        # ── Broken markdown links ────────────────────────────────
        broken = cls._BROKEN_LINK_RE.findall(markdown)
        if len(broken) > 3:
            issues.append(f"空链接 [{len(broken)} 处]")

        # ── Score: 100 - penalty per issue ───────────────────────
        score = max(0, 100 - len(issues) * 20)

        return {"issues": issues, "score": score}


# ═══════════════════════════════════════════════════════════════════
# Response encoding hook — same as crawler.Crawler._hook_response_encoding
# ═══════════════════════════════════════════════════════════════════

def _hook_response_encoding(response, *args, **kwargs):
    """Response hook: correct encoding when the server omits charset.

    Some servers (e.g. people.com.cn) send ``Content-Type: text/html``
    without a charset directive and use the older
    ``<meta http-equiv="content-type" content="text/html;charset=UTF-8"/>``
    format that requests does not detect.  Without this hook requests
    defaults to ISO-8859-1 and ``resp.text`` produces mojibake.

    We detect the real encoding via ``apparent_encoding`` (chardet) and
    apply it before ``resp.text`` is ever accessed.
    """
    if response.encoding == "ISO-8859-1":
        response.encoding = response.apparent_encoding


# ═══════════════════════════════════════════════════════════════════
# All RSS feeds from config.yaml
# ═══════════════════════════════════════════════════════════════════

FEEDS = [
    # ═══ 官方媒体 ═══
    {
        "source_id": "xinhua-politics",
        "name": "新华社·时政",
        "feed_url": "http://www.xinhuanet.com/politics/news_politics.xml",
        "enabled": True,
    },
    {
        "source_id": "xinhua-finance",
        "name": "新华社·财经",
        "feed_url": "http://www.xinhuanet.com/fortune/news_fortune.xml",
        "enabled": True,
    },
    {
        "source_id": "xinhua-world",
        "name": "新华社·国际",
        "feed_url": "http://www.xinhuanet.com/world/news_world.xml",
        "enabled": True,
    },
    {
        "source_id": "people",
        "name": "人民日报",
        "feed_url": "https://rsshub.rssforever.com/people",
        "enabled": True,
    },
    {
        "source_id": "huanqiu",
        "name": "环球时报",
        "feed_url": "https://rsshub.rssforever.com/huanqiu/news",
        "enabled": True,
    },
    # ═══ 宏观经济/政策 ═══
    {
        "source_id": "yicai-news",
        "name": "第一财经",
        "feed_url": "https://rsshub.rssforever.com/yicai/news",
        "enabled": True,
    },
    {
        "source_id": "gelonghui-hot",
        "name": "格隆汇",
        "feed_url": "https://rsshub.rssforever.com/gelonghui/hot-article",
        "enabled": True,
    },
    # ═══ 市场快讯 ═══
    {
        "source_id": "jin10",
        "name": "金十数据",
        "feed_url": "https://rsshub.rssforever.com/jin10",
        "enabled": False,
    },
    # ═══ 金融资讯 ═══
    {
        "source_id": "stcn-hot",
        "name": "证券时报",
        "feed_url": "https://rsshub.rssforever.com/stcn/article/rank/yw",
        "enabled": True,
    },
    {
        "source_id": "jiemian-finance",
        "name": "界面财经",
        "feed_url": "https://rsshub.rssforever.com/jiemian/lists/1",
        "enabled": True,
    },
    # ═══ 股票信息 ═══
    {
        "source_id": "eastmoney-stock",
        "name": "东方财富·A股",
        "feed_url": "https://rsshub.rssforever.com/eastmoney/a",
        "enabled": False,
    },
    # ═══ 国际/世界格局 ═══
    {
        "source_id": "bloomberg",
        "name": "彭博社",
        "feed_url": "https://rsshub.rssforever.com/bloomberg",
        "enabled": False,
    },
    {
        "source_id": "bbc-world",
        "name": "BBC国际",
        "feed_url": "https://rsshub.rssforever.com/bbc/world",
        "enabled": False,
    },
    {
        "source_id": "cctv-world",
        "name": "央视国际",
        "feed_url": "https://rsshub.rssforever.com/cctv/world",
        "enabled": True,
    },
    {
        "source_id": "aljazeera-en",
        "name": "半岛电视台",
        "feed_url": "https://rsshub.rssforever.com/aljazeera/english",
        "enabled": False,
    },
    {
        "source_id": "yahoo-finance",
        "name": "雅虎财经",
        "feed_url": "https://finance.yahoo.com/news/rssindex",
        "enabled": False,
    },
    # ═══ 科技创投 ═══
    {
        "source_id": "36kr-news",
        "name": "36氪",
        "feed_url": "https://rsshub.rssforever.com/36kr/news/latest",
        "enabled": True,
    },
    {
        "source_id": "ifanr",
        "name": "爱范儿",
        "feed_url": "https://www.ifanr.com/feed",
        "enabled": True,
    },
]


def _create_session() -> requests.Session:
    """Create a requests Session matching the production crawler."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/91.0.4472.124 Safari/537.36"
        ),
        "Accept": (
            "application/feed+json, application/json, "
            "application/rss+xml, application/atom+xml, "
            "application/xml, text/xml, */*"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    session.hooks["response"].append(_hook_response_encoding)
    return session


def _summarize_tier(result: dict) -> str:
    """Guess which extraction tier produced the result."""
    md_len = len(result.get("markdown", ""))
    if md_len > 500:
        return "readability"
    elif md_len > 100:
        return "fallback"
    else:
        return "too_short"


def _run_single_feed(feed: dict, sample_count: int = 2) -> list[dict]:
    """Test a single RSS feed using the production code path.

    Flow: RSS feed → sample URLs → HTTP download → registry.parse()
    """
    source_id = feed["source_id"]
    name = feed["name"]
    feed_url = feed["feed_url"]
    enabled = feed["enabled"]

    status_icon = "✅" if enabled else "⏸️"
    print(f"\n{'='*60}")
    print(f"  {status_icon} {name} ({source_id})")
    print(f"  Feed: {feed_url}")
    print(f"{'='*60}")

    session = _create_session()
    rss_parser = RSSParser()

    # ── Step 1: Fetch RSS feed ────────────────────────────────────
    try:
        resp = session.get(feed_url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ❌ RSS feed 请求失败: {e}")
        return [{"source_id": source_id, "status": "feed_error", "error": str(e)}]

    try:
        items = rss_parser.parse(resp.text, feed_url)
    except Exception as e:
        print(f"  ❌ RSS 解析失败: {e}")
        return [{"source_id": source_id, "status": "parse_error", "error": str(e)}]

    if not items:
        print(f"  ❌ RSS feed 没有条目")
        return [{"source_id": source_id, "status": "no_items"}]

    print(f"  RSS 条目总数: {len(items)}")

    # ── Step 2: Test sample articles ──────────────────────────────
    samples = items[:sample_count]
    resolved_parser = registry._resolve(source_id, "")
    parser_name = type(resolved_parser).__name__ if resolved_parser else "None"
    print(f"  解析器: {parser_name}")

    results = []
    for i, item in enumerate(samples):
        url = item.url
        title = item.title

        print(f"\n  [{i+1}] {title[:70]}")
        print(f"      URL: {url[:120]}")

        # ── Step 3: Download article HTML ─────────────────────────
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            print(f"      ❌ HTTP 请求失败: {e}")
            results.append({
                "source_id": source_id,
                "url": url,
                "error": str(e),
                "status": "http_error",
            })
            continue

        print(f"      HTML: {len(resp.text)} bytes, 状态码: {resp.status_code}")

        # ── Step 4: Parse via registry (production code path) ─────
        try:
            result = registry.parse(source_id, resp.text, url)
        except Exception as e:
            print(f"      ❌ 解析异常: {e}")
            results.append({
                "source_id": source_id,
                "url": url,
                "error": str(e),
                "status": "parse_error",
            })
            continue

        if result is None:
            print(f"      ❌ 解析返回 None（所有降级路径均失败）")
            results.append({
                "source_id": source_id,
                "url": url,
                "status": "all_failed",
            })
            continue

        md = result.get("markdown", "")
        md_len = len(md)
        title_extracted = result.get("title", "")
        author = result.get("author", "")
        summary = result.get("summary", "")

        tier = _summarize_tier(result)
        qcheck = MarkdownQualityChecker.check(md, source_id)

        print(f"      标题: {(title_extracted or '(未提取到)')[:80]}")
        print(f"      作者: {author or '(无)'}")
        print(f"      摘要: {(summary or '(无)')[:80]}")
        print(f"      正文: {md_len} chars  |  层级: {tier}")
        if qcheck["issues"]:
            for issue in qcheck["issues"]:
                print(f"      ⚠️  格式问题: {issue}")
        print(f"      预览: {md[:200].replace(chr(10), ' ')}")

        # ── Quality judgment (length + format) ────────────────────
        if md_len >= 500 and qcheck["score"] >= 80:
            quality = "✅ 良好"
            status = "ok"
        elif md_len >= 200 and qcheck["score"] >= 60:
            quality = "⚠️ 边界"
            status = "marginal"
        else:
            quality = "❌ 不足"
            status = "insufficient"

        print(f"      质量: {quality}  (格式分: {qcheck['score']})")

        results.append({
            "source_id": source_id,
            "url": url,
            "title": title_extracted,
            "author": author,
            "summary": summary,
            "content_length": md_len,
            "tier": tier,
            "status": status,
        })

    # ── Per-feed summary ──────────────────────────────────────────
    ok = sum(1 for r in results if r.get("status") == "ok")
    marginal = sum(1 for r in results if r.get("status") == "marginal")
    insufficient = sum(1 for r in results if r.get("status") == "insufficient")
    errors = sum(1 for r in results if r.get("status") in ("http_error", "parse_error", "all_failed", "feed_error", "no_items"))

    print(f"\n  ── {name} 汇总 ──")
    print(f"    良好: {ok}, 边界: {marginal}, 不足: {insufficient}, 错误: {errors}")
    print(f"    结论: ", end="")
    if errors == len(results):
        print("🔴 需要调查（全部失败）")
    elif insufficient > ok:
        print("🟡 可能需要专用 Parser")
    elif marginal > 0 and ok == 0:
        print("🟡 边界情况，需人工判断")
    else:
        print("🟢 可用")

    return results


# ═══════════════════════════════════════════════════════════════════
# Main — run all or filter by source_id
# ═══════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) > 1:
        filter_text = sys.argv[1]
        feeds = [f for f in FEEDS if filter_text in f["source_id"] or filter_text in f["name"]]
        if not feeds:
            print(f"未找到匹配 '{filter_text}' 的源")
            sys.exit(1)
    else:
        feeds = FEEDS

    all_results: dict[str, list[dict]] = {}
    for feed in feeds:
        results = _run_single_feed(feed)
        all_results[feed["source_id"]] = results

    # ── Final summary ─────────────────────────────────────────────
    print("\n\n" + "=" * 70)
    print("  最终汇总")
    print("=" * 70)
    print(f"  {'源 ID':28s} {'状态':6s} {'OK':>3s} {'边界':>3s} {'不足':>3s} {'错误':>3s}  {'平均正文':>6s}  解析器")
    print(f"  {'-'*26}  {'-'*4}  {'-'*3}  {'-'*3}  {'-'*3}  {'-'*3}  {'-'*6}  {'-'*12}")

    enabled_ok = 0
    enabled_bad = 0
    disabled_sources = []

    for source_id, results in all_results.items():
        feed_info = next((f for f in FEEDS if f["source_id"] == source_id), {})
        enabled = feed_info.get("enabled", True)

        if not results or results[0].get("status") in ("feed_error", "no_items"):
            if enabled:
                print(f"  {source_id:28s} {'启用':6s}  {'—':>3s}  {'—':>3s}  {'—':>3s}  {'1':>3s}  {'—':>6s}  RSS获取失败")
                enabled_bad += 1
            else:
                disabled_sources.append((source_id, "RSS获取失败"))
            continue

        ok = sum(1 for r in results if r.get("status") == "ok")
        marginal = sum(1 for r in results if r.get("status") == "marginal")
        insufficient = sum(1 for r in results if r.get("status") == "insufficient")
        errors = sum(1 for r in results if r.get("status") in ("http_error", "parse_error", "all_failed"))
        lens = [r.get("content_length", 0) for r in results if r.get("content_length")]
        avg_len = sum(lens) // len(lens) if lens else 0

        # Resolved parser name
        resolved = registry._resolve(source_id, "")
        parser_name = type(resolved).__name__ if resolved else "?"

        state = "启用" if enabled else "禁用"

        print(f"  {source_id:28s} {state:6s} {ok:3d} {marginal:3d} {insufficient:3d} {errors:3d}  {avg_len:5d}c  {parser_name}")

        if enabled:
            if ok >= len(results) * 0.5:
                enabled_ok += 1
            else:
                enabled_bad += 1
        else:
            status = "可用" if ok >= len(results) * 0.5 else "有问题"
            disabled_sources.append((source_id, status))

    print(f"\n  启用的源: {enabled_ok} 正常, {enabled_bad} 需要修复")
    if disabled_sources:
        print(f"  禁用的源:")
        for sid, status in disabled_sources:
            print(f"    - {sid}: {status}")


if __name__ == "__main__":
    main()
