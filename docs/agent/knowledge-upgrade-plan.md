# 知识库 RAG 升级计划

> **父文档**: [index.md](index.md)
> **状态**: 规划中（2026-07-14 起草）。Phase A 朴素 dense RAG 已跑通并验证，本文档规划后续检索质量与规模的演进路径。
> **前置**: [phase3-knowledge.md](phase3-knowledge.md)（当前实现）、[persona.md](persona.md)（消费方）
> **触发条件**: 切片数突破 ~5 万 / 检索质量不达标（精确关键词漏召、边缘命中混入）/ 产品验证后需提质
> **原则**: 每个阶段独立可验证、不破坏现有 `KnowledgeEngine` 接口、本地优先（Ollama 可跑的不上云）

---

## 1. 现状评估

当前知识库（Phase A）是最朴素的 **dense-only RAG**：

```
文档 ──chunk_text()──► ~1200字符片段(overlap 200)
      ──EmbeddingClient.embed()──► 768维向量(nomic-embed-text)
      ──ingest_knowledge()──► knowledge_chunks(embedding vector(768), HNSW)

查询 ──embed(query)──► ORDER BY embedding <=> query::vector LIMIT 5
      ──retrieve_render()──► "## 知识库" 文本块 ──► 注入 system prompt
```

**已验证可用**：本地 Ollama `nomic-embed-text` ingest + search 跑通，相关度命中正确。当前规模（千级切片）查询 <10ms，性能充裕。

**短板清单**（朴素 dense 共性，非实现缺陷）：

| 短板 | 表现 | 根因 |
|------|------|------|
| 精确关键词漏召 | 查"贵州茅台"匹配不到含"茅台"的片段 | dense 只看语义相似，不保证词面命中 |
| 边缘命中混入 | 相关度 0.6 左右的弱相关片段挤进 top-5 | 无 rerank，召回即最终结果 |
| 语义鸿沟 | 查"08 年次贷"匹配不上"金融危机"片段 | 单次 embed 无法桥接同义表达 |
| 切片断上下文 | 关键论据被切到相邻片段 | 固定 1200 字符硬切，overlap 仅部分缓解 |
| 规模瓶颈 | namespace 内全扫，HNSW 不擅后过滤 | 当前仅 namespace 过滤，无 metadata 预过滤 |

**与记忆系统的对照**（值得注意）：`LongTermMemory` 用的是 jieba TF-IDF + PG FTS/CJK ILIKE（关键词检索），知识库却是纯 dense（语义检索）。两者检索范式恰好互补，后续 hybrid 化可统一思路。

---

## 2. 升级路线图

按 **性价比（质量提升 / 实现成本）** 排序，逐阶段独立交付：

| 优先级 | 升级 | 质量提升 | 成本 | 触发条件 |
|--------|------|---------|------|---------|
| **P1** | Hybrid 检索（BM25 + Dense 融合） | 高 | 中 | 中文场景几乎必做 |
| **P2** | Cross-encoder Rerank | 高 | 低 | top-5 质量不达标时 |
| **P3** | Query 改写 / 扩展（HyDE / 子问题） | 中 | 中 | 用户提问风格多变后 |
| **P4** | 语义切片 / Parent-Child | 中 | 中 | 长文档占比升高 |
| **P5** | Metadata 过滤 + 预过滤 | 低（质量）/ 高（规模） | 中 | 切片数破 5 万 |

**不做的事**（见第 8 节）：GraphRAG、独立向量库、云端 embedding。

---

## 3. P1 - Hybrid 检索（BM25 + Dense 融合）

**动机**：中文新闻场景精确关键词（公司名/股票代码/人名/政策名）命中很重要，纯 dense 会漏。PG 自带 `to_tsvector` + jieba 分词即可做 BM25，零新组件。

**方案**：召回阶段并行跑两路，分数归一化后加权融合。

```
query ──┬─ dense 路 ──► embed ──► ORDER BY <=> LIMIT 20
        └─ BM25 路  ──► to_tsvector(query) ──► ts_rank LIMIT 20
                                                        │
                                   归一化(min-max) + 加权融合(α=0.5)
                                                        ▼
                                              取 Top-5 返回
```

**实现草图**（`storage/postgres.py` 新增 `search_knowledge_hybrid`）：

