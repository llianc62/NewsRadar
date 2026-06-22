# Sentiment Analyzer 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建可扩展的 Analyzer 抽象架构，将 heat_score 从 PostgreSQL 层迁移到分析层，并在同一框架中实现基于 jieba 词典的情感分析。

**Architecture:** `news/analyzer/` 包，`Analyzer` 抽象基类定义 `analyze_heat` 和 `analyze_sentiment` 两个方法。`JiebaAnalyzer` 继承并实现两者：heat 逻辑从 `PostgreSQL` 直接迁移，sentiment 用 jieba 分词 + 情感词典匹配。Crawler 在 `enrich_content()` 之后、`persist()` 之前调用 analyzer。

**Tech Stack:** Python 3.12+, jieba, psycopg2, pytest

## Global Constraints

- Python 3.12+（项目使用 `match/case` 和 PEP 604 unions）
- 不可变性：不修改对象，创建新值后原地赋值
- TDD：每个任务先写测试 -> 验证失败 -> 实现 -> 验证通过 -> 提交
- 函数 < 50 行，文件 < 800 行
- 使用 `uv sync && uv pip install pytest` 安装依赖

---

## File Structure

```
news/analyzer/
├── __init__.py       # create_analyzer(config, db) → Analyzer | None
├── analyzer.py       # Analyzer ABC (analyze_heat + analyze_sentiment)
├── jieba.py          # JiebaAnalyzer (heat + sentiment + decay SQL)
└── agent.py          # AgentAnalyzer 预留（raise NotImplementedError）

config/loader.py      # + _load_analyzer_config(), config["analyzer"] key
config.yaml           # + analyzer 顶层段
news/models.py        # + sentiment_score: int = 0
news/crawler.py       # + _get_analyzer(), _query_today_hotlist(), 分析阶段
storage/postgres.py   # - _process_hotlist_heat(), - _calc_heat_score()
                      #   _build_row() + sentiment_score, - save_news_data 中 heat 调用
data/                 # + senti_positive.txt, senti_negative.txt, senti_negation.txt, senti_degree.txt
tests/test_analyzer.py# 新增（heat 迁移测试 + sentiment 测试 + factory 测试）
tests/test_heat_score.py # 适配导入路径（PostgreSQL → JiebaAnalyzer）
```

---

### Task 1: 创建 news/analyzer 包骨架 + AgentAnalyzer 预留

**Files:**
- Create: `news/analyzer/__init__.py`
- Create: `news/analyzer/analyzer.py`
- Create: `news/analyzer/agent.py`

**Interfaces:**
- Produces: `Analyzer` ABC with `analyze_heat(source_id, items, db_map)` and `analyze_sentiment(items)`
- Produces: `create_analyzer(config, db)` factory — for now only supports `backend: jieba`, raises `NotImplementedError` for `agent`

- [ ] **Step 1: 创建 analyzer.py 抽象基类**

```python
# news/analyzer/analyzer.py
# coding=utf-8
"""Analyzer abstract base class."""

from abc import ABC, abstractmethod


class Analyzer(ABC):
    """分析器抽象基类。

    子类：
    - JiebaAnalyzer: 本地离线分析（heat_score + sentiment_score）
    - AgentAnalyzer: LLM 分析（未来，需 API key + config 开关）
    """

    def __init__(self, config: dict, db=None):
        self._config = config
        self._db = db

    @abstractmethod
    def analyze_heat(self, source_id: str, items: list, db_map: dict) -> None:
        """计算热度分。原地修改 item.heat_score 和 item.ranks。

        db_map 由调用方（Crawler）查询当天 DB 快照后传入，
        格式: {url: {"heat_score": int, "ranks": [[int,int],...]}}
        """
        ...

    @abstractmethod
    def analyze_sentiment(self, items: list) -> None:
        """计算情感分。原地修改 item.sentiment_score。

        items 为 dict 列表，每个 dict 需有 "title" 和 "content" 键。
        """
        ...
```

- [ ] **Step 2: 创建 agent.py 预留**

```python
# news/analyzer/agent.py
# coding=utf-8
"""AgentAnalyzer — LLM-based analysis (future)."""

from news.analyzer.analyzer import Analyzer


class AgentAnalyzer(Analyzer):
    """LLM 分析器（未来实现，需 API key + config 开关）。"""

    def __init__(self, config: dict, db=None):
        super().__init__(config, db)
        raise NotImplementedError("AgentAnalyzer 尚未实现")

    def analyze_heat(self, source_id: str, items: list, db_map: dict) -> None:
        raise NotImplementedError("AgentAnalyzer 尚未实现")

    def analyze_sentiment(self, items: list) -> None:
        raise NotImplementedError("AgentAnalyzer 尚未实现")
```

