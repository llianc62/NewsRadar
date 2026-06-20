# Parser 全量测试覆盖 — 设计文档

> **状态:** 草稿
> **日期:** 2026-06-20
> **目标:** 为 `news/parser.py` 的所有解析路径、边界处理和已知 bug 场景设计并编写测试用例

## 背景

[news/parser.py](../../news/parser.py) 是 NewsRadar 最复杂的模块之一，负责将 HTML 解析为 Markdown+元数据。经过 15+ 次 commit 迭代，当前共约 900 行代码，涵盖 6 大子系统、70+ 个处理场景。现有测试仅有 [tests/test_parser.py](../../tests/test_parser.py) 中的 11 个测试用例，全部聚焦于 `_trim_noise` 方法，其他子系统零覆盖。

## 目标

为所有 74 个场景编写测试用例，覆盖率达到 80%+。

---

## 设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 文件组织 | 按子系统拆分 9 个测试文件 | 每个文件聚焦、<300 行、可独立运行 |
| Fixture 策略 | `conftest.py` 共享 HTML 构建工具 + 内联 HTML 字符串 | 比外部 fixture 文件更可读，失败定位更快 |
| 测试风格 | pytest 函数 + 类组织 | 与现有 `test_parser.py` 保持一致 |
| SPA 测试 | 用真实结构的 HTML 字符串模拟 | 关键路径用完整 HTML，辅助函数用简化输入 |
| 覆盖率目标 | 80%+ 行覆盖 | 匹配项目测试标准 |

---

## 文件结构

```
tests/
├── conftest.py                        # 共享 fixtures（新建）
├── test_parser_trim_noise.py          # A. Noise Trimming（已有, 重命名, 补充 8 个）
├── test_parser_spa.py                 # B. SPA Data 提取（新建, 17 个）
├── test_parser_trafilatura.py         # C. trafilatura 路径（新建, 10 个）
├── test_parser_fallback.py            # D. Fallback HTML strip（新建, 5 个）
├── test_parser_lazy_images.py         # E. 懒加载图片修复（新建, 3 个）
├── test_parser_beautify.py            # F. Markdown 美化（新建, 4 个）
├── test_parser_build_image.py         # G. 图片兜底构建（新建, 4 个）
├── test_parser_json_helpers.py        # H. JSON 解析辅助（新建, 5 个）
└── test_parser_edge_cases.py          # I. 边界条件/截断/标题/结果构建（新建, 6 个）
```

---

## 各模块详细设计

### A. Noise Trimming — `test_parser_trim_noise.py`（20 个：11 已有 + 9 补充）

**现状:** 11 个已有测试，覆盖基本场景。重命名 `test_parser.py` → `test_parser_trim_noise.py`。

**已有测试（不变）:**

| # | 测试 | 覆盖场景 |
|---|------|----------|
| 1 | `test_keeps_paragraph_body` | 正文段落完整保留 |
| 2 | `test_trims_footer_copyright` | 尾部版权信息移除 |
| 3 | `test_trims_head_navigation` | 头部导航链接移除 |
| 4 | `test_trims_share_buttons_before_body` | 分享按钮移除 |
| 5 | `test_short_page_degrades_to_none` | 过短页面 → None |
| 6 | `test_malformed_html_degrades_to_none` | 非法 HTML → None |
| 7 | `test_body_with_h1_heading_kept` | h1 标题保留（最高优先级） |
| 8 | `test_h2_fallback_when_no_paragraph` | 无 h1/长段落时 h2 作为起始信号 |
| 9 | `test_link_density_detects_noise` | 高链接密度 → 噪声 |
| 10 | `test_preserves_figure_with_image` | `<figure>` 含 `<img>` 保留 |
| 11 | `test_preserves_image_inside_paragraph` | `<p>` 含 `<img>` 保留 |

**补充测试（9 个）:**

| # | 测试 | 覆盖场景 | 关联 commit |
|---|------|----------|-------------|
| 12 | `test_short_page_with_h1_not_degraded` | 有 h1 的短页面不降级 | — |
| 13 | `test_h4_h5_h6_skipped_as_footer_headings` | h4/h5/h6 被跳过（"扫码下载"等） | — |
| 14 | `test_start_gt_end_degrades_to_none` | start > end 重叠 → None | 25b6320 |
| 15 | `test_no_blocks_degrades_to_none` | 无 block 标签 → None | — |
| 16 | `test_preserves_nested_div_between_boundaries` | DOM 剪枝保留 `<div>` 内原始格式 | 25b6320 |
| 17 | `test_removes_meta_wrapper_different_parents` | h1 和 content div 不同 parent 时移除 metadata | c0b643c |
| 18 | `test_short_copyright_line_trimmed` | `© 2024 某某` < 30 chars → 尾噪声 | — |
| 19 | `test_long_paragraph_as_end_signal` | ≥ 50 chars + link_density < 0.3 → 尾边界 | — |
| 20 | `test_output_wrapped_in_article` | 输出包裹 `<html><body><article>` | 571595f |

