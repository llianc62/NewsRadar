"""crawl4ai 集成单元测试 - WAF 站点 HTML 下载路由 + WAF 指纹检测。

crawl4ai 替换 Playwright 负责 WAF 站点(huxiu/xueqiu/juejin)的 HTML 下载,
拿到 HTML 后走现有 parser.parse。本测试 mock crawl4ai 层,聚焦路由逻辑。
crawl4ai 真实抓取能力由集成测试覆盖。
"""
from unittest.mock import MagicMock

import pytest

from news.crawler import Crawler


@pytest.fixture
def crawler():
    """最小 config 构造 Crawler(不连 DB,不启动浏览器,lazy)。"""
    config = {
        "crawler": {
            "max_workers": 2, "timeout": 30, "max_retry": 3,
            "newsnow": {"sources": []}, "rss": {"sources": []},
        },
        "storage": {},
        "app": {"timezone": "UTC"},
    }
    return Crawler(config)


def _make_item(url="https://www.huxiu.com/article/1.html", source_id="huxiu"):
    return {"url": url, "source_id": source_id, "content": "", "title": ""}


# ── _looks_like_waf_challenge ─────────────────────────────────────────


def test_waf_challenge_detects_aliyun(crawler):
    html = '<meta name="aliyun_waf_aa" content="x"><title>滑动验证页面</title>'
    assert crawler._looks_like_waf_challenge(html) is True


def test_waf_challenge_detects_access_verification(crawler):
    html = "# Access Verification\nslide to complete the verification process"
    assert crawler._looks_like_waf_challenge(html) is True


def test_waf_challenge_negative_normal_content(crawler):
    html = "<html><body><h1>当AI模型被按下暂停键</h1><p>Anthropic...</p></body></html>"
    assert crawler._looks_like_waf_challenge(html) is False


def test_waf_challenge_negative_empty(crawler):
    assert crawler._looks_like_waf_challenge("") is False
    assert crawler._looks_like_waf_challenge(None) is False


# ── _download_and_parse WAF 分支路由 ──────────────────────────────────


def test_waf_url_routes_to_crawl4ai_not_playwright(crawler, monkeypatch):
    """WAF 域名走 crawl4ai,不调 Playwright。"""
    c4ai = MagicMock(return_value="<html>real content</html>")
    pw = MagicMock(return_value=(None, "err"))
    monkeypatch.setattr(crawler, "_download_with_crawl4ai", c4ai)
    monkeypatch.setattr(crawler, "_download_with_playwright", pw)
    monkeypatch.setattr(
        crawler.parser, "parse",
        MagicMock(return_value={"markdown": "md", "title": "t"}),
    )

    item = _make_item()
    assert crawler._download_and_parse(item) is True
    c4ai.assert_called_once()
    pw.assert_not_called()
    assert item["content"] == "md"


def test_crawl4ai_failure_records_failed_tasks(crawler, monkeypatch):
    """crawl4ai 返回 None -> 记录 failed_tasks,return False。"""
    monkeypatch.setattr(crawler, "_download_with_crawl4ai", MagicMock(return_value=None))
    recorded = []
    monkeypatch.setattr(crawler, "_record_content_fetch_failure",
                        lambda item, err: recorded.append((item["url"], err)))

    item = _make_item()
    assert crawler._download_and_parse(item) is False
    assert len(recorded) == 1
    assert "crawl4ai" in recorded[0][1].lower()


def test_non_waf_url_uses_requests(crawler, monkeypatch):
    """非 WAF 域名走 requests,不调 crawl4ai。"""
    c4ai = MagicMock(return_value=None)
    monkeypatch.setattr(crawler, "_download_with_crawl4ai", c4ai)

    fake_resp = MagicMock()
    fake_resp.text = "<html>normal</html>"
    monkeypatch.setattr(
        "news.crawler.http_get_with_retry",
        MagicMock(return_value=(fake_resp, None)),
    )
    monkeypatch.setattr(
        crawler.parser, "parse",
        MagicMock(return_value={"markdown": "md", "title": "t"}),
    )

    item = _make_item(url="https://example.com/news/1", source_id="example")
    assert crawler._download_and_parse(item) is True
    c4ai.assert_not_called()
