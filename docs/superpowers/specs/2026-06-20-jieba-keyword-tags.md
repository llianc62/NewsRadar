# Jieba + TextRank 关键词提取 — 设计文档

> **状态:** 草稿  
> **日期:** 2026-06-20  
> **目标:** 让所有来源的新闻都有 tags，不再仅有澎湃新闻

## 问题

当前 tags 完全依赖 `trafilatura.extract_metadata()` 从页面 HTML 的 `<meta property="keywords">` 提取。在 4 个新闻源中，仅澎湃新闻的页面模板包含此标签：

| 来源 | 有 meta keywords | 有 tags 的文章 |
|------|:---:|:---:|
| 澎湃新闻 | ✅ | 20 / 142 |
| 华尔街见闻 | ❌ | 0 / 67 |
| 财联社热门 | ❌ | 0 / 91 |
| 36氪 | ❌ | 0 / 0 |

即使澎湃新闻也不是每篇都有 — 取决于编辑是否给文章打标签。

## 方案

在 content enrichment 阶段新增 fallback：**trafilatura 没提取到 tags → jieba TextRank 从正文提取**。

```
下载 HTML → trafilatura 解析 → meta keywords 提取 tags
                                    │
                    有 tags ────────┤────── 无 tags
                      │                        │
                      ▼                        ▼
                   直接使用           jieba.analyse.textrank()
                                          │
                                          ▼
                                     5 个关键词作为 tags
```

## 集成点

**唯一改动文件:** `news/crawler.py`

**改动位置:** `_download_and_parse()` 方法，[line 396](news/crawler.py#L396)

```
当前:
    item["tags"] = parsed.get("tags", [])

改动后:
    item["tags"] = parsed.get("tags", [])
    if not item["tags"] and item.get("content"):
        item["tags"] = _extract_keywords_textrank(item["content"])
```

**为何选此处而非 parser：**
- Parser 的职责是"从 HTML 提取内容"，不应感知 NLP 策略
- Crawler 的 `_download_and_parse` 是 content enrichment 的汇聚点 — 所有路径（daemon、cloud sync、refetch）都经过它
- 在此 fallback，无论怎么来的文章都受益

## jieba TextRank 配置

```python
import jieba.analyse

def _extract_keywords_textrank(content: str, topk: int = 5) -> list[str]:
    """从 Markdown 正文提取关键词，jieba TextRank fallback。"""
    # 去除 Markdown 语法噪音（链接、图片、格式标记）
    text = re.sub(r'!\[.*?\]\(.*?\)', '', content)   # 图片
    text = re.sub(r'\[([^\]]*)\]\(.*?\)', r'\1', text)  # 链接保留文字
    text = re.sub(r'[#*>`|~\-_]', ' ', text)           # 格式标记
    text = re.sub(r'\s+', ' ', text).strip()
    
    if len(text) < 50:
        return []
    
    # TextRank — 词性过滤：地名/名词/动名词/人名/机构名
    keywords = jieba.analyse.textrank(
        text,
        topK=topk,
        withWeight=False,
        allowPOS=('ns', 'n', 'vn', 'nr', 'nt', 'nz'),
    )
    return keywords
```

### 设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 算法 | TextRank | 无需训练语料，比 TF-IDF 更适合单文档 |
| 词性过滤 | `ns/n/vn/nr/nt/nz` | 过滤掉副词、形容词、虚词等无意义词 |
| TopK | 5 | 与澎湃新闻编辑标注数量一致 |
| 最小正文长度 | 50 字符 | 过短的正文提取不出有意义的关键词 |
| Markdown 预处理 | strip 语法标记 | 提高分词质量，避免 `**` `#` 干扰 |
| 导入方式 | lazy import | `jieba` 是新依赖，不应在 import 时强制加载 |

### 允许的词性