- [ ] **Step 3: 创建 __init__.py 工厂函数**

```python
# news/analyzer/__init__.py
# coding=utf-8
"""Analyzer factory."""

from typing import Optional

from news.analyzer.analyzer import Analyzer


__all__ = ["create_analyzer", "Analyzer"]


def create_analyzer(config: dict, db=None) -> Optional[Analyzer]:
    """根据配置创建分析器。

    config.yaml:
        analyzer:
          enabled: true
          backend: jieba       # jieba | agent（未来）
    """
    analyzer_cfg = config.get("analyzer", {})
    if not analyzer_cfg.get("enabled", True):
        return None

    backend = analyzer_cfg.get("backend", "jieba")
    if backend == "agent":
        from .agent import AgentAnalyzer
        return AgentAnalyzer(config, db)

    from .jieba import JiebaAnalyzer
    return JiebaAnalyzer(config, db)
```

- [ ] **Step 4: 提交**

```bash
git add news/analyzer/__init__.py news/analyzer/analyzer.py news/analyzer/agent.py
git commit -m "feat: add Analyzer ABC and factory skeleton"
```

---

### Task 2: 配置层 — config.yaml + loader.py

**Files:**
- Modify: `config.yaml`
- Modify: `config/loader.py`

**Interfaces:**
- Produces: `config["analyzer"]` = `{"enabled": True, "backend": "jieba"}`

- [ ] **Step 1: 修改 config.yaml，新增顶层 analyzer 段**

```yaml
# config.yaml — 在文件末尾（或合适位置）新增：
analyzer:
  enabled: true
  backend: jieba
```

- [ ] **Step 2: 修改 config/loader.py，新增 _load_analyzer_config**

在 `_load_web_config` 函数之后（约 line 177），新增：

```python
def _load_analyzer_config(raw: Dict) -> Dict:
    analyzer = raw.get("analyzer", {})
    return {
        "enabled": analyzer.get("enabled", True),
        "backend": analyzer.get("backend", "jieba"),
    }
```

- [ ] **Step 3: 在 load_config() 中注册 analyzer key**

在 `load_config()` 函数的 config dict 中（约 line 205-213），新增 `"analyzer"` 条目：

```python
config = {
    "app": _load_app_config(raw),
    "crawler": _load_crawler_config(raw),
    "notification": _load_notification_config(raw),
    "storage": _load_storage_config(raw),
    "postgresql": _load_postgresql_config(raw),
    "web": _load_web_config(raw),
    "analyzer": _load_analyzer_config(raw),  # 新增
}
```

- [ ] **Step 4: 验证配置加载**

```bash
python -c "from config.loader import load_config; c = load_config(); print(c.get('analyzer'))"
```

Expected: `{'enabled': True, 'backend': 'jieba'}`

- [ ] **Step 5: 提交**

```bash
git add config.yaml config/loader.py
git commit -m "feat: add analyzer config section"
```

---

### Task 3: NewsItem 模型 + _build_row 变更

**Files:**
- Modify: `news/models.py:26-28`
- Modify: `storage/postgres.py:454-487`

**Interfaces:**
- Produces: `NewsItem.sentiment_score: int = 0`
- Produces: `_build_row` 返回 21 元组（原来是 20），第 21 位是 `item.sentiment_score`

- [ ] **Step 1: NewsItem 新增 sentiment_score**

```python
# news/models.py — 在 heat_score 下面新增
@dataclass
class NewsItem:
    # ... 现有字段 ...
    heat_score: int = 0         # 热度值 0-100
    sentiment_score: int = 0    # 情感值 0-100（50=中性）  ← 新增
    published_at: str = ""
    crawled_at: str = ""
```

- [ ] **Step 2: _build_row 元组加 sentiment_score**

`_build_row` 当前返回 20 元组，需要插入 `item.sentiment_score`。查看 PostgreSQL INSERT SQL 中的列顺序来确定插入位置。检查 `storage/postgres.py` 中 `_INSERT_PREFIX` 常量：

