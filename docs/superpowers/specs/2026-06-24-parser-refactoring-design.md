# Parser 拆分重构设计

## 动机

当前 `news/parser.py` (~800+ 行) 已成为整个系统最臃肿的模块。HTML 解析的三层流水线
（SPA → readability → fallback）中混杂了大量站点特定的处理逻辑：

- `_handle_ifeng()` — 凤凰网 DOM 预处理
- `_extract_spa_data()` — 攒了 Next.js / SSR / JSON-LD / JS 变量提取等多种模式
- `_fix_lazy_images()` — 针对 thepaper.cn / ithome 的 `data-src` 处理
- `_extract_js_content_vars()` — ckxxapp / xinhuamm 的 JS 变量提取
- `_trim_noise()` — 通用的噪声裁剪，但站点特定的 DOM 结构处理也写在里面

这些逻辑互相之间没有公共性，却都挤在一个类里。每次新增站点或修复一个站点的解析 bug，
都要改动这个巨大的文件，测试回滚风险高，逻辑边界模糊。

## 目标

将单体的 `HtmlParser` 拆分为**基类 + 11 个站点 Parser**，每个站点 Parser 只负责自己
站点的 HTML 解析逻辑。先确保每个站点 Parser 独立完整工作，最后再提取公共模式减少冗余。

## 设计决策

### 模式选择：继承（模板方法）

**选择理由：** 这组对象有统一的接口（`parse(html, url) → Dict | None`）和共享的
降级链（readability → fallback），差异只在于各自的提取逻辑。继承能让子类只覆写
`_extract()` 或 `_preprocess()` 而不必关心通用流水线。

**约束：**

- 子类只能覆写 `_preprocess()` 和 `_extract()`，不能覆写 `parse()`
- 必须通过 `_build_result()` 返回结果
- 不能 import 其他站点 Parser
- 不能发起 HTTP 请求

### 路由机制：source_id → Parser

`Crawler._download_and_parse(item)` 中已经有 `item["source_id"]`，不需要 URL 域名
匹配。`ParserRegistry` 维护 `{source_id: Parser实例}` 映射，未注册的 source_id
自动走 `HtmlParser()` 默认实例兜底。

## 目录结构

```
news/parser/
├── __init__.py              # 导出 parser_registry
├── parser.py                # HtmlParser 基类
├── registry.py              # ParserRegistry（source_id → Parser 路由）
└── sites/
    ├── __init__.py          # 自动注册逻辑
    ├── thepaper.py          # 澎湃新闻 — __NEXT_DATA__ JSON
    ├── ifeng.py             # 凤凰网 — DOM 预处理
    ├── cankaoxiaoxi.py      # 参考新闻 — JS 变量提取
    ├── wallstreetcn.py      # 华尔街见闻 — __SSR__ JSON
    ├── cls.py               # 财联社
    ├── zaobao.py            # 联合早报
    ├── kaopu.py             # 靠谱新闻
    ├── fastbull.py          # 法布财经
    ├── ithome.py            # IT之家
    ├── sspai.py             # 少数派
    └── juejin.py            # 稀土掘金
```

> `wallstreetcn-hot` 和 `wallstreetcn-news` 共用一个 `WallstreetcnParser`，
> 注册时两个 source_id 指向同一实例。`cls-hot` 和 `cls-depth` 同理。

## HtmlParser 基类

`news/parser/parser.py`

只保留明确通用的方法——那些"一看就知道没有站点偏见"的方法：

```
HtmlParser
├── parse(html, url)                 # 模板方法骨架
│   ├── html = self._preprocess(html, url)
│   ├── result = self._extract(html, url)
│   ├── result is None → _extract_with_readability(html, url)
│   ├── result is None → _fallback(html, url)
│   └── 内容截断 (max_content_length)
│
├── _preprocess(html, url)           # Hook — 默认返回原 html
├── _extract(html, url)              # Hook — 默认返回 None
│
├── _extract_with_readability()      # 通用: readability-lxml + markdownify
├── _fallback()                      # 通用: HTML 标签剥离 + 元数据提取
│
├── _build_result(...)               # 通用: 纯数据构造
├── _extract_title_from_html()       # 通用: og:title → <title>
├── _extract_meta()                  # 通用: <meta> content 属性提取
├── _extract_markdown_heading()      # 通用: Markdown 首行 H1
├── _beautify_markdown_formatting()  # 通用: 粗体 ** 标记规范化
└── _handle_markdown_bold()          # 通用: ** 分隔符对齐
```

### 模板方法

```python
class HtmlParser:
    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self.max_content_length = (
            self._config.get("crawler", {}).get("max_content_length", 100000)
        )

    def parse(self, html: str, url: str = "") -> dict | None:
        if not html or not html.strip():
            return None

        html = self._preprocess(html, url)

        result = self._extract(html, url)
        if result is None:
            result = self._extract_with_readability(html, url)
        if result is None:
            result = self._fallback(html, url)

        if result:
            md = result.get("markdown", "")
            if md and len(md) > self.max_content_length:
                result["markdown"] = md[:self.max_content_length] + "\n\n..."

        return result

    def _preprocess(self, html: str, url: str) -> str:
        return html

    def _extract(self, html: str, url: str) -> dict | None:
        return None
```

## ParserRegistry

`news/parser/registry.py`