```python
def search_knowledge_hybrid(self, q_emb, query_text, namespace, top_k=5, alpha=0.5):
    # dense 召回 top-20
    dense = self.search_knowledge(q_emb, namespace, top_k=20)  # 已有
    # BM25 召回 top-20（需建 tsvector 列 + jieba GIN 索引）
    bm25 = self._bm25_search(query_text, namespace, top_k=20)
    # 分数归一化 + 加权
    fused = _rrf_or_minmax(dense, bm25, alpha)  # RRF 更稳健
    return fused[:top_k]
```

**改动点**：
- `knowledge_chunks` 加 `tsv tsvector` 生成列 + GIN 索引（ingest 时自动维护）
- 中文分词：复用 `JiebaAnalyzer` 的 jieba，或建 PG `zhparser` 扩展（前者更轻）
- `KnowledgeEngine.retrieve()` 增加 `query_text` 透传（dense 路只需向量，hybrid 需原文）
- 融合算法优先 **RRF**（Reciprocal Rank Fusion，无需校准分数尺度，比 min-max 稳健）

**验证**：
- ingest 含"茅台""A股"等关键词的文档，搜精确词面能命中（当前会漏）
- 对比纯 dense，hybrid 的 nDCG@5 提升（建小评测集 ~20 query）

---

## 4. P2 - Cross-encoder Rerank

**动机**：dense 召回粗（双塔，query/doc 独立编码），rerank 精（交叉编码，query-doc 联合打分）。对 top-20 重排出 top-5，质量提升明显，开销小。

**方案**：召回 top-20（dense 或 hybrid）→ 本地 cross-encoder 重排 → 取 top-5。

```
召回 top-20 ──► bge-reranker-base(query, doc) 逐对打分 ──► 排序取 top-5
```

**实现**：
- 新增 `agent/knowledge/reranker.py`，`Reranker` 类包装本地模型
- 模型选 `BAAI/bge-reranker-base`（中文友好，~400MB，CPU 可跑）或 Ollama 暂无 reranker，走 HuggingFace `transformers` 直加载
- `KnowledgeEngine.retrieve()` 增加可选 rerank 阶段（配置开关 `knowledge.rerank: true/false`）

**改动点**：
- `KnowledgeEngine.__init__` 增 `reranker` 可选依赖（仿 `knowledge`/`analyzer` 的可选注入模式）
- `retrieve()` 召回 top-N（N=20）→ reranker 打分 → 截断 top_k
- 配置：`knowledge.rerank_model`、`knowledge.rerank_top_n`

**验证**：
- 边缘命中（similarity 0.6 的弱相关）被 rerank 挤出 top-5
- 重排延迟 <200ms（20 条 × cross-encoder），可接受

---

## 5. P3 - Query 改写 / 扩展

**动机**：用户提问与文档措辞不一致时（"市场恐慌" vs "暴跌""流动性危机"），单次 embed 桥接不了。让 LLM 先改写 query 再检索。

**方案**：两选一（或都做，配置切换）：

- **Multi-query**：LLM 把原 query 拆成 3-5 个子问题，并行检索后去重融合
- **HyDE**：LLM 先生成一个"假设答案"，用答案的 embedding 去检索（答案比问题更接近文档措辞）

```
原 query ──LLM──► [子问题1, 子问题2, ...] ──各 embed+检索──► 融合去重 top-5
        └─HyDE─► 假设答案 ──embed──► 检索 top-5
```

**实现**：
- `KnowledgeEngine` 增 `query_rewriter`（可选，复用 `ModelHub` 的 quick 模型）
- 配置：`knowledge.query_rewrite: none|multiquery|hyde`
- 成本：每次检索多 1 次 LLM 调用（用 quick 模型，~500ms）

**验证**：
- 查"08 年次贷"能召回含"金融危机""雷曼"的片段
- 改写延迟可接受（<1s）

---

## 6. P4 - 语义切片 / Parent-Child

**动机**：固定 1200 字符硬切可能切断论据上下文。parent-child 模式：召回小片段（精准），返回其所属大片段（完整上下文）。

**方案**：

- **语义切片**：按标题/段落语义边界切（而非固定字符数），可用 LLM 或规则（Markdown 标题层级）
- **Parent-Child**：存两层切片--parent（大块，~2000 字符）+ child（小块，~300 字符，带 `parent_id`）。检索 child，返回 parent。