```python
# storage/postgres.py — _build_row 返回值改动
# 原元组末尾: ... json.dumps(item.ranks) if item.ranks else '[]', item.heat_score,
# 改为末尾追加 sentiment_score:
return (
    item.title,
    source_id,
    item.source_name,
    item.source_type,
    tier,
    priority,
    item.url,
    item.mobile_url,
    item.rank,
    item.guid,
    ts_pub,
    item.summary,
    item.author,
    item.content,
    item.category if item.category else None,
    item.tags if item.tags else [],
    crawled_from,
    ts_crawled,
    json.dumps(item.ranks) if item.ranks else '[]',
    item.heat_score,
    item.sentiment_score,  # 新增（第 21 列）
)
```

- [ ] **Step 3: 确认 PostgreSQL INSERT SQL 列数匹配**

检查 `_INSERT_PREFIX` 和对应的 INSERT SQL 模板。如果 SQL 模板指定了列名（如现有的 `_HOTLIST_INSERT_SQL`），需要确认列名列表包含 `sentiment_score`，或者在 `VALUES` 中 `DEFAULT` 留为 NULL（如果模板用 `DEFAULT` 表示未指定列）。

```bash
grep -n "INSERT INTO news_articles" storage/postgres.py
```

- [ ] **Step 4: 运行现有测试确认无破坏**

```bash
pytest tests/test_postgres_write.py tests/test_postgres_batch.py -v
```

- [ ] **Step 5: 提交**

```bash
git add news/models.py storage/postgres.py
git commit -m "feat: add sentiment_score field to NewsItem and _build_row"
```

---

### Task 4: 创建情感词典文件

**Files:**
- Create: `data/senti_positive.txt`
- Create: `data/senti_negative.txt`
- Create: `data/senti_negation.txt`
- Create: `data/senti_degree.txt`

- [ ] **Step 1: 创建 data/senti_positive.txt**

```
# 金融新闻正面情感词典
# 格式：词  权重(1-5)
暴涨  4
涨停  4
利好  3
突破  3
反弹  2
大涨  3
新高  3
创新  2
稳健  2
增长  2
提升  2
回暖  2
复苏  2
分红  2
回购  2
盈利  2
扭亏  3
增持  2
中标  2
获批  3
签约  2
扩产  2
放量  2
领涨  3
跑赢  2
超预期 3
净利  2
预增  2
预喜  2
雄起  3
起飞  2
涨停板 4
开门红 2
翻倍  3
飙升  3
井喷  3
逆势  2
走强  2
企稳  2
景气  2
升温  2
提振  2
放宽  2
宽松  2
降准  3
降息  2
刺激  1
扶持  2
补贴  2
优惠  2
减免  2
减税  2
增值  2
升值  2
走俏  2
热销  2
爆款  2
供不应求 2
产能释放 2
业绩预增 3
政策利好 3
资金流入 2
机构看好 2
评级上调 3
目标价上调 3
```

- [ ] **Step 2: 创建 data/senti_negative.txt**

```
# 金融新闻负面情感词典
# 格式：词  权重(1-5)
暴跌  4
跌停  4
亏损  3
利空  3
下滑  2
大跌  3
新低  3
违约  3
爆雷  4
暴雷  4
退市  4
崩盘  4
踩踏  3
恐慌  2
抛售  2
减持  2
套现  2
跑路  3
倒闭  3
破产  3
裁员  2
关停  2
限产  2
停产  2
断供  2
缺货  2
涨价  1
贬值  2
跌跌不休 3
跳水  3
重挫  3
低迷  2
萎缩  2
恶化  2
收紧  3
加息  2
加税  2
罚款  2
处罚  2
违规  2
造假  3
欺诈  4
被查  3
连跌  2
阴跌  2
滞涨  2
衰退  3
泡沫  2
踩雷  4
失速  2
预警  2
预亏  3
业绩变脸 3
资金流出 2
大股东减持 3
评级下调 3
目标价下调 3
投诉  2
质量门 3
造假门 4
召回  2
赔偿  2
诉讼  2
停产整顿 3
```

- [ ] **Step 3: 创建 data/senti_negation.txt**

```
# 否定词词典
不
没
非
无
并非
绝不
毫无
并未
从未
不再
不可
不会
不算
谈不上
```

- [ ] **Step 4: 创建 data/senti_degree.txt**