**共享工具函数（迁至 `conftest.py`）:**

```python
def make_html(body: str, head_noise: str = "", tail_noise: str = "") -> str:
    """构建最小 HTML 页面，在 body 内包含可选的头部/尾部噪声。
    从 tests/test_parser.py 的 _make_html 迁出，改为公开函数供所有测试文件使用。"""
```

---

### B. SPA Data 提取 — `test_parser_spa.py`（17 个，新建）

**测试 HTML Fixture 策略:**
- `_find_json_candidates` 和 `_find_article_in_json` 用精确的最小 HTML 字符串测试
- `_extract_spa_data` 主流程用完整的 SPA 页面 HTML 端到端测试

#### B1. JSON 候选发现

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 1 | `test_finds_next_data_js_assignment` | `__NEXT_DATA__ = {"props":{...}}` | 找到并解析 JSON |
| 2 | `test_finds_next_data_script_tag` | `<script id="__NEXT_DATA__" type="application/json">{...}</script>` | 找到并解析 JSON |
| 3 | `test_finds_ssr_assignment` | `__SSR__ = {"article":{...}}` | 找到并解析 JSON |
| 4 | `test_finds_nuxt_assignment` | `__NUXT__ = {"state":{...}}` | 找到并解析 JSON |
| 5 | `test_finds_json_ld_article` | `<script type="application/ld+json">{"@type":"Article",...}</script>` | 找到 JSON-LD |
| 6 | `test_handles_malformed_json_gracefully` | `__NEXT_DATA__ = {broken...` | 返回 None，不抛异常 |

#### B2. 文章递归搜索

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 7 | `test_finds_article_by_title_and_content` | `{"a":{"title":"t","content":"c...50+"}}` | 返回该对象 |
| 8 | `test_finds_jsonld_by_headline_and_article_body` | `{"@type":"Article","headline":"t","articleBody":"b"}` | 返回正确字段映射 |
| 9 | `test_picks_longest_content_when_multiple` | 两个 title+content 对象 | 选 content 最长的 |
| 10 | `test_extracts_jsonld_keywords_list` | `"keywords":["a","b"]` | tags = ["a","b"] |
| 11 | `test_extracts_jsonld_keywords_comma_string` | `"keywords":"a,b,c"` | tags = ["a","b","c"] |

#### B3. SPA content → Markdown 主流程

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 12 | `test_strips_blockquote_before_trafilatura` | content 含 `<blockquote><img src="x.jpg"></blockquote>` | 最终 markdown 含 `![](x.jpg)` |
| 13 | `test_preserves_bare_img_in_html_fragment` | content 含 `<p>文字</p><img src="x.jpg"><p>文字</p>` | 最终 markdown 含 `![](x.jpg)` |
| 14 | `test_falls_back_to_image_markdown_for_image_heavy` | content 仅 `<img>` 标签，文字 < 50 字符 | 触发 `_build_image_markdown` |
| 15 | `test_extracts_summary_from_og_description` | content 含关键词但 JSON 无 description | summary 来自 `<meta og:description>` |
| 16 | `test_skips_candidate_with_short_content` | content 字段 < 50 字符 | 跳过该 candidate |
| 17 | `test_integration_thepaper_next_data` | 完整 thepaper.cn HTML（嵌入 `__NEXT_DATA__`） | parse → markdown 含标题+图片+正文 |

**Fixture 示例（测试 17 的 HTML）:**

```python
THEPAPER_HTML = """<!DOCTYPE html>
<html>
<head>
<title>测试标题 - 澎湃新闻</title>
<meta name="description" content="测试摘要">
</head>
<body>
<script id="__NEXT_DATA__" type="application/json">{
  "props": {"pageProps": {"article": {
    "title": "测试文章标题",
    "content": "<p>正文段落。</p><img src=\\"https://x.com/photo.jpg\\" alt=\\"配图\\"><p>更多内容。</p>",
    "keywords": ["时政","经济"],
    "datePublished": "2026-06-15T10:00:00+08:00",
    "description": ""
  }}}
}</script>
</body>
</html>"""
```

---

### C. trafilatura 路径 — `test_parser_trafilatura.py`（10 个，新建）

