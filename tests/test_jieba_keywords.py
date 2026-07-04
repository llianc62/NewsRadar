"""Tests for jieba TF-IDF / TextRank keyword extraction."""

import os
import tempfile
import pytest

from news.analyzer.jieba import JiebaAnalyzer


def _make_analyzer(db=None):
    """Create a JiebaAnalyzer with minimal config."""
    return JiebaAnalyzer({"analyzer": {"enabled": True, "backend": "jieba"}}, db=db)


def _test_config():
    """Minimal config the Crawler constructor accepts."""
    return {
        "app": {"timezone": "Asia/Shanghai"},
        "storage": {
            "resource": {
                "endpoint_url": "http://localhost:9000",
                "bucket_name": "test-bucket",
                "access_key_id": "test-key",
                "secret_access_key": "test-secret",
            },
        },
    }


# ── _clean_markdown_syntax 单元测试 ─────────────────────────────────

def test_clean_markdown_strips_images():
    analyzer = _make_analyzer()
    text = analyzer._clean_markdown_syntax("特朗普 ![图片](https://example.com/img.png) 访问北京")
    assert "example.com" not in text
    assert "img.png" not in text


def test_clean_markdown_strips_links():
    analyzer = _make_analyzer()
    text = analyzer._clean_markdown_syntax("[特朗普](https://example.com/trump) 访问北京")
    assert "example.com" not in text
    assert "特朗普" in text


def test_clean_markdown_strips_formatting():
    analyzer = _make_analyzer()
    text = analyzer._clean_markdown_syntax("**特朗普** 访问 `北京`")
    assert "*" not in text
    assert "`" not in text


# ── _analyze_keywords_textrank 单元测试 ────────────────────────────

def test_strips_markdown_images():
    analyzer = _make_analyzer()
    content = "特朗普 ![图片](https://example.com/img.png) 访问北京"
    tags = analyzer._analyze_keywords_textrank(content)
    assert not any("example" in t or "http" in t or "img" in t for t in tags)


def test_strips_markdown_links():
    analyzer = _make_analyzer()
    content = "[特朗普](https://example.com/trump) 访问北京"
    tags = analyzer._analyze_keywords_textrank(content)
    assert not any("example" in t or "http" in t for t in tags)


def test_strips_markdown_formatting():
    analyzer = _make_analyzer()
    content = "**特朗普** 访问 `北京`"
    tags = analyzer._analyze_keywords_textrank(content)
    assert not any("*" in t or "`" in t for t in tags)


def test_empty_content_returns_empty():
    analyzer = _make_analyzer()
    assert analyzer._analyze_keywords_textrank("") == []


def test_short_content_returns_empty():
    analyzer = _make_analyzer()
    assert analyzer._analyze_keywords_textrank("短。") == []
    assert analyzer._analyze_keywords_textrank("今天天气不错。明天可能下雨。") == []


def test_whitespace_only_returns_empty():
    analyzer = _make_analyzer()
    assert analyzer._analyze_keywords_textrank("   \n  \t  ") == []


def test_extracts_from_chinese_news():
    analyzer = _make_analyzer()
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作，"
        "高市早苗则强调日本在亚太地区的战略地位。"
        "此次会谈持续了约两小时，会后双方发表了联合声明。"
    )
    tags = analyzer._analyze_keywords_textrank(content)
    assert 1 <= len(tags) <= 5
    assert all(isinstance(t, str) and len(t) > 0 for t in tags)


def test_topk_respected():
    analyzer = _make_analyzer()
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。会谈持续约两小时。"
    )
    tags = analyzer._analyze_keywords_textrank(content, topk=3)
    assert len(tags) <= 3


def test_no_duplicate_tags():
    analyzer = _make_analyzer()
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。" * 3
    )
    tags = analyzer._analyze_keywords_textrank(content)
    assert len(tags) == len(set(tags))


# ── _extract_keywords (TF-IDF 优先, TextRank 兜底) ────────────────

def test_extract_keywords_no_db_falls_back_to_textrank(monkeypatch):
    """无数据库时 _extract_keywords 回退到 TextRank"""
    analyzer = _make_analyzer(db=None)
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作。"
        "会谈持续约两小时，会后双方发表了联合声明。"
    )
    tags = analyzer._extract_keywords(content)
    assert 1 <= len(tags) <= 5
    assert all(isinstance(t, str) and len(t) > 0 for t in tags)


