# Parser 输出质量验证与修复计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对 11 个站点 Parser 逐一进行 grab-one 在线验证，检查输出 Markdown 的内容完整性和格式正确性，修复发现的问题，并为缺少真实 fixture 测试的 Parser 补齐测试。

**Architecture:** 分两阶段：Phase 1 快速摸底（并发 grab-one 所有 11 个站点），Phase 2 逐个修复（按问题严重程度排序）。

**Tech Stack:** Python 3.12+, pytest, requests, newsnow API (`https://newsnow.busiyi.world/api/s?id=<source_id>&latest`)

---

## 当前状态概览

| Parser | 代码行数 | 自定义逻辑 | 真实 fixture | fixture 测试 | 状态 |
|--------|---------|-----------|-------------|-------------|------|
| IfengParser | 79 | `_preprocess`(lxml DOM清理+懒加载图片) | ✅ 55565B | 11 个 | 已验证 |
| IthomeParser | 114 | `_preprocess`(懒加载图片) + `_extract`(regex提取#paragraph) | ❌ | 0 个 | **需验证** |
| CkxxappParser | 88 | `_extract`(JS变量提取) | ✅ 1393B(合成) | 6 个 | **需验证** |
| ClsParser | 206 | `_extract`(__NEXT_DATA__) | ✅ 17936B | 9 个 | 已验证 |
| ZaobaoParser | 234 | `_extract`(JSON-LD + div.articleBody) | ✅ 179192B | 10 个 | 已验证 |
| ThepaperParser| 257 | `_extract`(__NEXT_DATA__) | ✅ 29006B | 4 个 | 已验证 |
| WallstreetcnParser| 261 | `_extract`(__SSR__) | ✅ 39594B | 10 个 | 已验证 |
| **FastbullParser** | 9 | 无(纯Stub) | ❌ | 0 个 | **需验证** |
| **JuejinParser** | 9 | 无(纯Stub) | ❌ | 0 个 | **需验证** |
| **KaopuParser** | 9 | 无(纯Stub) | ❌ | 0 个 | **需验证** |
| **SspaiParser** | 9 | 无(纯Stub) | ❌ | 0 个 | **需验证** |

**已有 fixture 的 Parser**（5个）：ifeng, cls, zaobao, thepaper, wallstreetcn — 但 still need grab-one 在线验证确认输出质量。

**没有 fixture 的 Parser**（6个）：ithome, cankaoxiaoxi, fastbull, kaopu, sspai, juejin — 需要保存真实 HTML + 写 fixture 测试。

---

## 验证标准

对每个站点 Parser 的 grab-one 输出检查：

- [ ] **内容完整性** — 正文内容完整（>200字符），没有截断
- [ ] **图片处理** — 图片显示真实 URL，不是占位符
- [ ] **标题正确** — 标题来自文章页面，不是网站名
- [ ] **噪声清除** — 无导航栏、版权声明、广告、评论区等噪声
- [ ] **格式干净** — Markdown 格式正确，无乱码，无 HTML 残留
- [ ] **作者/日期** — metadata 字段有值（非必须，有则更好）

---

### Task 1: 快速摸底 — 并发 grab-one 所有站点

直接用 newsnow API 获取最新文章链接，批量 grab-one 保存输出到临时文件，快速评估所有站点。

**已获取的 URL：**

| source_id | URL |
|-----------|-----|
| wallstreetcn-hot | https://wallstreetcn.com/articles/3775351 |
| cls-hot | https://www.cls.cn/detail/2407859 |
| thepaper | https://www.thepaper.cn/newsDetail_forward_33439602 |
| zaobao | https://www.zaochenbao.com/news/politics/202606/2474898.html |
| cankaoxiaoxi | https://ckxxapp.ckxx.net/pages/2026/06/24/3c51c84696714b3c83ecb16ac8414d79.html |
| kaopu | https://kaopu.news/story/2026-06-24/... |
| fastbull-news | https://www.fastbull.com/cn/news-detail/4380496_1 |
| ifeng | https://news.ifeng.com/c/8uDdI76KjZE |
| ithome | https://www.ithome.com/0/968/171.htm |
| sspai | https://sspai.com/post/111216 |
| juejin | https://juejin.cn/post/7654102171461402662 |

- [ ] **Step 1: 并发运行 grab-one 保存输出**

```bash
mkdir -p /tmp/parser-verify

# 并发执行（每个超时 60s），输出保存到独立文件
for url_info in \
  "wallstreetcn:https://wallstreetcn.com/articles/3775351" \
  "cls:https://www.cls.cn/detail/2407859" \
  "thepaper:https://www.thepaper.cn/newsDetail_forward_33439602" \
  "zaobao:https://www.zaochenbao.com/news/politics/202606/2474898.html" \
  "cankaoxiaoxi:https://ckxxapp.ckxx.net/pages/2026/06/24/3c51c84696714b3c83ecb16ac8414d79.html" \
  "kaopu:https://kaopu.news/story/2026-06-24/%E4%BC%8A%E6%9C%97%E5%85%B3%E9%97%AD%E9%9C%8D%E5%B0%94%E6%9C%A8%E5%85%B9%E6%B5%B7%E5%B3%A1-%E7%BE%8E%E4%BC%8A%E5%8F%8C%E6%96%B9%E8%AF%B4%E6%B3%95%E4%B8%8D%E4%B8%80-e38906" \
  "fastbull:https://www.fastbull.com/cn/news-detail/4380496_1" \
  "ifeng:https://news.ifeng.com/c/8uDdI76KjZE" \
  "ithome:https://www.ithome.com/0/968/171.htm" \
  "sspai:https://sspai.com/post/111216" \
  "juejin:https://juejin.cn/post/7654102171461402662"
do
  name="${url_info%%:*}"
  url="${url_info#*:}"
  (python -m cli grab-one "$url" -o markdown > /tmp/parser-verify/${name}.md 2>&1) &
done
wait

# 检查每个输出文件的大小和内容
for f in /tmp/parser-verify/*.md; do
  echo "=== $(basename $f) === $(wc -c < $f) bytes ==="
  head -3 "$f"
  echo "..."
  echo ""
done
```

- [ ] **Step 2: 逐文件评估输出质量**

对每个 `/tmp/parser-verify/<source>.md` 检查：
1. 是否有真实图片 URL（不是 base64 占位符、不是 `data:image`）
2. 正文内容是否 >200 字符
3. 是否有明显的导航/页脚噪声
4. 标题是否来自文章（不是网站名）

- [ ] **Step 3: 记录问题清单**

将问题归类：
- **P0 (阻塞):** 内容为空/截断，完全不可用
- **P1 (高优):** 图片占位符未替换，主要噪声未清除
- **P2 (中优):** 元数据缺失，部分格式问题
- **P3 (低优):** Stub 够用，不需要自定义逻辑

---

### Task 2: 修复 IthomeParser fixture 缺失 + 补充测试

**问题:** IthomeParser 只有 2 个基础测试（空HTML + 简单HTML），没有真实 fixture 测试。上次只修复了图片懒加载问题，但没有保存 HTML fixture 作为回归测试。

**Files:**
- Create: `tests/parser_sites/fixtures/ithome.html`
- Modify: `tests/parser_sites/test_ithome.py`

- [ ] **Step 1: 保存真实 HTML fixture**

```bash
python -c "
import requests
resp = requests.get(
    'https://www.ithome.com/0/968/171.htm',
    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
    timeout=30
)
with open('tests/parser_sites/fixtures/ithome.html', 'w', encoding='utf-8') as f:
    f.write(resp.text)
print(f'Saved {len(resp.text)} bytes')
"
```

- [ ] **Step 2: 编写 fixture 测试**

```python
# 追加到 tests/parser_sites/test_ithome.py（文件顶部添加 imports）
from pathlib import Path
from lxml import html as lxml_html

FIXTURES = Path(__file__).parent / "fixtures"


class TestIthomeParserFixture:
    """Test with real ithome.com HTML fixture."""

    def test_extracts_content_from_real_fixture(self):
        html = (FIXTURES / "ithome.html").read_text(encoding="utf-8")
        parser = IthomeParser()
        result = parser.parse(html, url="https://www.ithome.com/0/968/171.htm")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]
        # 图片应该是真实 URL，不是透明占位符
        assert "img.ithome.com/images/v2/t.png" not in result["markdown"]

    def test_fix_lazy_images_converts_srcset(self):
        html = '<img srcset="https://img.example.com/photo.jpg 2x" src="placeholder.png">'
        tree = lxml_html.fromstring(html)
        count = IthomeParser._fix_lazy_images(tree)
        assert count == 1
        result = lxml_html.tostring(tree, encoding="unicode")
        assert 'src="https://img.example.com/photo.jpg"' in result

    def test_fix_lazy_images_converts_data_original(self):
        html = '<img data-original="https://img.example.com/photo.jpg" src="placeholder.png">'
        tree = lxml_html.fromstring(html)
        count = IthomeParser._fix_lazy_images(tree)
        assert count == 1
        result = lxml_html.tostring(tree, encoding="unicode")
        assert 'src="https://img.example.com/photo.jpg"' in result

    def test_fix_lazy_images_skips_data_uri(self):
        html = '<img srcset="data:image/gif;base64,abc" src="placeholder.png">'
        tree = lxml_html.fromstring(html)
        count = IthomeParser._fix_lazy_images(tree)
        assert count == 0
```

- [ ] **Step 3: 运行测试**

```bash
pytest tests/parser_sites/test_ithome.py -v
```

---

### Task 3: 修复 Stub Parser — FastbullParser, KaopuParser, SspaiParser, JuejinParser

这 4 个 Parser 是空壳（9 行代码），完全依赖基类 `HtmlParser` 的 readability → fallback 降级链。需要：
1. 保存真实 HTML fixture
2. 写 fixture 测试验证 readability 降级链对每个站点是否有效
3. 如果 readability 在某些站点表现差，添加自定义 `_preprocess` 或 `_extract`

**Files:**
- Create: `tests/parser_sites/fixtures/fastbull.html`
- Create: `tests/parser_sites/fixtures/kaopu.html`
- Create: `tests/parser_sites/fixtures/sspai.html`
- Create: `tests/parser_sites/fixtures/juejin.html`
- Modify: `tests/parser_sites/test_fastbull.py`
- Modify: `tests/parser_sites/test_kaopu.py`
- Modify: `tests/parser_sites/test_sspai.py`
- Modify: `tests/parser_sites/test_juejin.py`
- Potentially Modify: `news/parser/sites/fastbull.py`, `kaopu.py`, `sspai.py`, `juejin.py`

- [ ] **Step 1: 保存所有真实 HTML fixture**

```bash
SOURCES=(
  "fastbull:https://www.fastbull.com/cn/news-detail/4380496_1"
  "kaopu:https://kaopu.news/story/2026-06-24/e38906"
  "sspai:https://sspai.com/post/111216"
  "juejin:https://juejin.cn/post/7654102171461402662"
)

for entry in "${SOURCES[@]}"; do
  name="${entry%%:*}"
  url="${entry#*:}"
  python -c "
import requests
resp = requests.get('$url', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}, timeout=30)
with open('tests/parser_sites/fixtures/${name}.html', 'w', encoding='utf-8') as f:
    f.write(resp.text)
print(f'Saved ${name}.html: {len(resp.text)} bytes')
"
done
```

- [ ] **Step 2: 为每个 Stub Parser 编写 fixture 测试**

```python
# 追加到 tests/parser_sites/test_fastbull.py
from pathlib import Path
FIXTURES = Path(__file__).parent / "fixtures"

class TestFastbullParserFixture:
    def test_extracts_content_from_real_fixture(self):
        html = (FIXTURES / "fastbull.html").read_text(encoding="utf-8")
        parser = FastbullParser()
        result = parser.parse(html, url="https://www.fastbull.com/cn/news-detail/4380496_1")
        assert result is not None
        assert len(result["markdown"]) > 200
        assert result["title"]

    def test_no_placeholder_images(self):
        html = (FIXTURES / "fastbull.html").read_text(encoding="utf-8")
        parser = FastbullParser()
        result = parser.parse(html, url="https://www.fastbull.com/")
        if result:
            # 不应包含 base64 占位符
            assert "data:image/gif;base64" not in result["markdown"]
```

对 kaopu、sspai、juejin 重复同样结构（替换类名和 URL）。

- [ ] **Step 3: 运行所有 Stub 测试，暴露问题**

```bash
pytest tests/parser_sites/test_fastbull.py tests/parser_sites/test_kaopu.py \
       tests/parser_sites/test_sspai.py tests/parser_sites/test_juejin.py -v
```

- [ ] **Step 4: 如果 fixture 测试失败 — 分析原因并修复**

对每个失败的 Stub，检查：
1. `grab-one` 输出是否为空？→ readability 无法提取 → 需要自定义 `_extract()`
2. 图片是否占位符？→ 需要添加 `_preprocess` 修复懒加载图片
3. 噪声是否过多？→ 需要添加 `_preprocess` 去除 DOM 噪声

修复模式（以 juejin 为例）：

```python
# news/parser/sites/juejin.py — 如果 readability 不够好
from __future__ import annotations

from lxml import html as lxml_html

from news.parser.parser import HtmlParser


class JuejinParser(HtmlParser):
    """稀土掘金解析器 — 需要处理 SPA 懒加载图片。"""

    _LAZY_IMAGE_ATTRS = ("data-src", "data-original")

    def _preprocess(self, html: str, url: str) -> str:
        """修复懒加载图片。"""
        try:
            tree = lxml_html.fromstring(html)
        except Exception:
            return html

        fixed = self._fix_lazy_images(tree)
        return (
            lxml_html.tostring(tree, encoding="unicode")
            if fixed > 0
            else html
        )
```

- [ ] **Step 5: 如果所有站点 readability 直接可用，Stub 维持不变**

不需要改动，只补 fixture 测试即可。

---

### Task 4: 修复 CankaoxiaoxiParser fixture 不真实的问题

**问题:** `tests/parser_sites/fixtures/cankaoxiaoxi.html` 只有 1393 字节，是手工构造的合成 HTML，不是从真实网站抓取的。测试依赖这个假 fixture，无法验证真实场景。

**Files:**
- Create: `tests/parser_sites/fixtures/cankaoxiaoxi.html`（覆盖原文件）
- Modify: `tests/parser_sites/test_cankaoxiaoxi.py`

- [ ] **Step 1: 保存真实 HTML fixture**

```bash
python -c "
import requests
resp = requests.get(
    'https://ckxxapp.ckxx.net/pages/2026/06/24/3c51c84696714b3c83ecb16ac8414d79.html',
    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
    timeout=30
)
with open('tests/parser_sites/fixtures/cankaoxiaoxi.html', 'w', encoding='utf-8') as f:
    f.write(resp.text)
print(f'Saved {len(resp.text)} bytes')
"
```

- [ ] **Step 2: 更新测试 — 修改断言匹配真实内容**

真实页面的标题/作者/日期会与合成 fixture 不同，需要根据实际内容更新断言。

```bash
# 先用 grab-one 看实际输出
python -m cli grab-one "https://ckxxapp.ckxx.net/pages/2026/06/24/3c51c84696714b3c83ecb16ac8414d79.html" -o markdown
```

根据实际输出更新 `test_cankaoxiaoxi.py` 中的断言值。

- [ ] **Step 3: 运行测试**

```bash
pytest tests/parser_sites/test_cankaoxiaoxi.py -v
```

---

### Task 5: 对接已有 Parser 的在线验证

对已有 fixture 和自定义逻辑的 Parser（wallstreetcn, cls, thepaper, zaobao, ifeng），用 grab-one 做最终在线验证确认无回归。

- [ ] **Step 1: 并发验证 5 个已验证 Parser**

```bash
for url_info in \
  "wallstreetcn:https://wallstreetcn.com/articles/3775351" \
  "cls:https://www.cls.cn/detail/2407859" \
  "thepaper:https://www.thepaper.cn/newsDetail_forward_33439602" \
  "zaobao:https://www.zaochenbao.com/news/politics/202606/2474898.html" \
  "ifeng:https://news.ifeng.com/c/8uDdI76KjZE"
do
  name="${url_info%%:*}"
  url="${url_info#*:}"
  echo "=== $name ==="
  python -m cli grab-one "$url" -o markdown 2>&1 | head -20
  echo ""
done
```

- [ ] **Step 2: 如有问题修复**

如果某个 Parser 的 grab-one 出现图片占位符、内容为空等问题，进行针对性修复。

---

### Task 6: 全量测试 + 覆盖率验证

- [ ] **Step 1: 运行全量 Parser 测试**

```bash
pytest tests/parser_sites/ -v --tb=short
```

- [ ] **Step 2: 检查覆盖率**

```bash
pytest tests/parser_sites/ --cov=news/parser --cov-report=term-missing
```

确保每个站点 Parser 的 `_extract()` 和 `_preprocess()` 方法都被测试覆盖。

- [ ] **Step 3: 运行全量回归测试**

```bash
pytest --cov=news --cov-report=term-missing
```

确保总体覆盖率 ≥ 80% 且无已存在测试被破坏。

---

## 提交序列

```
test: add ithome real HTML fixture and fixture-based tests
test: add fastbull/kaopu/sspai/juejin real HTML fixtures and tests
test: replace cankaoxiaoxi synthetic fixture with real HTML
fix: <如果发现并修复了问题，对应的 fix commit>
test: verify all 11 parsers pass grab-one online validation
```

---

## 全局约束

- Python ≥ 3.12
- 函数 <50 行，文件 <800 行
- 测试覆盖率 ≥ 80%
- 提交消息格式：`<type>: <描述>`（feat, fix, test, refactor, chore）
- 站点 Parser 只能覆写 `_preprocess()` 和 `_extract()`
- 站点 Parser 必须通过 `_build_result()` 返回结果
- 站点 Parser 不能发起 HTTP 请求、不能 import 其他站点 Parser
