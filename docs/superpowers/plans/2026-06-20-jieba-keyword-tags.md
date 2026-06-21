# Jieba + TextRank 关键词提取 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当 trafilatura 无法从页面元数据提取 tags 时，用 jieba TextRank 从正文自动提取关键词作为 fallback。

**Architecture:** 在 `news/crawler.py` 的 `_download_and_parse()` 中新增 fallback 逻辑 — trafilatura 提取到 tags 则直接用，没有则调 jieba TextRank。新函数 `_extract_keywords_textrank()` 作为模块级函数，负责 Markdown 清洗 + 分词 + TextRank 提取。jieba 用 lazy import 避免未安装时阻塞。

**Tech Stack:** Python >= 3.12, jieba >= 0.42（新依赖）, trafilatura（现有）

**Spec:** [2026-06-20-jieba-keyword-tags.md](../specs/2026-06-20-jieba-keyword-tags.md)

## Global Constraints

- 仅在 trafilatura 未提取到 tags 且正文 >= 50 字符时调用 jieba
- TopK = 5，词性过滤 `ns/n/vn/nr/nt/nz`
- jieba 通过 lazy import 加载，不阻塞无 jieba 的运行环境
- Markdown 语法标记（`**`、`[]()`、`![]()`、`#`）需在分词前清洗
- 澎湃新闻等已有编辑标注 tags 的来源不受影响
- 所有现有测试必须继续通过

---

### Task 1: `_extract_keywords_textrank()` — 关键词提取函数

**Files:**
- Create: `tests/test_keywords.py`
- Modify: `news/crawler.py:1-22` (add import)
- Modify: `news/crawler.py:397-420` (add new function)

**Interfaces:**
- Produces: `_extract_keywords_textrank(content: str, topk: int = 5) -> list[str]` — 模块级函数，从 Markdown 正文提取关键词
- Consumes: `jieba.analyse.textrank`（lazy import）

- [ ] **Step 1: 编写测试文件**

创建 `tests/test_keywords.py`：

```python
"""Tests for jieba TextRank keyword extraction fallback."""

import pytest


# ── 正文清洗 ──────────────────────────────────────────────────────

def test_strips_markdown_images():
    """图片语法 `![alt](url)` 被完全去除"""
    from news.crawler import _extract_keywords_textrank

    content = "特朗普 ![图片](https://example.com/img.png) 访问北京"
    tags = _extract_keywords_textrank(content)
    # 不应出现 URL 片段
    assert not any("example" in t or "http" in t or "img" in t for t in tags)


def test_strips_markdown_links():
    """链接语法 `[text](url)` 保留文字、去除 URL"""
    from news.crawler import _extract_keywords_textrank

    content = "[特朗普](https://example.com/trump) 访问北京"
    tags = _extract_keywords_textrank(content)
    assert not any("example" in t or "http" in t for t in tags)


def test_strips_markdown_formatting():
    """粗体 `**text**` 和标题 `# heading` 标记被去除"""
    from news.crawler import _extract_keywords_textrank

    content = "**特朗普** 访问 `北京`"
    tags = _extract_keywords_textrank(content)
    # 标记字符不应出现在 tags 中
    assert not any("*" in t or "`" in t for t in tags)


# ── 边界条件 ──────────────────────────────────────────────────────

def test_empty_content_returns_empty():
    from news.crawler import _extract_keywords_textrank

    assert _extract_keywords_textrank("") == []


def test_short_content_returns_empty():
    """正文 < 50 字符跳过提取"""
    from news.crawler import _extract_keywords_textrank

    assert _extract_keywords_textrank("短。") == []
    assert _extract_keywords_textrank("今天天气不错。明天可能下雨。") == []


def test_whitespace_only_returns_empty():
    from news.crawler import _extract_keywords_textrank

    assert _extract_keywords_textrank("   \n  \t  ") == []


# ── 正常提取 ──────────────────────────────────────────────────────

def test_extracts_from_chinese_news():
    """正常中文新闻正文 → 返回 <= 5 个关键词"""
    from news.crawler import _extract_keywords_textrank

    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。特朗普表示将继续推动双边合作，"
        "高市早苗则强调日本在亚太地区的战略地位。"
        "此次会谈持续了约两小时，会后双方发表了联合声明。"
    )
    tags = _extract_keywords_textrank(content)
    assert 1 <= len(tags) <= 5
    assert all(isinstance(t, str) and len(t) > 0 for t in tags)