def test_extract_keywords_filters_generic_words():
    """默认 jieba IDF 应自动压低通用词权重，关键词不包含"公司"等泛化词"""
    analyzer = _make_analyzer(db=None)
    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "这家公司的主要项目涉及多个企业的合作。"
        "双方讨论了贸易和安全议题。会谈持续约两小时。"
    )
    tags = analyzer._extract_keywords(content)
    assert len(tags) >= 1
    # "公司""企业""项目" 在几乎所有文档中出现，默认 IDF 应压低
    generic = {"公司", "企业", "项目"}
    assert not any(t in generic for t in tags), (
        f"Generic words should be filtered by IDF, got: {tags}"
    )


def test_extract_keywords_short_content_returns_empty():
    """正文 < 50 字符返回空列表"""
    analyzer = _make_analyzer()
    assert analyzer._extract_keywords("短。") == []


# ── POS 过滤移除后回归测试 ─────────────────────────────────────────

def test_no_pos_filter_allows_common_nouns():
    """不限词性时应能提取普通名词（"AI""芯片""算力"等财经科技词）。"""
    analyzer = _make_analyzer(db=None)
    content = (
        "Meta收跌5%创下近期新低，扎克伯格在AI和算力领域的巨额投资令市场担忧。"
        "分析人士指出，人工智能基础设施建设的回报周期较长，短期内难以看到盈利改善。"
        "不过，多位华尔街分析师仍然看好Meta在智能硬件方面的长期布局。"
    )
    tags = analyzer._extract_keywords(content)
    assert len(tags) >= 1
    # 不限词性后，"AI""算力""智能"等非专有名词应能被提取
    assert any(t in {"AI", "算力", "智能", "Meta", "投资"} for t in tags), (
        f"Non-proper-noun tech/finance terms should appear, got: {tags}"
    )


def test_no_pos_filter_allows_tech_terms():
    """不限词性时应能提取科技类普通名词。"""
    analyzer = _make_analyzer(db=None)
    content = (
        "CPO（共封装光学）技术被认为是AI光互联领域的重要突破。"
        "ams OSRAM和英伟达在CPO领域的合作引发市场关注，"
        "VCSEL和硅光技术成为下一代数据中心互连的关键方向。"
        "业内专家认为，CPO将大幅降低数据中心功耗并提升带宽密度。"
    )
    tags = analyzer._extract_keywords(content)
    assert len(tags) >= 1
    # "CPO""技术""领域" 等非专有名词应能被提取
    assert any(t in {"CPO", "技术", "光互联", "数据中心"} for t in tags), (
        f"Tech terms should appear without POS filter, got: {tags}"
    )


# ── 标题参与提取测试 ─────────────────────────────────────────────────

def test_title_included_in_keyword_extraction():
    """analyze_keywords 应将标题与正文拼接后提取关键词。"""
    analyzer = _make_analyzer()
    items = [
        {
            "title": "宇树科技科创板IPO注册生效",
            "content": (
                "机器人企业宇树科技近日完成科创板IPO注册。"
                "该公司主营四足机器人研发制造，产品广泛应用于工业巡检和消费领域。"
                "此次上市将助力公司进一步扩大产能和研发投入规模。"
            ),
            "tags": [],
        },
    ]
    analyzer.analyze_keywords(items)
    tags = items[0]["tags"]
    assert len(tags) >= 1
    # 标题中的"宇树""IPO"应在关键词中出现
    assert any("宇树" in t or "IPO" in t for t in tags), (
        f"Title keywords should appear in extracted tags, got: {tags}"
    )


def test_only_content_no_title_still_works():
    """只有 content 没有 title 时正常提取不报错。"""
    analyzer = _make_analyzer()
    items = [
        {
            "title": "",
            "content": (
                "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
                "双方讨论了贸易和安全议题。会谈持续约两小时，会后双方发表了联合声明。"
            ),
            "tags": [],
        },
    ]
    analyzer.analyze_keywords(items)
    assert len(items[0]["tags"]) >= 1


def test_title_only_no_content():
    """只有 title 没有 content 时使用 title 提取。"""
    analyzer = _make_analyzer()
    items = [
        {
            "title": "宇树科技科创板IPO注册生效 机器人行业迎利好",
            "content": "",
            "tags": [],
        },
    ]
    analyzer.analyze_keywords(items)
    # 标题太短（<50字符）应返回空
    assert items[0]["tags"] == []


# ── analyze_keywords(items) 批量接口 ────────────────────────────────

