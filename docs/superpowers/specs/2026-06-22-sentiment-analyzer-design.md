# Sentiment Analyzer 设计文档

## 概述

为 NewsRadar 新增情感分析能力。将现有 heat_score 计算逻辑从 `PostgreSQL` 迁移到可扩展的 `Analyzer` 抽象架构，并在同一框架中实现基于 jieba 词典的情感分析（sentiment_score）。

### 目标

1. 抽象 `Analyzer` 基类，支持未来切换后端（jieba → LLM agent）
2. 将 heat_score 从存储层迁移到分析层
3. 实现 jieba 词典情感分析

### 非目标

- 置信度（confidence）计算 —— 暂不实现，后续处理
- AgentAnalyzer（LLM）实现 —— 仅预留接口

---

## 架构

### 目录结构

```
news/
├── analyzer/
│   ├── __init__.py       # create_analyzer(config, db) 工厂函数
│   ├── analyzer.py       # Analyzer 抽象基类
│   ├── jieba.py          # JiebaAnalyzer — 本地分析（heat + sentiment）
│   └── agent.py          # AgentAnalyzer — LLM 分析（未来，预留）
├── crawler.py
├── models.py
└── ...
```

### Analyzer 抽象基类

```python
# news/analyzer/analyzer.py
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
        格式: {url: {"heat_score": int, "ranks": list}}
        """
        ...

    @abstractmethod
    def analyze_sentiment(self, items: list) -> None:
        """计算情感分。原地修改 item.sentiment_score。"""
        ...
```

### 工厂函数

```python
# news/analyzer/__init__.py
def create_analyzer(config: dict, db=None) -> Analyzer:
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
        raise NotImplementedError("AgentAnalyzer 尚未实现")

    from .jieba import JiebaAnalyzer
    return JiebaAnalyzer(config, db)
```

---

## 配置

### config.yaml

```yaml
# 顶层配置
analyzer:
  enabled: true
  backend: jieba
```

### config/loader.py

```python
def _load_analyzer_config(raw: Dict) -> Dict:
    analyzer = raw.get("analyzer", {})
    return {
        "enabled": analyzer.get("enabled", True),
        "backend": analyzer.get("backend", "jieba"),
    }
```

---

## 数据模型

### NewsItem 变更

```python
# news/models.py
@dataclass
class NewsItem:
    # ... 现有字段 ...
    heat_score: int = 0         # 现有，热度值 0-100
    sentiment_score: int = 0    # 新增，情感值 0-100（50=中性）
```

### 数据库

Schema 已有对应列，无需 migration：

```sql
-- storage/postgres.sql（已有）
sentiment_score INTEGER DEFAULT NULL CHECK (sentiment_score BETWEEN 0 AND 100),
confidence      INTEGER DEFAULT NULL CHECK (confidence BETWEEN 0 AND 100),
```

### _build_row 变更

```python
# storage/postgres.py
@staticmethod
def _build_row(...) -> Tuple:
    return (
        # ... 现有字段 ...
        item.heat_score,       # 已有
        item.sentiment_score,  # 新增
    )
```

---

## JiebaAnalyzer 设计

### 词典文件

```
data/
├── senti_positive.txt    # 正面词 + 权重
├── senti_negative.txt    # 负面词 + 权重
├── senti_negation.txt    # 否定词
├── senti_degree.txt      # 程度副词 + 系数
└── jieba_idf.txt         # 已有
```

**词典格式示例：**

```
# senti_positive.txt — 词  权重(1-5)
暴涨  4
突破  3
利好  3
涨停  4
稳健  2
增长  2
反弹  2
分红  2
```

```
# senti_negation.txt
不  没  非  无  并非  绝不  毫无
```

```
# senti_degree.txt — 词  系数
非常  2.0
极其  2.5
大幅  2.0
略微  0.5
稍微  0.5
有点  0.5
```

### heat_score 计算

从 `PostgreSQL._process_hotlist_heat()` 迁移，逻辑不变：

```
new:      (1 − rank/total) × 100
existing: prev_heat + (new_pct − old_pct) × 0.3
dropped:  prev_heat × 0.7
```

`_calc_heat_score()` 静态方法从 `PostgreSQL` 移到 `JiebaAnalyzer`。