def test_topk_respected():
    """返回数量不超过 topk"""
    from news.crawler import _extract_keywords_textrank

    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。会谈持续约两小时。"
    )
    tags = _extract_keywords_textrank(content, topk=3)
    assert len(tags) <= 3


def test_no_duplicate_tags():
    """返回的 tags 没有重复"""
    from news.crawler import _extract_keywords_textrank

    content = (
        "美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，"
        "双方讨论了贸易和安全议题。" * 3  # 重复三次增加词频
    )
    tags = _extract_keywords_textrank(content)
    assert len(tags) == len(set(tags))
```

- [ ] **Step 2: 运行测试 — 期望全部 FAIL（函数不存在）**

```bash
pytest tests/test_keywords.py -v
```

期望：全部 8 个测试 FAIL，报 `ImportError: cannot import name '_extract_keywords_textrank'`

- [ ] **Step 3: 添加 jieba 依赖**

修改 `pyproject.toml:18`，在 `"typer>=0.26.7"` 之后加一行：

```toml
    "jieba>=0.42",
```

完整依赖块变为：

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "jinja2>=3.1",
    "requests>=2.33",
    "boto3>=1.42",
    "PyYAML>=6.0",
    "pytz>=2026.1",
    "feedparser>=6.0",
    "psycopg2-binary>=2.9",
    "trafilatura>=2.0",
    "mistune>=3.2.1",
    "typer>=0.26.7",
    "jieba>=0.42",
]
```

安装 jieba：

```bash
uv pip install jieba
```

验证：

```bash
python3 -c "import jieba; print('jieba', jieba.__version__)"
```

- [ ] **Step 4: 实现 `_extract_keywords_textrank()`**

在 `news/crawler.py` 中，找到 `_extract_image_urls` 静态方法（约 line 454），在其 **上方** 插入新函数。

具体：在 line 397（`return True` 之后、`_run_batch_image_download` 之前）插入：

```python
# ── Keyword extraction (jieba TextRank fallback) ─────────────────

def _extract_keywords_textrank(content: str, topk: int = 5) -> list[str]:
    """从 Markdown 正文提取关键词，用作 tags fallback。

    仅当页面元数据（meta keywords / JSON-LD）无 tags 时调用。
    使用 jieba TextRank 算法 + 词性过滤，适合中文新闻正文。

    Args:
        content: Markdown 格式的 article body。
        topk: 最多返回的关键词数量。

    Returns:
        关键词列表（可能少于 *topk* 当正文信息量不足时），
        或空列表当正文过短或无法提取。
    """
    # ── 清洗 Markdown 语法 ──────────────────────────────────────
    text = re.sub(r'!\[.*?\]\(.*?\)', '', content)          # 图片
    text = re.sub(r'\[([^\]]*)\]\(.*?\)', r'\1', text)      # 链接保留文字
    text = re.sub(r'[#*>`|~\-_]', ' ', text)                # 格式标记
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) < 50:
        return []

    # ── jieba TextRank ─────────────────────────────────────────
    try:
        import jieba.analyse
    except ImportError:
        return []

    keywords = jieba.analyse.textrank(
        text,
        topK=topk,
        withWeight=False,
        allowPOS=('ns', 'n', 'vn', 'nr', 'nt', 'nz'),
    )
    return keywords
```

> **注意：** 这不是 `Crawler` 的方法，是模块级函数。放在 `_extract_image_urls` 附近（两个都是模块级静态工具函数）。

同时需要在文件顶部 import 区域确认 `import re` 已存在（line 18，无需改动）。

- [ ] **Step 5: 运行测试 — 期望全部 PASS**

```bash
pytest tests/test_keywords.py -v
```

期望：全部 8 个测试 PASS。

- [ ] **Step 6: 提交**

```bash
git add tests/test_keywords.py news/crawler.py pyproject.toml
git commit -m "feat: add _extract_keywords_textrank() for jieba TextRank keyword extraction"
```

---

### Task 2: 集成 — `_download_and_parse()` 中启用 tags fallback

**Files:**
- Modify: `news/crawler.py:396`

**Interfaces:**
- Consumes: `_extract_keywords_textrank(content: str, topk: int = 5) -> list[str]` (from Task 1)

- [ ] **Step 1: 编写集成测试**

在 `tests/test_keywords.py` 末尾追加：

```python
# ── 集成：_download_and_parse tags fallback ──────────────────────

