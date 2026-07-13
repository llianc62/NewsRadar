# Analyzer — 分析引擎

分析引擎负责三类分析：**热度评分**（heat_score，0-100）、**情感分析**（sentiment_score，0-100）、**关键词提取**（tags）。

## 架构

```
news/analyzer/
├── __init__.py     # create_analyzer(config, db) 工厂函数
├── analyzer.py     # Analyzer 抽象基类
├── jieba.py        # JiebaAnalyzer — 本地离线分析（heat + sentiment + keywords）
└── agent.py        # AgentAnalyzer — LLM 分析（预留，NotImplementedError）
```

`Analyzer` 抽象基类定义两个接口：
- `analyze_heat(source_id, items, db_map)` — 原地修改 `item.heat_score` 和 `item.ranks`
- `analyze_sentiment(items)` — 原地修改 `item.sentiment_score`

工厂函数 `create_analyzer()` 根据 `config.yaml` 中 `analyzer.backend` 选择实现。`analyzer.enabled: false` 时返回 `None`，完全跳过分析。

## Heat Score（热度评分）

仅对 hotlist 来源生效。RSS、manual、cloud-synced 条目 `heat_score` 为 NULL/0。

### 三轮分类

每轮爬取时，`analyze_heat()` 查询当天 DB 中该源的已有记录（`db_map`），与本轮结果对比：

| 分类 | 判定 | 公式 |
|------|------|------|
| **New**（首次上榜） | URL 不在 db_map 中 | `(1 − rank/total) × 100` |
| **Existing**（仍在榜） | URL 在 db_map 中 | `prev_heat + (new_pct − old_pct) × 0.3` |
| **Dropped**（掉榜） | db_map 中有但本轮无 | `prev_heat × 0.7`（直接 UPDATE DB） |

阻尼系数：**0.3**（增量调整，避免单轮剧烈波动）和 **0.7**（掉榜衰减，连续 3 轮掉榜降至 ~34%）。结果 clamp 到 0-100。

### 云端快照

Cloud CI 爬取时计算百分位快照（只有 New 分支），写入 SQLite。Daemon 同步时 `total=0` 被 `valid_items` 过滤跳过，快照值原样保留。后续 daemon 自己抓取时增量算法自然接管。

### 轨迹示例

```
第1轮: #10/20 → heat=50 → 首次
第2轮: #7/20  → heat=55 → 升榜 (+15×0.3)
第3轮: #3/20  → heat=61 → 升榜 (+20×0.3)
第4轮: 不在榜 → heat=43 → 掉榜 (×0.7)
第5轮: 不在榜 → heat=30 → 再次掉榜
第6轮: #5/20  → heat=48 → 重新上榜（在衰减值上调整）
```

## Sentiment（情感分析）

基于 jieba 分词 + 四组词典的规则引擎。输出 0-100，50 为中性。

### 词典文件

| 文件 | 格式 | 内容 |
|------|------|------|
| `data/senti_positive.txt` | `词 权重` (1-5) | 正面词：暴涨 4、利好 3、涨停 4... |
| `data/senti_negative.txt` | `词 权重` (1-5) | 负面词：暴跌 4、亏损 3、跌停 4... |
| `data/senti_negation.txt` | 每行一词 | 否定词：不、没、非、并非、绝不... |
| `data/senti_degree.txt` | `词 系数` | 程度副词：非常 2.0、极其 2.5、略微 0.5... |

### 算法流程

```
title + content → clean_markdown_syntax() → jieba.lcut() → 逐词评分 → tanh 映射到 0-100
```

- 否定词翻转后续 3 词极性（"不会亏损" → 正面）
- 程度副词修改当前乘数（"极其利好" > "利好"）
- `tanh(net/5) × 50 + 50` 压缩避免极端值
- 词典惰性加载，首次调用时从 `data/` 读取

## 关键词提取（Tags）

`JiebaAnalyzer.extract_keywords()` — TF-IDF 优先，TextRank 兜底。

### 两层策略

1. **TF-IDF**（优先）：使用 jieba 内置 IDF 模型，自动压低通用词权重
2. **TextRank**（兜底）：TF-IDF 提取失败时回退，基于图排序算法

### 集成点

`Crawler._download_and_parse()` 中：trafilatura 没提取到 tags → jieba 从正文提取 5 个关键词。

## 配置

```yaml
# config.yaml
analyzer:
  enabled: true
  backend: jieba   # jieba | agent（未来）
```

## 关键文件

| 文件 | 用途 |
|------|------|
| `news/analyzer/jieba.py` | JiebaAnalyzer 完整实现（~400 行） |
| `news/analyzer/analyzer.py` | Analyzer 抽象基类 |
| `news/analyzer/__init__.py` | 工厂函数 + config 路由 |
| `news/analyzer/agent.py` | AgentAnalyzer 预留桩 |
| `data/senti_*.txt` | 四组情感词典 |
| `data/jieba_idf.txt` | 自定义 IDF 语料（自动构建） |
| `tests/test_heat_score.py` | Heat score 单元测试 |
| `tests/test_analyzer.py` | Analyzer 单元测试（heat + sentiment） |