| # | 测试 | 场景 | 关键验证 |
|---|------|------|----------|
| 1 | `test_extracts_content_from_full_page` | 完整 HTML → markdown | markdown 非空、含正文 |
| 2 | `test_returns_none_for_short_content` | markdown < 50 字符 | 返回 None |
| 3 | `test_title_prefers_h1_over_og_title` | markdown 含 `# 文章标题` + `<meta og:title="文章标题 | 网站名">` | title = "文章标题"（无后缀） |
| 4 | `test_metadata_exception_handled` | trafilatura metadata 抛异常 | 不崩溃，返回基础结果 |
| 5 | `test_skip_trim_respected` | `skip_trim=True` | `_trim_noise` 不被调用 |
| 6 | `test_trim_applied_when_not_skipped` | `skip_trim=False`（默认） | `_fix_lazy_images` + `_trim_noise` 先执行 |
| 7 | `test_extracts_categories_and_tags` | HTML 含 `<meta name="keywords">` | tags 正确解析 |
| 8 | `test_deduplicates_tags` | categories[1:] 和 tags 有重叠 | 最终 tags 无重复 |
| 9 | `test_extracts_author_date_description` | HTML 含 author/date/description meta | 全部提取 |
| 10 | `test_beautify_applied_to_output` | 输入含 `**text **` | 输出为 `**text**` |

---

### D. Fallback HTML Strip — `test_parser_fallback.py`（5 个，新建）

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 1 | `test_strips_non_content_tags` | `<script>...</script><style>...</style><nav>...</nav><footer>...</footer>` + 正文 | 正文保留，标签移除 |
| 2 | `test_extracts_meta_fields` | `<meta name="author" content="张三"><meta name="description" content="摘要"><meta property="article:published_time" content="2026-01-01">` | author/description/published_at 提取 |
| 3 | `test_filters_short_paragraphs` | 多个段落，仅 1 个 > 80 字符 | 只有长段落保留 |
| 4 | `test_returns_none_for_short_content` | 总内容 ≤ 100 字符 | None |
| 5 | `test_extracts_title` | `<title>文章标题</title><meta property="og:title" content="OG 标题">` | title 提取正确 |

---

### E. 懒加载图片 — `test_parser_lazy_images.py`（3 个，新建）

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 1 | `test_swaps_data_src_with_data_uri_placeholder` | `<img data-src="real.jpg" src="data:image/gif;base64,R0lGODlh...">` | `<img src="real.jpg">` |
| 2 | `test_swaps_data_original_with_data_uri_placeholder` | `<img data-original="real.jpg" src="data:image/png;base64,...">` | `<img src="real.jpg">` |
| 3 | `test_preserves_normal_img_unchanged` | `<img src="real.jpg" alt="正常图片">` | 不变 |

> **已知局限（记录为测试 3 的补充注释）:** 正则要求 `src` 是 `data:image/...` 才替换。若占位符是 `blank.gif` 等非 data URI 格式，当前正则不匹配。此项为未来改进点，非本次修复目标。

---

### F. Markdown 美化 — `test_parser_beautify.py`（4 个，新建）

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 1 | `test_normalizes_stray_spaces_in_bold_markers` | `** text** 和 **text **` | 均变为 `**text**` |
| 2 | `test_adds_space_around_bold_adjacent_text` | `是**text**普` | `是 **text** 普` |
| 3 | `test_removes_praise_button` | `- +1\n\n# 标题` | `# 标题` |
| 4 | `test_idempotent_on_clean_markdown` | 已格式正确的 markdown | 不变 |

---

### G. 图片 Markdown 构建 — `test_parser_build_image.py`（4 个，新建）

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 1 | `test_extracts_img_to_markdown_syntax` | `<img src="x.jpg" alt="alt">` | `![](x.jpg)` |
| 2 | `test_preserves_remaining_text` | `<img src="x.jpg"><p>说明文字</p>` | 输出含 `![](x.jpg)` + "说明文字" |
| 3 | `test_handles_multiple_images` | `<img src="a.jpg"><img src="b.jpg">` | 两个 `![]()` 各行 |
| 4 | `test_returns_empty_for_no_images_or_text` | `<div></div>` | 空字符串 |

---

### H. JSON 解析辅助 — `test_parser_json_helpers.py`（5 个，新建）

测试 `_extract_bracketed_json` 和 `_extract_json_ld` 两个私有方法。

| # | 测试 | 输入 | 预期 |
|---|------|------|------|
| 1 | `test_bracket_match_simple` | `var = {"key":"value"};` | 解析为 `{"key":"value"}` |
| 2 | `test_bracket_match_nested` | `{"a":{"b":{"c":1}}}` | 正确匹配最外层括号 |
| 3 | `test_bracket_match_unclosed_returns_empty` | `{"key":"value"` | 返回 [] |
| 4 | `test_json_ld_extracts_multiple_script_tags` | 两个 `<script type="application/ld+json">` 标签 | 两个 JSON 都解析 |
| 5 | `test_json_ld_skips_invalid_json` | `<script type="application/ld+json">{invalid}</script>` | 跳过，不抛异常 |