def test_download_and_parse_falls_back_to_textrank_when_no_meta_tags(monkeypatch):
    """当 trafilatura 无 tags 时，用 jieba TextRank 填补"""
    from news.crawler import Crawler

    # 构造一个返回无 tags 内容的 parser mock
    class FakeParser:
        def parse(self, html, url=""):
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
                "tags": [],  # 无 tags → 触发 fallback
            }

    crawler = Crawler({"app": {"timezone": "Asia/Shanghai"}}, parser=FakeParser())

    # mock HTTP session 避免真实网络请求
    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout: FakeResp())

    item = {"url": "https://example.com/news/1"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert len(item["tags"]) >= 1
    assert len(item["tags"]) <= 5
    assert all(isinstance(t, str) for t in item["tags"])


def test_download_and_parse_preserves_meta_tags_when_present(monkeypatch):
    """当 trafilatura 有 tags 时，保留原始 tags，不覆盖"""
    from news.crawler import Crawler

    class FakeParser:
        def parse(self, html, url=""):
            return {
                "markdown": "正文内容。",
                "title": "测试",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": ["编辑标注", "原创"],
            }

    crawler = Crawler({"app": {"timezone": "Asia/Shanghai"}}, parser=FakeParser())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout: FakeResp())

    item = {"url": "https://example.com/news/2"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == ["编辑标注", "原创"]


def test_download_and_parse_short_content_no_fallback(monkeypatch):
    """正文 < 50 字符时，无 meta tags 也不会生成 NLP tags"""
    from news.crawler import Crawler

    class FakeParser:
        def parse(self, html, url=""):
            return {
                "markdown": "短。",
                "title": "测试",
                "author": "",
                "published_at": "",
                "summary": "",
                "category": "",
                "tags": [],
            }

    crawler = Crawler({"app": {"timezone": "Asia/Shanghai"}}, parser=FakeParser())

    class FakeResp:
        text = "<html></html>"
        def raise_for_status(self):
            pass

    monkeypatch.setattr(crawler.session(), "get", lambda url, timeout: FakeResp())

    item = {"url": "https://example.com/news/3"}
    ok = crawler._download_and_parse(item)
    assert ok is True
    assert item["tags"] == []
```

- [ ] **Step 2: 运行集成测试 — 期望 FAIL**

```bash
pytest tests/test_keywords.py::test_download_and_parse_falls_back_to_textrank_when_no_meta_tags -v
pytest tests/test_keywords.py::test_download_and_parse_preserves_meta_tags_when_present -v
pytest tests/test_keywords.py::test_download_and_parse_short_content_no_fallback -v
```

3 个集成测试应该 FAIL（还未修改 `_download_and_parse()`）。

- [ ] **Step 3: 修改 `_download_and_parse()` 集成 fallback**

修改 `news/crawler.py:396`，将：

```python
        item["tags"] = parsed.get("tags", [])
        return True
```

改为：

```python
        item["tags"] = parsed.get("tags", [])
        if not item["tags"] and item.get("content"):
            item["tags"] = _extract_keywords_textrank(item["content"])
        return True
```

完整方法结尾变为：

```python
        item["content"] = parsed["markdown"]
        item["author"] = parsed.get("author", "")
        item["published_at"] = parsed.get("published_at", "")
        item["summary"] = parsed.get("summary", "")
        item["category"] = parsed.get("category", "")
        item["tags"] = parsed.get("tags", [])
        if not item["tags"] and item.get("content"):
            item["tags"] = _extract_keywords_textrank(item["content"])
        return True
```

- [ ] **Step 4: 运行全部测试 — 期望全部 PASS**

```bash
pytest tests/test_keywords.py -v
```

期望：11 个测试全部 PASS（8 个单元 + 3 个集成）。

再跑现有测试确保无回归：

```bash
pytest tests/ -v
```

期望：所有 4 个已有测试文件（test_parser、test_refetch、test_notification_frontend、test_delete）继续 PASS。

- [ ] **Step 5: 提交**

```bash
git add news/crawler.py tests/test_keywords.py
git commit -m "feat: integrate jieba TextRank tags fallback into _download_and_parse"
```

---

## 完成检查

- [ ] `pyproject.toml` 新增 `jieba>=0.42`
- [ ] `news/crawler.py` 新增 `_extract_keywords_textrank()` 函数
- [ ] `news/crawler.py:_download_and_parse()` 在无 meta tags 时调用 fallback
- [ ] `tests/test_keywords.py` 包含 8 个单元测试 + 3 个集成测试
- [ ] 所有已有测试 0 回归
- [ ] 澎湃新闻有 meta keywords 的文章不受影响（editorial tags 保留）
- [ ] 无 meta keywords 的文章自动获得 jieba TextRank 关键词