```
# 程度副词词典
# 格式：词  系数
极其  2.5
非常  2.0
大幅  2.0
显著  2.0
明显  1.8
尤为  2.0
强烈  2.0
极度  2.5
特别  1.8
很    1.5
较    1.3
略微  0.5
稍微  0.5
有点  0.5
略    0.5
稍    0.5
轻度  0.5
微    0.5
不大  0.5
```

- [ ] **Step 5: 提交**

```bash
git add data/senti_positive.txt data/senti_negative.txt data/senti_negation.txt data/senti_degree.txt
git commit -m "feat: add sentiment lexicon files for jieba-based analysis"
```

---

### Task 5: JiebaAnalyzer — heat_score 迁移

**Files:**
- Create: `news/analyzer/jieba.py`（heat 部分）
- Modify: `storage/postgres.py`（移除 heat 逻辑）
- Modify: `tests/test_heat_score.py`（适配导入）

**Interfaces:**
- Produces: `JiebaAnalyzer.analyze_heat(source_id, items, db_map)` — 实现与 `PostgreSQL._process_hotlist_heat` 完全相同的逻辑
- Produces: `JiebaAnalyzer._calc_heat_score(prev_heat, prev_ranks, new_ranks_entry)` — 从 `PostgreSQL` 迁移的静态方法
- Removes: `PostgreSQL._process_hotlist_heat()`, `PostgreSQL._calc_heat_score()`

- [ ] **Step 1: 先修改 test_heat_score.py，改为从 JiebaAnalyzer 导入**

```python
# tests/test_heat_score.py — 修改导入
from news.analyzer.jieba import JiebaAnalyzer

# 将 PostgreSQL._calc_heat_score 替换为 JiebaAnalyzer._calc_heat_score
# 例如：
# score = JiebaAnalyzer._calc_heat_score(None, [], [1, 20])
```

所有测试方法中 `PostgreSQL._calc_heat_score(...)` → `JiebaAnalyzer._calc_heat_score(...)`。`TestProcessHotlistHeat` 类中的 `db._process_hotlist_heat(...)` 暂时保留（等 Task 7 再改）。

- [ ] **Step 2: 运行修改后的测试，确认失败**

```bash
pytest tests/test_heat_score.py::TestCalcHeatScore -v
```

Expected: `ImportError: cannot import name 'JiebaAnalyzer'` — 还未创建

- [ ] **Step 3: 创建 news/analyzer/jieba.py（heat 部分）**

```python
# news/analyzer/jieba.py
# coding=utf-8
"""JiebaAnalyzer — local offline analysis using jieba."""

import math
import os
from typing import Any, Dict, List, Optional

from news.analyzer.analyzer import Analyzer

# 词典文件默认路径
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")


def _load_dict(filepath: str) -> Dict[str, float]:
    """Load a word-weight dictionary file.
    
    Format: one entry per line — ``word  weight`` (space-separated).
    Lines starting with ``#`` are comments.
    """
    d = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.rsplit(None, 1)  # word  weight
                if len(parts) == 2:
                    try:
                        d[parts[0]] = float(parts[1])
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return d


class JiebaAnalyzer(Analyzer):
    """基于 jieba 的本地离线分析器。

    负责：
    - heat_score: 热度分计算（从 PostgreSQL 迁移）
    - sentiment_score: 基于词典的情感分析
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config, db)
        # 情感词典惰性加载
        self._positive_dict: Optional[Dict[str, float]] = None
        self._negative_dict: Optional[Dict[str, float]] = None
        self._negation_set: Optional[set] = None
        self._degree_dict: Optional[Dict[str, float]] = None

    # ── Heat score ─────────────────────────────────────────────────

    @staticmethod
    def _calc_heat_score(
        prev_heat: Optional[int],
        prev_ranks: list,       # [[7,20], [5,20]]
        new_ranks_entry: list,  # [rank, total] from current round
    ) -> int:
        """Calculate heat score, returns 0-100."""
        new_rank, new_total = new_ranks_entry
        if not prev_ranks or prev_heat is None:
            # First appearance: percentile
            return round(max(0, min(100, (1 - new_rank / new_total) * 100)))

        # Still on the list: incremental adjustment
        last_r, last_t = prev_ranks[-1]
        last_pct = (1 - last_r / last_t) * 100
        new_pct = (1 - new_rank / new_total) * 100
        delta = new_pct - last_pct  # percentage-point difference

        return round(max(0, min(100, prev_heat + delta * 0.3)))

    def analyze_heat(self, source_id: str, items: list, db_map: dict) -> None:
        """Process heat score for hotlist items of one source.

        db_map 格式: {url: {"heat_score": int, "ranks": [[int,int],...]}}
        """
        valid_items = [it for it in items if it.ranks]

        # ① Compare sets
        this_urls = {item.url for item in valid_items if item.url}
        db_urls = set(db_map.keys())

        new_urls = this_urls - db_urls
        existing_urls = this_urls & db_urls
        dropped_urls = db_urls - this_urls

        # ② First appearance — percentile
        for item in valid_items:
            if item.url in new_urls:
                r, t = item.ranks[0]
                item.heat_score = round(
                    max(0, min(100, (1 - r / t) * 100))
                )
                item.ranks = [[r, t]]

        # ③ Still on list — delta adjustment
        for item in valid_items:
            if item.url in existing_urls:
                prev = db_map[item.url]
                item.heat_score = self._calc_heat_score(
                    prev_heat=prev["heat_score"],
                    prev_ranks=prev["ranks"],
                    new_ranks_entry=item.ranks[0],
                )
                item.ranks = (prev["ranks"] or []) + [item.ranks[0]]

        # ④ Dropped from list — ×0.7 decay (requires DB write)
        if dropped_urls and self._db is not None:
            with self._db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE news_articles
                           SET heat_score = CAST(
                               ROUND(GREATEST(0, LEAST(100,
                                   COALESCE(heat_score, 0) * 0.7
                               ))) AS INTEGER
                           )
                           WHERE source_id = %s
                             AND source_type = 'hotlist'
                             AND url = ANY(%s)""",
                        (source_id, list(dropped_urls)),
                    )
            print(
                f"[Analyzer] Heat decay: {len(dropped_urls)} URLs dropped"
                f" from {source_id}"
            )

    # ── Sentiment (Task 6 实现) ────────────────────────────────────

    def analyze_sentiment(self, items: list) -> None:
        """TODO: Task 6 实现。"""
        raise NotImplementedError("analyze_sentiment will be implemented in Task 6")
```