---

### I. 边界条件 — `test_parser_edge_cases.py`（6 个，新建）

| # | 测试 | 场景 |
|---|------|------|
| 1 | `test_truncates_content_over_max_length` | content > max_content_length → 截断 + `... (truncated)` |
| 2 | `test_extract_heading_returns_first_h1` | `# 标题\n## 副标题` → "标题" |
| 3 | `test_extract_heading_empty_when_no_h1` | 无 `# ` 行 → "" |
| 4 | `test_build_result_strips_hash_from_tags` | `["#tag1","#tag2"]` → `["tag1","tag2"]` |
| 5 | `test_build_result_removes_empty_strings` | `["#","tag"]` → `["tag"]` |
| 6 | `test_parse_returns_none_for_empty_html` | `""` → None |

---

### conftest.py — 共享 Fixtures（新建）

```python
import pytest


def make_html(body: str, head_noise: str = "", tail_noise: str = "") -> str:
    """构建最小 HTML 页面，在 body 内包含可选的头部/尾部噪声。"""
    return f"""<!DOCTYPE html>
<html>
<head><title>Test Article</title></head>
<body>
<article>
{head_noise}
{body}
{tail_noise}
</article>
</body>
</html>"""


@pytest.fixture
def parser():
    """返回默认配置的 HtmlParser 实例。"""
    from news.parser import HtmlParser
    return HtmlParser()
```

---

## 实现顺序

按复杂度和依赖关系排列：

| 优先级 | 模块 | 文件 | 场景数 | 理由 |
|--------|------|------|--------|------|
| **P0** | conftest | `conftest.py` | — | 基础设施，所有测试依赖 |
| **P1** | Lazy Images | `test_parser_lazy_images.py` | 3 | 最简单，纯函数，先积累节奏 |
| **P1** | Beautify | `test_parser_beautify.py` | 4 | 纯文本处理，无外部依赖 |
| **P1** | JSON Helpers | `test_parser_json_helpers.py` | 5 | 纯函数，输入输出明确 |
| **P1** | Edge Cases | `test_parser_edge_cases.py` | 6 | 边界条件，简单直接 |
| **P2** | Fallback | `test_parser_fallback.py` | 5 | 需要 HTML fixture |
| **P2** | Image Building | `test_parser_build_image.py` | 4 | 静态方法，无状态 |
| **P3** | trafilatura | `test_parser_trafilatura.py` | 10 | 需要完整 HTML，可能较慢 |
| **P3** | SPA Data | `test_parser_spa.py` | 17 | 最复杂，需要完整 SPA HTML |
| **P4** | Trim Noise 补充 | `test_parser_trim_noise.py` | +8 | 扩展已有文件，需兼容现有测试 |

---

## 覆盖目标

| 模块 | 目标覆盖率 |
|------|-----------|
| `_fix_lazy_images` | 100% |
| `_beautify_markdown_formatting` | 100% |
| `_build_image_markdown` | 100% |
| `_extract_bracketed_json` | 100% |
| `_extract_json_ld` | 100% |
| `_find_json_candidates` | 90%+ |
| `_find_article_in_json` | 90%+ |
| `_extract_spa_data` | 85%+ |
| `_extract_with_trafilatura` | 80%+ |
| `_trim_noise` | 85%+ |
| `_fallback` | 80%+ |
| `parse` | 90%+ |
| **整体** | **80%+** |

---

## 不变

- 现有 `test_parser.py` 的 12 个测试完整保留，仅重命名文件
- `HtmlParser` 公共 API 不变
- pytest 配置不变（无 `setup.cfg` / `conftest.py` 冲突）
- 测试运行命令不变：`pytest` / `pytest tests/test_parser_*.py`

## 影响范围

- 仅新增 `tests/` 下的测试文件
- `news/parser.py` 不修改（已有未提交的 SPA image fix 保留）
- 无依赖变更

## 风险

| 风险 | 缓解 |
|------|------|
| trafilatura 测试慢（真实 HTML 解析） | 用最小 HTML fixture，避免全页 |
| SPA 测试 HTML 字符串转义复杂 | 使用 Python raw string + 最小化 JSON 嵌套 |
| 测试间 fixture 污染 | HtmlParser 无状态，每次新建实例 |
| 覆盖率数字不达标 | P4 阶段针对性补充，必要时放宽阈值 |