```python
class ParserRegistry:
    def __init__(self):
        self._parsers: dict[str, HtmlParser] = {}
        self._default = HtmlParser()

    def register(self, source_id: str, parser: HtmlParser) -> None:
        self._parsers[source_id] = parser

    def parse(self, source_id: str, html: str, url: str = "") -> dict | None:
        parser = self._parsers.get(source_id, self._default)
        return parser.parse(html, url)
```

## 自动注册

`sites/__init__.py` 维护 source_id 到 Parser 类名的映射，`_build_registry()` 在模块
加载时通过 `importlib` 自动实例化和注册。

新增站点只需两步：
1. 创建 `sites/<name>.py`
2. 在 `_SITE_PARSER_MAP` 加一行

## 各站点 Parser 清单

| source_id(s) | 类名 | 文件 | 已知处理逻辑 |
|---|---|---|---|
| `thepaper` | `ThepaperParser` | `thepaper.py` | `__NEXT_DATA__` JSON → readability |
| `ifeng` | `IfengParser` | `ifeng.py` | `_preprocess()` 删除 DOM 噪声 → readability |
| `cankaoxiaoxi` | `CkxxappParser` | `cankaoxiaoxi.py` | `var contentTxt = "..."` JS 变量 → readability |
| `wallstreetcn-hot`, `wallstreetcn-news` | `WallstreetcnParser` | `wallstreetcn.py` | `__SSR__` JSON |
| `cls-hot`, `cls-depth` | `ClsParser` | `cls.py` | 待验证 |
| `zaobao` | `ZaobaoParser` | `zaobao.py` | 待验证 |
| `kaopu` | `KaopuParser` | `kaopu.py` | 待验证 |
| `fastbull-news` | `FastbullParser` | `fastbull.py` | 待验证 |
| `ithome` | `IthomeParser` | `ithome.py` | 待验证 |
| `sspai` | `SspaiParser` | `sspai.py` | 待验证 |
| `juejin` | `JuejinParser` | `juejin.py` | 待验证 |

"待验证"的站点先用真实 URL 抓取页面，分析 HTML 结构后决定是否需要覆写 `_extract()`。
如果 readability 能直接覆盖（即基类默认行为），站点 Parser 可以就是空子类，但**文件
必须存在**——每个 source_id 都有自己独立的 Parser 文件。

## Crawler 适配

改动量最小，仅涉及两处：

```python
# __init__
from news.parser import parser_registry
self.parser_registry = parser_registry

# _download_and_parse
parsed = self.parser_registry.parse(item["source_id"], resp.text, url)
```

旧 `self.parser` 变量和 `HtmlParser(config)` 直接实例化一并删除。

## 测试策略

### 离线单元测试

每站点两个 fixture + 一个断言类：

```
tests/parser_sites/
├── test_thepaper.py
├── test_ifeng.py
├── test_cankaoxiaoxi.py
├── test_wallstreetcn.py
├── test_cls.py
├── test_zaobao.py
├── test_kaopu.py
├── test_fastbull.py
├── test_ithome.py
├── test_sspai.py
├── test_juejin.py
└── fixtures/
    ├── thepaper.html
    ├── ifeng.html
    ├── cankaoxiaoxi.html
    └── ...
```

每个测试至少覆盖：
- 正文 Markdown 不为空、长度合理
- 标题正确提取
- 无导航/版权/页脚噪声混入
- 返回值类型符合 `Dict[str, Any]`

### 在线集成验证

每个站点 Parser 实现后，必须通过 `grab-one` 抓取真实页面验证 markdown 质量：

```bash
python -m cli grab-one "<真实文章URL>" --output-style markdown
```

人工确认输出满足"干净标准的 markdown 文字"。

## 迁移策略

1. **框架搭建** — 创建 `news/parser/` 目录结构、基类 `parser.py`、`registry.py`、
   `sites/__init__.py`、Crawler 适配
2. **三个已知站点** — thepaper、ifeng、cankaoxiaoxi（逻辑已明确，从旧 parser 提取）
3. **其余 8 个站点** — 逐个用 `grab-one` 抓取真实页面，分析 HTML 结构，编写 `_extract()`
4. **清理** — 删除旧 `news/parser.py`，更新所有 import 路径

## 禁止放入基类的内容

以下方法当前在 `HtmlParser` 中，但不放入新基类——各站点 Parser 自己实现：

- `_find_json_candidates()` — SPA JSON 发现，各站点使用的 JSON 模式不同
- `_find_article_in_json()` — JSON 树遍历，"选最长 content"等启发式是站点特定的
- `_extract_bracketed_json()` — SPA 辅助工具
- `_extract_js_content_vars()` — ckxxapp 特有的 JS 变量提取
- `_extract_spa_data()` — 整个 SPA 流水线按站点拆分
- `_fix_lazy_images()` — 各站懒加载属性名不同（`data-src` vs `data-original`）
- `_trim_noise()` — DOM 噪声裁剪逻辑与站点页面结构强耦合
- `_handle_ifeng()` — 凤凰网专用
- `_build_image_markdown()` — 图片降级策略可能因站而异

## 后续优化

所有站点 Parser 完成并稳定后，做一轮"提取公共模式"的重构：

1. 扫描各站点 Parser，识别出现 >= 3 次的相同模式
2. 将确认通用的方法上提到 `HtmlParser` 或提取到 `news/parser/utils.py`
3. 不再"为了通用而通用"——只在有充分证据时才抽象
