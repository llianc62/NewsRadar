# Parser — HTML 内容提取

将各新闻源的 HTML 页面转为统一的 Markdown + 元数据结构。

## 架构

```
news/parser/
├── __init__.py          # 导出 registry 单例
├── parser.py            # HtmlParser 基类（~347 行）
├── registry.py          # Registry — source_id/hostname 路由
└── sites/               # 12 个站点特定解析器
    ├── __init__.py      # 注册所有站点解析器
    ├── wallstreetcn.py  # 华尔街见闻（~261 行）
    ├── thepaper.py      # 澎湃新闻（~257 行）
    ├── zaobao.py        # 早报（~234 行）
    ├── cls.py           # 财联社（~206 行）
    ├── ithome.py        # IT之家（~118 行）
    ├── cankaoxiaoxi.py  # 参考消息（~88 行）
    ├── ifeng.py         # 凤凰网（~79 行）
    ├── fastbull.py      # Fastbull（~32 行）
    ├── sspai.py         # 少数派（~9 行）
    ├── kaopu.py         # 靠谱新闻（~9 行）
    └── juejin.py        # 掘金（~9 行）
```

## 提取流水线

`HtmlParser.parse(html, url)` 按以下顺序尝试：

```
_preprocess(html, url)     # 1. 站点可覆写的 DOM 清理
    ↓
_extract(html, url)        # 2. 站点可覆写的自定义提取（SPA/JSON）
    ↓ 返回 None 则降级
_extract_with_readability  # 3. readability-lxml + markdownify + trafilatura 元数据
    ↓ 失败则降级
_fallback                  # 4. 裸 HTML 标签剥离 + meta 提取
```

每层都会调用 `_build_result()` 生成统一 dict：
```python
{"markdown": str, "title": str, "author": str,
 "published_at": str, "summary": str, "category": str, "tags": list[str]}
```

## 两个扩展钩子

站点解析器继承 `HtmlParser`，只需覆写：

- **`_preprocess(html, url) → html`** — DOM 清理（删除广告 div、无关 script 等）
- **`_extract(html, url) → dict | None`** — 自定义提取（SPA 的 `__NEXT_DATA__`、JSON-LD 等）；返回 `None` 则走默认 readability 链

## Registry 路由

`registry.parse(source_id, html, url)` 三级路由：

1. **source_id 精确匹配** — `"thepaper"` → `ThepaperParser`
2. **URL hostname 域名匹配** — `wallstreetcn.com` → `WallstreetcnParser`（含父域名回退：`www.wallstreetcn.com` → `wallstreetcn.com`）
3. **默认兜底** — `HtmlParser` 基类实例

注册在 `sites/__init__.py` 中集中完成，模块导入时自动填充全局 `registry` 单例。

## readability 路径细节

`_extract_with_readability()`:
1. `readability-lxml` 提取正文 HTML（`Document.summary()`）
2. `markdownify` 转 Markdown（ATX 标题，strip script/style）
3. 截断 H1 之前的页面头部噪音
4. `trafilatura.extract_metadata()` 提取作者/日期/摘要/分类/tags
5. `_beautify_markdown_formatting()` — 规范化 `**bold**` 标记 + 去除澎湃新闻 `- +1` 点赞噪音
6. `_trim_noise()` — 基于块级 DOM 分析和链接密度的尾部噪音裁剪

## Fallback 路径

当 readability 失败时：正则剥离 `<script>/<style>/<nav>/<header>/<footer>/<aside>` → 去除所有 HTML 标签 → `html.unescape` → 按段落长度过滤（>80 字符）→ 从 `<meta>` 提取标题/作者/描述/发布时间。

## 添加新站点解析器

1. 创建 `news/parser/sites/<site>.py`，继承 `HtmlParser`
2. 覆写 `_extract()` 和/或 `_preprocess()`
3. 在 `news/parser/sites/__init__.py` 中注册：
   ```python
   registry.register("source_id", SiteParser(), domains=["example.com"])
   ```
4. 在 `tests/parser_sites/` 添加真实 HTML fixture 测试

## 关键文件

| 文件 | 用途 |
|------|------|
| `news/parser/parser.py` | HtmlParser 基类 — 完整提取流水线 |
| `news/parser/registry.py` | 三级路由 + 全局 registry 单例 |
| `news/parser/sites/__init__.py` | 所有站点解析器注册 |
| `tests/test_parser_*.py` | 解析器单元测试（9 个文件） |
| `tests/parser_sites/` | 站点解析器 fixture 测试（12 个站点） |