- [ ] **Step 4: 运行 TestCalcHeatScore，确认全部通过**

```bash
pytest tests/test_heat_score.py::TestCalcHeatScore -v
```

Expected: 14 passed

- [ ] **Step 5: 提交**

```bash
git add news/analyzer/jieba.py tests/test_heat_score.py
git commit -m "feat: migrate heat_score logic from PostgreSQL to JiebaAnalyzer"
```

---

### Task 6: JiebaAnalyzer — sentiment_score 实现

**Files:**
- Modify: `news/analyzer/jieba.py`（添加 analyze_sentiment 实现 + 词典加载）

**Interfaces:**
- Consumes: `items: list[dict]`，每个 dict 需有 `"title"` 和 `"content"` 键（`"content"` 可为空）
- Produces: 原地设置 `item["sentiment_score"]` (0-100)

- [ ] **Step 1: 先写测试 tests/test_analyzer.py（sentiment 部分）**

```python
# tests/test_analyzer.py — sentiment 测试
# coding=utf-8
"""Tests for JiebaAnalyzer sentiment analysis."""

import pytest
from news.analyzer.jieba import JiebaAnalyzer


@pytest.fixture
def analyzer():
    """JiebaAnalyzer without DB."""
    cfg = {"analyzer": {"enabled": True, "backend": "jieba"}}
    return JiebaAnalyzer(cfg)


class TestAnalyzeSentiment:
    """Unit tests for analyze_sentiment."""

    def test_positive_text(self, analyzer):
        """利好文本 → score > 60."""
        items = [{"title": "业绩暴涨超预期", "content": "公司净利润大幅增长，股东分红创新高"}]
        analyzer.analyze_sentiment(items)
        assert items[0]["sentiment_score"] > 60

    def test_negative_text(self, analyzer):
        """利空文本 → score < 40."""
        items = [{"title": "股价暴跌", "content": "公司业绩下滑，亏损严重，面临退市风险"}]
        analyzer.analyze_sentiment(items)
        assert items[0]["sentiment_score"] < 40

    def test_neutral_text(self, analyzer):
        """无情感词 → score ≈ 50."""
        items = [{"title": "公司发布公告", "content": "公司今日发布公告，涉及日常经营事务"}]
        analyzer.analyze_sentiment(items)
        assert 40 <= items[0]["sentiment_score"] <= 60

    def test_negation_flips_polarity(self, analyzer):
        """"不会亏损" → 正面（不是负面）。"""
        items = [{"title": "不会亏损", "content": "公司表示今年不会出现亏损，预计盈利能力将改善"}]
        analyzer.analyze_sentiment(items)
        assert items[0]["sentiment_score"] > 50

    def test_degree_amplify(self, analyzer):
        """"极其利好" 得分 > "略微利好"。"""
        items_strong = [{"title": "极其利好", "content": ""}]
        items_weak = [{"title": "略微利好", "content": ""}]
        analyzer.analyze_sentiment(items_strong)
        analyzer.analyze_sentiment(items_weak)
        assert items_strong[0]["sentiment_score"] > items_weak[0]["sentiment_score"]

    def test_empty_content_title_only(self, analyzer):
        """空 content 仅 title → 不崩溃，返回有效值。"""
        items = [{"title": "利好政策出台", "content": ""}]
        analyzer.analyze_sentiment(items)
        assert 0 <= items[0]["sentiment_score"] <= 100

    def test_empty_all_text(self, analyzer):
        """全空 → score = 50（中性）。"""
        items = [{"title": "", "content": ""}]
        analyzer.analyze_sentiment(items)
        assert items[0]["sentiment_score"] == 50
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
pytest tests/test_analyzer.py::TestAnalyzeSentiment -v
```

