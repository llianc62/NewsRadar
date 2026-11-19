"""crawl4ai 集成单元测试 - WAF 站点 HTML 下载路由 + WAF 指纹检测 + 浏览器回收。

crawl4ai 替换 Playwright 负责 WAF 站点(huxiu/xueqiu/juejin)的 HTML 下载,
拿到 HTML 后走现有 parser.parse。本测试 mock crawl4ai 层,聚焦路由逻辑。
crawl4ai 真实抓取能力由集成测试覆盖。
"""
import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from news.crawler import Crawler, _env_proxy_url


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    """每个测试从干净代理环境起步,避免开发者机器残留 *_proxy 污染断言。"""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)


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


# ── WAF 挑战后浏览器回收 ──────────────────────────────────────────


class _FakeRunResult:
    def __init__(self, html: str, success: bool = True, error_message: str = ""):
        self.success = success
        self.html = html
        self.error_message = error_message


@pytest.fixture
def fake_loop():
    """真实后台事件循环 — arun 经 run_coroutine_threadsafe 提交。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


def _install_fake_browser(crawler, results):
    """注入 fake AsyncWebCrawler,arun 依次返回 results。"""
    calls = []

    async def fake_arun(url, config):
        calls.append(url)
        return results.pop(0)

    fake = MagicMock()
    fake.arun = fake_arun
    crawler._crawl4ai_crawler = fake
    return fake, calls


def test_waf_challenge_recycles_browser_then_retries(
    crawler, monkeypatch, fake_loop,
):
    """WAF 挑战 → 回收浏览器(新 profile) → 重试拿到正文。

    阿里云 WAF 按浏览器会话拉黑:同一浏览器内重试只会再次被挑战,
    必须换全新浏览器才能恢复(见 2026-09-03 huxiu 批量失败事故)。
    """
    crawler._crawl4ai_loop = fake_loop
    monkeypatch.setattr(crawler, "_ensure_crawl4ai", MagicMock())
    results = [
        _FakeRunResult('<meta name="aliyun_waf_aa">slide to complete'),
        _FakeRunResult("<html><body>正文</body></html>"),
    ]
    _install_fake_browser(crawler, results)
    recycle = MagicMock()
    monkeypatch.setattr(crawler, "_recycle_crawl4ai", recycle)

    html = crawler._download_with_crawl4ai(
        "https://www.huxiu.com/article/1.html", 30)

    assert html == "<html><body>正文</body></html>"
    recycle.assert_called_once()


def test_nav_timeout_recycles_browser_then_retries(
    crawler, monkeypatch, fake_loop,
):
    """导航超时(success=False)也应回收浏览器再重试,而非同浏览器硬重试。

    阿里云 WAF 也可能以「挂起连接不返回滑块页」的形式拉黑会话,表现为
    page.goto 超时;这类失败同样要换全新浏览器才能恢复(见 huxiu ACS-GOTO
    Timeout 30000ms 事故)。
    """
    crawler._crawl4ai_loop = fake_loop
    monkeypatch.setattr(crawler, "_ensure_crawl4ai", MagicMock())
    results = [
        _FakeRunResult(
            "",
            success=False,
            error_message="Failed on navigating ACS-GOTO: Timeout 30000ms exceeded",
        ),
        _FakeRunResult("<html><body>正文</body></html>"),
    ]
    _install_fake_browser(crawler, results)
    recycle = MagicMock()
    monkeypatch.setattr(crawler, "_recycle_crawl4ai", recycle)

    html = crawler._download_with_crawl4ai(
        "https://www.huxiu.com/article/1.html", 30)

    assert html == "<html><body>正文</body></html>"
    recycle.assert_called_once()


def test_nav_timeout_all_fail_returns_none(crawler, monkeypatch, fake_loop):
    """多次导航超时 → 返回 None,回收次数 = 重试次数 - 1(末次不再回收)。"""
    crawler._crawl4ai_loop = fake_loop
    monkeypatch.setattr(crawler, "_ensure_crawl4ai", MagicMock())
    results = [
        _FakeRunResult("", success=False, error_message="Timeout 30000ms"),
        _FakeRunResult("", success=False, error_message="Timeout 30000ms"),
        _FakeRunResult("", success=False, error_message="Timeout 30000ms"),
    ]
    _install_fake_browser(crawler, results)
    recycle = MagicMock()
    monkeypatch.setattr(crawler, "_recycle_crawl4ai", recycle)

    assert crawler._download_with_crawl4ai(
        "https://www.huxiu.com/article/1.html", 30) is None
    assert recycle.call_count == 2  # max_retry=3,末次失败后不再回收


def test_waf_challenge_all_fail_returns_none(crawler, monkeypatch, fake_loop):
    """所有重试均被挑战 → 返回 None,回收次数 = 重试次数 - 1(末次不再回收)。"""
    crawler._crawl4ai_loop = fake_loop
    monkeypatch.setattr(crawler, "_ensure_crawl4ai", MagicMock())
    results = [
        _FakeRunResult("aliyun_waf_aa"),
        _FakeRunResult("aliyun_waf_aa"),
        _FakeRunResult("aliyun_waf_aa"),
    ]
    _install_fake_browser(crawler, results)
    recycle = MagicMock()
    monkeypatch.setattr(crawler, "_recycle_crawl4ai", recycle)

    assert crawler._download_with_crawl4ai(
        "https://www.huxiu.com/article/1.html", 30) is None
    assert recycle.call_count == 2  # max_retry=3,末次挑战后不再回收


def test_recycle_closes_old_browser_and_rebuilds(crawler, monkeypatch):
    """_recycle_crawl4ai:关旧浏览器 + 重建(ensure)。"""
    closed = MagicMock()
    ensured = MagicMock()
    monkeypatch.setattr(crawler, "_close_crawl4ai", closed)
    monkeypatch.setattr(crawler, "_ensure_crawl4ai", ensured)

    crawler._crawl4ai_crawler = MagicMock()
    crawler._recycle_crawl4ai()
    closed.assert_called_once()
    ensured.assert_called_once()


def test_recycle_noop_when_not_initialized(crawler, monkeypatch):
    """未初始化时回收为 no-op,不触发关停/重建。"""
    closed = MagicMock()
    ensured = MagicMock()
    monkeypatch.setattr(crawler, "_close_crawl4ai", closed)
    monkeypatch.setattr(crawler, "_ensure_crawl4ai", ensured)

    crawler._recycle_crawl4ai()
    closed.assert_not_called()
    ensured.assert_not_called()


# ── _env_proxy_url ──────────────────────────────────────────────────


def test_env_proxy_url_reads_https_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    assert _env_proxy_url() == "http://127.0.0.1:7890"


def test_env_proxy_url_reads_lowercase(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    assert _env_proxy_url() == "http://127.0.0.1:7890"


def test_env_proxy_url_https_wins_over_http(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7891")
    assert _env_proxy_url() == "http://127.0.0.1:7890"


def test_env_proxy_url_empty_string_is_unset(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    assert _env_proxy_url() is None


def test_env_proxy_url_unset_returns_none(monkeypatch):
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    assert _env_proxy_url() is None


def test_env_proxy_url_falls_back_to_http_proxy(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7891")
    assert _env_proxy_url() == "http://127.0.0.1:7891"


# ── _ensure_crawl4ai 代理注入 ───────────────────────────────────────


def test_ensure_crawl4ai_passes_proxy_to_browser_config(crawler, monkeypatch):
    """设了代理 env 时,BrowserConfig 应收到 proxy_config dict。"""
    import sys
    import types

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")

    browser_config_calls = []

    def fake_browser_config(**kwargs):
        browser_config_calls.append(kwargs)
        return MagicMock()

    async def _noop():
        return None

    fake_crawler = MagicMock()
    fake_crawler.start.side_effect = _noop   # 返回协程对象,供 run_until_complete
    fake_crawler.close.side_effect = _noop

    def fake_async_web_crawler(config):
        return fake_crawler

    fake = types.ModuleType("crawl4ai")
    fake.AsyncWebCrawler = fake_async_web_crawler
    fake.BrowserConfig = fake_browser_config
    monkeypatch.setitem(sys.modules, "crawl4ai", fake)

    try:
        crawler._ensure_crawl4ai()
        assert browser_config_calls[0]["proxy_config"] == {
            "server": "http://127.0.0.1:7890",
        }
    finally:
        crawler._close_crawl4ai()


def test_playwright_launch_gets_proxy_kwargs(crawler, monkeypatch):
    """设了代理 env 时,chromium.launch 应收到 proxy={"server": ...}。"""
    import sys
    import types

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")

    launch_calls = []

    def fake_launch(**kwargs):
        launch_calls.append(kwargs)
        return MagicMock()

    class _FakeChromium:
        launch = staticmethod(fake_launch)

    class _FakePw:
        chromium = _FakeChromium()

    class _FakeSyncPlaywright:
        def start(self):
            return _FakePw()

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _FakeSyncPlaywright()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    try:
        crawler._get_playwright_executor("huxiu.com")
    finally:
        executor = crawler._playwright_executors.pop("huxiu.com", None)
        if executor is not None:
            executor.shutdown(wait=False)
        crawler._playwright_browsers.pop("huxiu.com", None)

    assert launch_calls[0].get("proxy") == {
        "server": "http://127.0.0.1:7890",
    }
