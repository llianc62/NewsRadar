# 正文提取噪音削减 — 掐头去尾方案

> 日期: 2026-06-17
> 状态: 已确认

## 问题

`trafilatura.extract` 在提取中文新闻正文时，会将页面头尾的非正文元素（页脚版权信息、"分享到"按钮文字、面包屑导航残留、底部推荐链接等）混入 Markdown 输出。

## 核心洞察

**噪音只出现在正文开始之前和正文结束之后，正文内部不会被 CMS 后期插入噪音元素。** 因此不需要逐元素判断"是不是噪音"，只需找到正文的两个边界点。

## 方案：掐头去尾

### 管线位置

在 `_extract_with_trafilatura` 内部，将原始 HTML 先通过 `_trim_noise` 预处理，得到干净 HTML 后再交给 trafilatura。预处理失败时退化回原始 HTML。

```
HTML → _trim_noise() → 干净 HTML → trafilatura → Markdown
                ↓ 失败
            原始 HTML → trafilatura → Markdown
```

### 块提取

从 DOM 中按文档序提取候选块级文本节点：

标签白名单：`<p> <h1>-<h6> <blockquote> <ul> <ol> <pre>`

对每个候选块计算：
- `text_len`: 纯文本字符数
- `link_density`: 链接内文字数 / 总文字数
- `html`: 原始 inner HTML（保留结构，供 trafilatura 格式转换）

### 边界检测

**掐头** — 从前向后扫描，找到第一个满足以下任一条件的块：

| 条件 | 理由 |
|------|------|
| `text_len >= 80` 且 `link_density < 0.3` | 第一个"像段落的段落" |
| `tag in ('h1','h2','h3')` 且 `text_len >= 10` | 第一个正文级标题 |

**去尾** — 从后向前扫描，找到第一个满足以下条件的块：

| 条件 | 理由 |
|------|------|
| `text_len >= 50` 且 `link_density < 0.3` | 最后一个"像段落的段落" |
| `tag in ('h1','h2','h3','h4')` | 最后一个正文标题 |

### 退化条件

以下情况返回 `None`，走原始 trafilatura：

- 总块数 < 3（页面太短，不值得处理）
- `start > end`（头尾重叠，检测矛盾）
- `start == 0` 且 `end == len(blocks)-1`（没有找到边界）
- lxml 解析失败

### 与现有管线的关系

- 只在 `_extract_with_trafilatura` 中生效，`_fallback` 和 `_extract_spa_data` 不受影响
- `_beautify_markdown_formatting` 继续保留（格式规范化）
- 标题仍从原始 HTML 的 `<title>` / `og:title` 提取
- 不引入新依赖（lxml 已存在）

## 实现范围

- 新增 `_trim_noise()` 方法（约 60 行）
- 新增 `Block` 数据类
- 修改 `_extract_with_trafilatura()` 调用处（约 5 行）
- 新增单元测试（约 40 行）