Expected: 全部 FAIL（NotImplementedError）

- [ ] **Step 3: 实现 analyze_sentiment + 词典加载**

在 `JiebaAnalyzer` 类中替换 `analyze_sentiment` 方法，并添加私有方法：

```python
# news/analyzer/jieba.py — 替换 analyze_sentiment 并添加以下方法

    def _ensure_dicts(self) -> None:
        """惰性加载情感词典。"""
        if self._positive_dict is not None:
            return
        self._positive_dict = _load_dict(
            os.path.join(_DATA_DIR, "senti_positive.txt"))
        self._negative_dict = _load_dict(
            os.path.join(_DATA_DIR, "senti_negative.txt"))
        self._degree_dict = _load_dict(
            os.path.join(_DATA_DIR, "senti_degree.txt"))
        self._negation_set = set()
        neg_path = os.path.join(_DATA_DIR, "senti_negation.txt")
        try:
            with open(neg_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self._negation_set.add(line)
        except FileNotFoundError:
            pass

    def analyze_sentiment(self, items: list) -> None:
        """计算情感分。原地修改 item["sentiment_score"]。

        仅处理 dict 形式的 item（与 Crawler 中 item dict 一致）。
        """
        self._ensure_dicts()

        import jieba

        for item in items:
            title = item.get("title") or ""
            content = item.get("content") or ""
            text = title + " " + content

            if not text.strip():
                item["sentiment_score"] = 50
                continue

            # jieba 分词
            words = jieba.lcut(text)

            # 逐词评分
            pos_score, neg_score = self._score_words(words)

            # 映射到 0-100
            item["sentiment_score"] = self._to_sentiment_score(pos_score, neg_score)

    def _score_words(self, words: list) -> tuple:
        """遍历分词结果，返回 (pos_score, neg_score)。"""
        pos = 0.0
        neg = 0.0
        negation_active = 0  # 否定词作用窗口（剩余词数）
        degree_multiplier = 1.0

        for w in words:
            # 程度副词：修改当前乘数
            if w in self._degree_dict:
                degree_multiplier = self._degree_dict[w]
                continue

            # 否定词：翻转后续 3 词的极性
            if w in self._negation_set:
                negation_active = 3
                continue

            # 正面词
            if w in self._positive_dict:
                weight = self._positive_dict[w] * degree_multiplier
                if negation_active > 0:
                    neg += weight  # 否定 → 归入负面
                    negation_active -= 1
                else:
                    pos += weight

            # 负面词
            elif w in self._negative_dict:
                weight = self._negative_dict[w] * degree_multiplier
                if negation_active > 0:
                    pos += weight  # 否定 → 归入正面
                    negation_active -= 1
                else:
                    neg += weight

            # 窗口递减（非情感词也消耗窗口）
            elif negation_active > 0:
                negation_active -= 1

            # 重置乘数（每个词只用一次）
            degree_multiplier = 1.0

        return pos, neg

    @staticmethod
    def _to_sentiment_score(pos: float, neg: float) -> int:
        """将正负得分映射到 0-100。"""
        if pos + neg == 0:
            return 50  # 中性
        net = pos - neg
        scaled = math.tanh(net / 5.0) * 50.0  # -50 ~ +50
        return round(50 + scaled)  # 0-100
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
pytest tests/test_analyzer.py::TestAnalyzeSentiment -v
```

Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add news/analyzer/jieba.py tests/test_analyzer.py
git commit -m "feat: implement sentiment analysis with jieba lexicon matching"
```

---

### Task 7: Crawler 集成 analyzer + PostgreSQL 瘦身

**Files:**
- Modify: `news/crawler.py` — `__init__`, `fetch_all`, 新增 `_get_analyzer`, `_query_today_hotlist`
- Modify: `storage/postgres.py` — `save_news_data` 移除 heat 调用
- Modify: `tests/test_heat_score.py` — `TestProcessHotlistHeat` 适配

**Interfaces:**
- Consumes: `JiebaAnalyzer` from `news/analyzer`
- Produces: `Crawler._get_analyzer()` lazy-init
- Produces: `Crawler._query_today_hotlist(source_id) → db_map`
- Removes: `PostgreSQL._process_hotlist_heat()`

- [ ] **Step 1: 修改 Crawler.__init__**

```python
# news/crawler.py — 在 __init__ 中添加
# (约 line 107，在 self._image_processor 之后)

        # Analyzer (lazy) — 共享分析器实例
        self._analyzer: Any = None