### sentiment_score 计算

**流程：**

```
item["title"] + item["content"]
    → clean_markdown()          ← 复用现有函数，去掉 Markdown 语法噪音
    → jieba.lcut()              ← 分词
    → 逐词查情感词典             ← 累加正/负面得分
    → 否定词翻转 + 程度副词调整
    → 映射到 0-100
```

**算法伪代码：**

```python
def analyze_sentiment(self, items: list) -> None:
    for item in items:
        # 1. 合并文本（title 权重 ×1.5，正文 ×1.0）
        text = (item.get("title") or "") + " " + (item.get("content") or "")
        text = clean_markdown(text)

        # 2. jieba 分词
        words = jieba.lcut(text)

        # 3. 逐词评分 + 否定/程度处理
        pos_score, neg_score = self._score_words(words)

        # 4. 映射到 0-100
        # 中性点 50，正负各偏 50
        item["sentiment_score"] = self._to_sentiment_score(pos_score, neg_score)

def _score_words(self, words: list) -> tuple:
    """遍历分词结果，返回 (pos_score, neg_score)"""
    pos = 0.0
    neg = 0.0
    negation_active = 0  # 否定词作用窗口
    degree_multiplier = 1.0

    for w in words:
        # 程度副词：修改当前乘数
        if w in self._degree_dict:
            degree_multiplier = self._degree_dict[w]
            continue

        # 否定词：翻转后续 2-3 词的极性
        if w in self._negation_dict:
            negation_active = 3  # 作用窗口 = 3 个词
            continue

        # 正面词
        if w in self._positive_dict:
            weight = self._positive_dict[w] * degree_multiplier
            if negation_active:
                neg += weight  # 否定 → 归入负面
                negation_active -= 1
            else:
                pos += weight

        # 负面词
        elif w in self._negative_dict:
            weight = self._negative_dict[w] * degree_multiplier
            if negation_active:
                pos += weight  # 否定 → 归入正面
                negation_active -= 1
            else:
                neg += weight

        # 窗口递减
        elif negation_active > 0:
            negation_active -= 1

        # 重置乘数
        degree_multiplier = 1.0

    return pos, neg

def _to_sentiment_score(self, pos: float, neg: float) -> int:
    """将正负得分映射到 0-100。"""
    if pos + neg == 0:
        return 50  # 中性
    # tanh 压缩，避免极端值
    net = pos - neg
    scaled = math.tanh(net / 5) * 50  # -50 ~ +50
    return round(50 + scaled)  # 0-100
```

---

## Crawler 集成

### fetch_all() 新流程

```
fetch_all()
 ├─ Hot-list fetch
 ├─ RSS fetch
 ├─ enrich_content()          # 现有：下载 HTML → parse → tags
 │   ├─ _run_batch_parse()
 │   └─ _run_batch_image_download()
 ├─ [分析阶段]                 # 新增
 │   ├─ analyzer.analyze_sentiment(contentful_items)
 │   └─ for each source:
 │        db_map ← _query_today_hotlist(source_id)
 │        analyzer.analyze_heat(source_id, hotlist_items, db_map)
 └─ persist()                 # 现有：纯存储
```

### 关键代码