def test_analyze_keywords_batch_sets_tags_on_items():
    """analyze_keywords(items) 原地设置每个 item 的 tags"""
    analyzer = _make_analyzer()
    items = [
        {
            "content": (
                "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
                "双方讨论了贸易和安全议题。会谈持续约两小时，会后双方发表了联合声明。"
            ),
            "tags": [],
        },
        {"content": "短。", "tags": []},
    ]
    analyzer.analyze_keywords(items)
    assert len(items[0]["tags"]) >= 1
    assert items[1]["tags"] == []


# ── 集成：_download_and_parse preserves parser tags ─────────────────

def test_download_and_parse_preserves_meta_tags_when_present(monkeypatch):
    """当 trafilatura 有 tags 时，保留原始 tags"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler
    import news.crawler as crawler_mod

    class FakeParserRegistry:
        def parse(self, source_id, html, url=""):
            return {
                "markdown": "正文内容。",
                "title": "测试",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": ["编辑标注", "原创"],
            }

    monkeypatch.setattr(crawler_mod, "parser", FakeParserRegistry())
    crawler = Crawler(_test_config())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout, headers=None: FakeResp())

    item = {"url": "https://example.com/news/2", "source_id": "test"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == ["编辑标注", "原创"]


def test_download_and_parse_empty_tags_stays_empty(monkeypatch):
    """parser 返回空 tags 时保持空列表，keywords 由上层 fetch_all 统一处理"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler
    import news.crawler as crawler_mod

    class FakeParserRegistry:
        def parse(self, source_id, html, url=""):
            return {
                "markdown": (
                    "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
                    "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作。"
                    "会谈持续约两小时，会后双方发表了联合声明。"
                ),
                "title": "测试标题",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": [],  # 无 tags → 不再在 _download_and_parse 中 fallback
            }

    monkeypatch.setattr(crawler_mod, "parser", FakeParserRegistry())
    crawler = Crawler(_test_config())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout, headers=None: FakeResp())

    item = {"url": "https://example.com/news/1", "source_id": "test"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == []  # keywords 由 fetch_all 的 analyze_keywords(items) 统一处理


def test_download_and_parse_short_content_preserves_tags(monkeypatch):
    """正文短时 tags 保持 parser 返回的原值"""
    import storage.s3
    monkeypatch.setattr(storage.s3.S3Client, "_ensure_bucket", lambda self: None)
    from news.crawler import Crawler
    import news.crawler as crawler_mod

    class FakeParserRegistry:
        def parse(self, source_id, html, url=""):
            return {
                "markdown": "短。",
                "title": "测试",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": [],
            }

    monkeypatch.setattr(crawler_mod, "parser", FakeParserRegistry())
    crawler = Crawler(_test_config())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout, headers=None: FakeResp())

    item = {"url": "https://example.com/news/3", "source_id": "test"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == []


# ── _clean_tags 标签清理测试 ──────────────────────────────────────────

def test_clean_tags_filters_junk_regex():
    """启发式规则过滤纯数字、数字开头量值、纯符号、单字符。"""
    analyzer = _make_analyzer()
    tags = {"123", "3.5亿", "10000亿元", "---", "a", "AI", "芯片", "特朗普"}
    cleaned = analyzer._clean_tags(tags)
    assert set(cleaned) == {"AI", "芯片", "特朗普"}


def test_clean_tags_filters_blacklist():
    """黑名单过滤已知垃圾标签。"""
    analyzer = _make_analyzer()
    tags = {"屏蔽外部", "许可证", "归母", "中证", "中证网", "AI", "半导体"}
    cleaned = analyzer._clean_tags(tags)
    assert set(cleaned) == {"AI", "半导体"}


def test_clean_tags_empty_set():
    analyzer = _make_analyzer()
    assert analyzer._clean_tags(set()) == []


def test_analyze_keywords_merges_and_cleans():
    """端到端：合并页面标签 + Jieba 关键词，过滤后写入。"""
    analyzer = _make_analyzer()
    items = [{
        "title": "特朗普与Meta讨论AI芯片合作",
        "content": (
            "美国前总统特朗普与Meta公司高管讨论了AI芯片领域的合作事宜。"
            "双方就半导体供应链和技术出口管制等议题交换了意见。"
            "分析人士认为这一合作将对全球芯片产业格局产生深远影响。"
        ),
        "tags": ["特朗普", "屏蔽外部", "AI"],  # "屏蔽外部" 应在黑名单中被过滤
    }]
    analyzer.analyze_keywords(items)
    tags = items[0]["tags"]
    assert "屏蔽外部" not in tags
    assert "特朗普" in tags  # 页面标签保留
    assert "AI" in tags      # 页面标签保留（同时也是 Jieba 关键词）
    assert len(tags) >= 3     # 至少还有 Jieba 提取的新词