| POS | 含义 | 示例 |
|-----|------|------|
| `ns` | 地名 | 北京、上海、乌克兰 |
| `n` | 普通名词 | 世界杯、无人机、政策 |
| `vn` | 动名词 | 调查、改革、制裁 |
| `nr` | 人名 | 特朗普、梁朝伟 |
| `nt` | 机构名 | 外交部、欧盟 |
| `nz` | 其他专名 | G7、VAR |

## 数据流（完整）

```
┌─ Hot-list API ─────────────────────────────────────┐
│  title, url, rank, published_at                     │
│  tags: []  (永远是空的)                               │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─ fetch_all(with_content=True) ──────────────────────┐
│  1. persist metadata (tags 为空)                     │
│  2. enrich_content()  ──────────────────────┐       │
└──────────────────────────────────────────────┼───────┘
                                               │
                                               ▼
┌─ _run_batch_parse() ─────────────────────────────────┐
│  ThreadPoolExecutor → _download_and_parse()          │
│    │                                                 │
│    ├─ 下载 HTML                                      │
│    ├─ parser.parse(html) → markdown + metadata       │
│    │                                                 │
│    ├─ tags = parsed.tags                             │
│    │   (trafilatura → meta keywords → 澎湃新闻有值)    │
│    │                                                 │
│    └─ if not tags and content:                       │
│         tags = _extract_keywords_textrank(content)   │
│         (jieba TextRank → 5 个关键词)                 │
└──────────────────────────────────────────────────────┘
                       │
                       ▼
┌─ persist(upsert) → PostgreSQL ─────────────────────┐
│  INSERT ... ON CONFLICT ... DO UPDATE               │
│  tags = text[] (现在所有来源都有值)                    │
└────────────────────────────────────────────────────┘
```

## 依赖

**新增依赖:** `jieba`（纯 Python，无 C 扩展，约 15MB 词典数据）

```toml
# pyproject.toml
dependencies = [
    # ...现有依赖...
    "jieba>=0.42",
]
```

```bash
uv sync  # 自动安装
```

jieba 首次加载时会初始化词典（~200ms），之后每次调用 `textrank()` 约 10-50ms（取决于文章长度）。

## 测试策略

### 单元测试 (tests/test_keywords.py)

```python
def test_textrank_extracts_from_chinese_news():
    """正常中文新闻正文 → 5 个关键词"""
    content = """
    美国前总统特朗普在G7峰会期间与日本首相高市早苗会谈，
    双方讨论了贸易和安全议题。特朗普表示...
    """
    tags = _extract_keywords_textrank(content)
    assert len(tags) == 5
    assert '特朗普' in tags

def test_short_content_returns_empty():
    """正文小于 50 字 → 返回空列表"""
    assert _extract_keywords_textrank("短文章。") == []

def test_empty_content_returns_empty():
    assert _extract_keywords_textrank("") == []

def test_markdown_noise_stripped():
    """Markdown 语法标记被正确去除"""
    content = "**特朗普** [链接](url) ![图](img.png) 访问北京"
    tags = _extract_keywords_textrank(content)
    assert '特朗普' in tags or '北京' in tags
    assert 'url' not in tags
```

### 集成测试

- daemon crawl → 验证非澎湃新闻来源也有 tags
- refetch → 验证 tags 被正确更新

## 向后兼容

- 澎湃新闻的编辑标注 tags **不受影响** — trafilatura 提取到了就不走 jieba
- `_build_result` 中的 `#` 前缀 strip 逻辑不受影响（jieba 不会生成 `#` 前缀）
- PostgreSQL schema 不变 — `tags text[]` 字段已存在

## 风险

| 风险 | 缓解 |
|------|------|
| jieba 词典首次加载慢 | lazy import，只在 fallback 时加载 |
| TextRank 对短文章效果差 | 正文 < 50 字符跳过 |
| 多线程并发分词 | jieba 分词是线程安全的（内部有锁） |
| 提取出无意义词 | `allowPOS` 词性过滤 + 后续可加 stop words |
| 新依赖增加安装复杂度 | `jieba` 是 PyPI 最流行的中文 NLP 库，纯 Python，无系统依赖 |