```

- [ ] **Step 2: 添加 _get_analyzer 和 _query_today_hotlist**

在 `news/crawler.py` 中，`_get_image_processor` 方法之后添加：

```python
    def _get_analyzer(self):
        if self._analyzer is None:
            from news.analyzer import create_analyzer
            self._analyzer = create_analyzer(self._config, db=self._get_pg_db())
        return self._analyzer

    def _query_today_hotlist(self, source_id: str) -> dict:
        """查询当天该 source 的 DB 快照，供 analyze_heat 使用。
        
        Returns:
            {url: {"heat_score": int, "ranks": list}}
        """
        import psycopg2.extras

        pg = self._get_pg_db()
        db_map: dict = {}
        with pg.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT url, heat_score, ranks
                       FROM news_articles
                       WHERE source_id = %s
                         AND source_type = 'hotlist'
                         AND crawled_at::date = CURRENT_DATE""",
                    (source_id,),
                )
                for row in cur.fetchall():
                    db_map[row["url"]] = {
                        "heat_score": row["heat_score"],
                        "ranks": row["ranks"] if row["ranks"] else [],
                    }
        return db_map
```

- [ ] **Step 3: 修改 fetch_all()，在 enrich 之后 persist 之前插入分析阶段**

```python
# news/crawler.py — fetch_all() 中
        # ── Enrichment ─────────────────────────────────────────────
        if with_content:
            self.enrich_content(*all_items, with_image=with_image)

        # ── Analysis ───────────────────────────────────────────────  ← 新增
        analyzer = self._get_analyzer()
        if analyzer is not None:
            # Sentiment: analyze items that have content body
            contentful = [it for it in all_items if it.get("content")]
            if contentful:
                analyzer.analyze_sentiment(contentful)

            # Heat: group hotlist items by source, query DB snapshots,
            # then process
            hotlist_items = [it for it in all_items
                           if isinstance(it, dict) and it.get("source_type") == "hotlist"]
            if hotlist_items:
                # Group by source_id
                by_source: Dict[str, list] = {}
                for it in hotlist_items:
                    sid = it.get("source_id", "")
                    by_source.setdefault(sid, []).append(it)

                for sid, items in by_source.items():
                    db_map = self._query_today_hotlist(sid)
                    analyzer.analyze_heat(sid, items, db_map)
        # ── Persistence ────────────────────────────────────────────
        self.persist(*all_items, output_style=output_style)
```

- [ ] **Step 4: 修改 _to_newsdata()，传递 sentiment_score**

```python
# news/crawler.py — _to_newsdata() 中 NewsItem 构造
        # 添加 sentiment_score 参数：
        sentiment_score=d.get("sentiment_score", 0),
```

位置在 `heat_score=d.get("heat_score", 0),` 之后。

- [ ] **Step 5: 从 PostgreSQL.save_news_data() 移除 _process_hotlist_heat**

```python
# storage/postgres.py — save_news_data() 中删除以下代码块（约 line 366-374）：
        # 删除：
        # for source_id, news_list in news_data.items.items():
        #     hotlist_items = [...]
        #     if hotlist_items:
        #         self._process_hotlist_heat(source_id, hotlist_items)
```

- [ ] **Step 6: 删除 PostgreSQL._process_hotlist_heat 方法**

```python
# storage/postgres.py — 删除 _process_hotlist_heat 和 _calc_heat_score 方法
# 删除约 line 491-598（整个 "Heat score" 区块）
```

- [ ] **Step 7: 运行全部相关测试**

```bash
pytest tests/test_heat_score.py tests/test_analyzer.py tests/test_postgres_write.py -v
```

预期全部通过。

- [ ] **Step 8: 提交**

```bash
git add news/crawler.py storage/postgres.py
git commit -m "feat: integrate analyzer into Crawler pipeline, remove heat from PostgreSQL"
```

---

### Task 8: 测试完善 + 回归验证

**Files:**
- Modify: `tests/test_analyzer.py`（补全 heat 迁移测试 + factory 测试）
- Modify: `tests/test_heat_score.py`（清除已废弃的 TestProcessHotlistHeat 或适配）

**Interfaces:**
- Verifies: `create_analyzer` factory 行为
- Verifies: `analyze_heat` 行为与原 `_process_hotlist_heat` 一致
- Verifies: 回归测试全部通过

- [ ] **Step 1: tests/test_analyzer.py 补充工厂测试**

在 `tests/test_analyzer.py` 末尾添加：

```python
class TestCreateAnalyzer:
    """Tests for create_analyzer factory."""

    def test_creates_jieba_analyzer(self):
        """backend=jieba → JiebaAnalyzer."""
        from news.analyzer import create_analyzer
        from news.analyzer.jieba import JiebaAnalyzer

        cfg = {"analyzer": {"enabled": True, "backend": "jieba"}}
        a = create_analyzer(cfg)
        assert isinstance(a, JiebaAnalyzer)

    def test_disabled_returns_none(self):
        """enabled=false → None."""
        from news.analyzer import create_analyzer

        cfg = {"analyzer": {"enabled": False, "backend": "jieba"}}
        a = create_analyzer(cfg)
        assert a is None

    def test_missing_config_returns_jieba_by_default(self):
        """缺失 analyzer config → 默认返回 JiebaAnalyzer（enabled=True）。"""
        from news.analyzer import create_analyzer
        from news.analyzer.jieba import JiebaAnalyzer

        cfg = {}
        a = create_analyzer(cfg)
        assert isinstance(a, JiebaAnalyzer)
```

- [ ] **Step 2: 运行全部测试**

```bash
pytest tests/test_analyzer.py tests/test_heat_score.py -v
```

- [ ] **Step 3: 运行完整测试套件做回归验证**

```bash
pytest --cov=news --cov=storage --cov-report=term-missing
```

- [ ] **Step 4: 提交**

```bash
git add tests/test_analyzer.py tests/test_heat_score.py
git commit -m "test: add analyzer factory tests and complete regression verification"
```

---

### Task 9: 端到端验证

- [ ] **Step 1: 确认配置加载正确**

```bash
python -c "
from config.loader import load_config
c = load_config()
print('analyzer:', c.get('analyzer'))
"
```

- [ ] **Step 2: 确认 import 链完整**

```bash
python -c "
from news.analyzer import create_analyzer, Analyzer
from news.analyzer.jieba import JiebaAnalyzer
print('All imports OK')
print('ABC:', Analyzer)
print('Jieba:', JiebaAnalyzer)
"
```

- [ ] **Step 3: 手动验证 sentiment 计算**

```bash
python -c "
from news.analyzer.jieba import JiebaAnalyzer
a = JiebaAnalyzer({'analyzer': {'enabled': True, 'backend': 'jieba'}})

items = [
    {'title': '公司业绩暴涨 股价涨停', 'content': '今日该公司发布年报，净利润增长翻倍，股东分红创新高'},
    {'title': '公司业绩下滑 面临亏损', 'content': '公司持续亏损，面临退市风险，大股东减持套现'},
    {'title': '行业日常新闻', 'content': '今日召开了行业例行会议，讨论了当前市场情况'},
]
a.analyze_sentiment(items)
for it in items:
    print(f'score={it[\"sentiment_score\"]:3d}  {it[\"title\"]}')
"
```

Expected: 正面 >60, 负面 <40, 中性 40-60

- [ ] **Step 4: 提交**

```bash
git add -A
git diff --cached --stat
git commit -m "chore: final integration verification"
```