```
parent(大块) ──► child(小块) × N
检索命中 child ──► 回溯 parent_id ──► 返回 parent 全文
```

**改动点**：
- `chunker.py` 增 `chunk_semantic()` / `chunk_parent_child()`
- `knowledge_chunks` 加 `parent_id` 列
- `retrieve_render()` 命中 child 后 JOIN 回 parent
- 配置：`knowledge.chunk_strategy: fixed|semantic|parent_child`

**验证**：
- 跨片段的论据不再丢失上下文
- 返回片段完整度提升（人工抽检）

---

## 7. P5 - Metadata 过滤 + 预过滤

**动机**：HNSW 是近似索引，**不擅长后过滤**（先取 top-K 再按 metadata 筛，可能筛没）。规模上来后需要按时间/来源/标签预过滤。

**方案**：

- **结构化 metadata**：`metadata` JSONB 列加 GIN 索引，支持 `source`/`date`/`tags` 过滤
- **预过滤**：先 SQL WHERE 缩小候选集，再在候选集上跑向量检索（pgvector 0.7+ 支持 filtered HNSW）
- **部分索引**：按 namespace 建 HNSW 部分索引（`WHERE namespace = 'investing/buffett'`），每命名空间独立索引

**改动点**：
- `search_knowledge()` 增 `filters: dict` 参数（`{"source": "...", "date_gte": "..."}`）
- `retrieve()` 透传 filters
- 配置：知识库 ingest 时支持 metadata 字段

**验证**：
- 大规模下（5 万+ 切片）过滤 + 检索延迟 <50ms
- 后过滤漏召回问题消失

---

## 8. 性能与规模基线

**当前性能**（本地 Ollama，千级切片）：

| 操作 | 延迟 | 瓶颈 |
|------|------|------|
| ingest 单文档（1 切片） | ~100ms | embedding（CPU） |
| search top-5 | <10ms | HNSW 查询 |
| query embedding | ~50-100ms | nomic-embed-text（CPU） |

**规模拐点**（何时该做哪个升级）：

| 切片数 | 建议升级 | 原因 |
|--------|---------|------|
| <1 万 | 维持现状 | 性能充裕，提质优先看产品反馈 |
| 1-5 万 | P1 hybrid + P2 rerank | 质量需求显现，PG 仍扛得住 |
| 5-10 万 | P5 metadata 预过滤 | HNSW 后过滤开始漏召 |
| 10 万+ | 评估独立向量库 | pgvector 单机 HNSW 内存压力 |

**本地 vs 云端 embedding**：当前本地 `nomic-embed-text`（768 维）零成本、数据不出本地，是正确选择。仅当 ingest 批量很大（万级文档）且本地延迟不可接受时，才考虑临时切云端 `text-embedding-3-small` 批量灌库（注意维度不同，需 DROP 重建表）。

---

## 9. 明确不做的事

| 不做 | 理由 |
|------|------|
| **GraphRAG / 知识图谱** | 实体抽取+关系建模成本高，NewsRadar 是新闻摘要不是推理任务，收益不匹配。等出现明确的多跳推理需求再评估 |
| **独立向量库**（Pinecone/Milvus/Qdrant） | pgvector 在 10 万级以内够用，多一个服务多一份运维。规模真到拐点再迁 |
| **云端 embedding 常驻** | 本地 Ollama 已够，云端只在批量灌库时临时用 |
| **多模态 embedding**（图片/图表） | 新闻正文为主，图表占比低，ROI 不足 |
| **实时增量索引**（新闻入库即进 KB） | 当前手动 ingest 足够；KB 是"专业知识"（投资哲学/宏观框架），不是"实时新闻"--新闻走 `search_news` 工具 |

---

## 10. 交付计划

每个 P 独立交付，互不阻塞。建议节奏：

- **P1 + P2 一起做**（检索质量核心提升，1-2 天）：hybrid 召回 + rerank，建小评测集验证 nDCG
- **P3 按需**（用户提问复杂度上来后）
- **P4 按需**（长文档占比升高后）
- **P5 规模驱动**（切片数逼近 5 万时）

每个 P 的交付物：实现 + 单元测试（mock embedding/reranker）+ 集成测试（真 PG）+ 配置开关（默认关闭，渐进启用）+ 本文档勾选。