```python
# news/crawler.py

def _get_analyzer(self):
    if self._analyzer is None:
        self._analyzer = create_analyzer(self._config)
    return self._analyzer

def _query_today_hotlist(self, source_id: str) -> dict:
    """查询当天该 source 的 DB 快照，供 analyze_heat 使用。"""
    pg = self._get_pg_db()
    db_map = {}
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

---

### PostgreSQL 瘦身

`save_news_data()` 移除 `_process_hotlist_heat()` 调用，只做纯 SQL 操作。

`_calc_heat_score()` 静态方法从 `PostgreSQL` 移到 `JiebaAnalyzer`。

索引 `idx_heat_score` 保留（前端排序仍需要）。

---

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `news/analyzer/__init__.py` | 新增 | `create_analyzer()` 工厂 |
| `news/analyzer/analyzer.py` | 新增 | `Analyzer` 抽象基类 |
| `news/analyzer/jieba.py` | 新增 | `JiebaAnalyzer`（heat + sentiment） |
| `news/analyzer/agent.py` | 新增 | `AgentAnalyzer`（预留，raise NotImplementedError） |
| `news/models.py` | 修改 | `NewsItem` 加 `sentiment_score: int = 0` |
| `news/crawler.py` | 修改 | 集成 analyzer 调用，新增 `_query_today_hotlist()` |
| `storage/postgres.py` | 修改 | 移除 `_process_hotlist_heat` + `_calc_heat_score`；`_build_row` 加 sentiment_score |
| `config/loader.py` | 修改 | 新增 `_load_analyzer_config()`；顶层 config 加 `analyzer` key |
| `config.yaml` | 修改 | 新增顶层 `analyzer` 段 |
| `data/senti_positive.txt` | 新增 | 正面情感词典 |
| `data/senti_negative.txt` | 新增 | 负面情感词典 |
| `data/senti_negation.txt` | 新增 | 否定词词典 |
| `data/senti_degree.txt` | 新增 | 程度副词词典 |
| `tests/test_analyzer.py` | 新增 | analyzer 单元测试（heat + sentiment） |
| `tests/test_heat_score.py` | 修改 | 适配新调用方式 |

---

## 测试策略

### 单元测试（`tests/test_analyzer.py`）

**heat_score（迁移验证）：**
| 测试 | 内容 |
|------|------|
| `test_heat_new_item` | 首次上榜 → 百分位计算 |
| `test_heat_existing_item_up` | 排名上升 → delta 正向调整 |
| `test_heat_existing_item_down` | 排名下降 → delta 负向调整 |
| `test_heat_clamp` | 边界 0-100 |
| `test_heat_delta_zero_at_n` | rank 不变 → heat 不变 |

**sentiment_score：**
| 测试 | 内容 |
|------|------|
| `test_senti_positive` | 利好文本 → score > 60 |
| `test_senti_negative` | 利空文本 → score < 40 |
| `test_senti_neutral` | 无情感词 → score ≈ 50 |
| `test_senti_negation` | "不会亏损" → 正面 |
| `test_senti_double_negation` | "并非无效" → 正面 |
| `test_senti_degree_amplify` | "极其利好" > "略微利好" |
| `test_senti_title_weight` | title 正面词贡献大于 body 中 |
| `test_senti_empty_content` | 空 content 仅 title → 不崩溃 |

**工厂方法：**
| 测试 | 内容 |
|------|------|
| `test_create_analyzer_jieba` | backend=jieba → JiebaAnalyzer |
| `test_create_analyzer_disabled` | enabled=false → 返回 None |

### 回归测试

- 现有 `test_heat_score.py` 全部通过
- 现有 `test_postgres_*.py` 全部通过
- `test_postgres_write.py` 验证 save 不再有 heat 副作用

---

## 迁移计划

### 阶段 1：纯重构（heat_score 迁移，行为不变）

1. 创建 `news/analyzer/` 包（`__init__.py` + `analyzer.py` + `jieba.py` + `agent.py`）
2. `JiebaAnalyzer` 实现 `analyze_heat()`，直接复用 `_process_hotlist_heat` 逻辑
3. `Crawler` 新增 `_query_today_hotlist()` 和 `_get_analyzer()`，在 `persist()` 前调用 `analyze_heat`
4. `PostgreSQL.save_news_data()` 移除 `_process_hotlist_heat()` 调用
5. `_calc_heat_score` 从 `PostgreSQL` 移到 `JiebaAnalyzer`
6. 配置 `loader.py` 新增 `analyzer` 段；`config.yaml` 新增 `analyzer` 段
7. 运行 `test_heat_score.py` 确认全部通过

### 阶段 2：情感分析

8. `NewsItem` 加 `sentiment_score` 字段
9. `_build_row` 加对应列值
10. `JiebaAnalyzer.analyze_sentiment()` 实现
11. `data/senti_*.txt` 四个词典文件
12. `Crawler.enrich_content()` 之后调用 `analyze_sentiment`
13. 编写 `test_analyzer.py` sentiment 测试

### 风险控制

- 阶段 1 先行合并并验证，确认 heat_score 无回归后再合并阶段 2
- `analyzer.enabled: false` 时可完全跳过分析阶段，保持向后兼容
- `_query_today_hotlist()` 查询走已有索引，不引入性能问题
