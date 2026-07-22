# Phase 3：知识库 RAG（pgvector）

> **父文档**: [index.md](index.md)
> **状态**: 实施中（2026-07-14 启动）。仿 ai-hedge-fund `FundamentalsSnapshot` 模式：数据 build 一次、检索、render 成文本喂给任意角色。
> **产出**: pgvector 知识库 + `KnowledgeEngine`，支撑 [角色扮演](persona.md)
> **可验证**: `python -m cli knowledge ingest <file> --namespace buffett` 后，`python -m cli knowledge search "查询" --namespace buffett` 返回语义相关片段

---

## 1. 设计思路

**核心隐喻**：知识库是"角色的专业知识来源"。用户原话"基于知识库创建角色"——每个角色从知识库检索自己领域的知识，render 进 prompt，再用人格声音回答。

**关键决策**：
- **存储用 pgvector**（复用现有 PG，装扩展；零新组件）。起步选型，量大再考虑 LlamaIndex KnowledgeGraph / GraphRAG。
- **知识 render 进 prompt，不经过 tool call**（对齐 ai-hedge-fund snapshot 模式）。人格放 system prompt（静态），知识放 `## 知识库` 块（动态）。
- **namespace 隔离**：巴菲特角色只检索 `investing/buffett` 命名空间，宏观角色检索 `macro-economics`。共享单表 + namespace 过滤，最干净。
- **复用已存在的死字段**：`Context.knowledge_context`（`agent/models.py:20`，注释"由 Memory/Knowledge 在 on_before 中填充"）原作者预留但未接线。我们采用镜像 memory 注入的方式激活它。

**与 Phase 2 记忆的区别**：
| | 记忆（Phase 2） | 知识库（Phase 3） |
|---|---|---|
| 内容来源 | 对话中 LLM 自动提取的事实 | 外部文档/新闻人工 ingest |
| 检索 | jieba TF-IDF + PG FTS/ILIKE（无向量） | pgvector 语义向量检索 |
| 表 | `agent_memories` | `knowledge_chunks` |
| 注入 | `ctx.memory_context` -> `## 相关记忆` | `ctx.knowledge_context` -> `## 知识库` |
| 生命周期 | 跨会话自动累积 | 手动 ingest / 按 namespace 管理 |

---

## 2. 注入机制（镜像 memory）

知识注入**完全对称**于记忆注入（`agent/memory.py:246` `on_before_execute` + `agent/executor.py` `_build_messages`）：

```
用户消息
  │
  ▼
PersonaAgent._make_ctx() 调 KnowledgeEngine.retrieve_render(msg, namespace)
  │
  ▼
写入 ctx.knowledge_context（文本块）
  │
  ▼
executor._build_messages() 在 system_prompt 之后、user_input 之前插入：
  {"role": "system", "content": "## 知识库\n{knowledge_text}"}
  │
  ▼
LLM 在"人格声音 + 知识上下文"下回答
```

消息顺序：`[system_prompt 人格] -> [## 相关记忆] -> [## 知识库] -> [user_input] -> [history]`

**executor 改动**（`agent/executor.py` 两个 `_build_messages`，DirectExecutor:167 / ReActExecutor:284，各加 4 行，紧随 memory 块）：

```python
if ctx.knowledge_context:
    knowledge_text = ctx.knowledge_context if isinstance(ctx.knowledge_context, str) else str(ctx.knowledge_context)
    messages.append({"role": "system", "content": f"## 知识库\n{knowledge_text}"})
```

---

## 3. 数据模型

### Docker 镜像

`docker-compose.yml`：`postgres:16-alpine` -> `pgvector/pgvector:pg16`（vanilla 镜像无 pgvector）。

### knowledge_chunks 表

在 `storage/postgres.py:_init_agent_schema()`（:371）新增，仿现有 `CREATE TABLE IF NOT EXISTS` + 单 commit 模式：

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source      TEXT NOT NULL,              -- 文档来源（文件名/URL/新闻ID）
    namespace   TEXT NOT NULL DEFAULT '',    -- 命名空间隔离（investing/buffett, macro-economics...）
    content     TEXT NOT NULL,              -- 切片文本
    embedding   vector(1536),               -- OpenAI text-embedding-3-small 维度（可配置）
    metadata    JSONB DEFAULT '{}',         -- {type: philosophy|framework|news, ...}
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_namespace
    ON knowledge_chunks(namespace);

CREATE INDEX IF NOT EXISTS idx_knowledge_embedding
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
```

CRUD 方法用 `get_conn()` ctx manager（`postgres.py:567`，PgMemoryStorage 同款）：
- `ingest_knowledge(chunks: list[dict])` —— 批量插入（content + embedding + namespace + source + metadata）
- `search_knowledge(query_embedding: list[float], namespace: str, top_k: int) -> list[dict]` —— `ORDER BY embedding <=> %s::vector LIMIT %s`，返回 `1 - (embedding <=> q) AS similarity`
- `delete_knowledge(namespace: str) -> int` —— 按 namespace 清空
- `count_knowledge(namespace: str) -> int`

---

## 4. Embedding 客户端

**新建 `agent/knowledge/embedding.py`**。代码库现无任何 embedding 代码；`ModelHub`/`BaseClient` 只包 chat。DeepSeek 不提供 embedding 端点，故知识库配置独立 base_url/api_key/model。

```python
class EmbeddingClient:
    """OpenAI 兼容 /v1/embeddings 客户端。"""
    def __init__(self, api_key: str, base_url: str = "", model: str = "text-embedding-3-small"):
        ...
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding。返回与 texts 等长的向量列表。"""
```

配置走新 `knowledge` 段（独立于 `models` 段）。

---

## 5. KnowledgeEngine

**新建 `agent/knowledge/engine.py`**：

```python
class KnowledgeEngine:
    """知识库引擎：文档切片 -> embedding -> 存 pgvector -> 语义检索 -> render。"""

    def __init__(self, store: KnowledgeStore, embedding: EmbeddingClient, top_k: int = 5):
        self._store = store
        self._embedding = embedding
        self._top_k = top_k

    def ingest_documents(self, docs: list[dict], namespace: str) -> int:
        """docs=[{source, content, metadata}] -> 切片 -> embed -> 存库。返回切片数。"""
        chunks = []
        for doc in docs:
            for piece in chunk_text(doc["content"]):
                chunks.append({**doc, "content": piece, "namespace": namespace})
        embeddings = self._embedding.embed([c["content"] for c in chunks])
        self._store.ingest([{**c, "embedding": e} for c, e in zip(chunks, embeddings)])
        return len(chunks)

    def retrieve(self, query: str, namespace: str, top_k: int | None = None) -> list[dict]:
        """语义检索 Top-K 片段。"""
        q_emb = self._embedding.embed([query])[0]
        return self._store.search(q_emb, namespace, top_k or self._top_k)

    def retrieve_render(self, query: str, namespace: str, top_k: int | None = None) -> str:
        """检索 + 格式化成文本块（仿 FundamentalsSnapshot.render()）。返回空串若无结果。"""
        results = self.retrieve(query, namespace, top_k)
        if not results:
            return ""
        blocks = [f"[{r['source']}] (相关度 {r['similarity']:.2f})\n{r['content']}" for r in results]
        return "\n\n---\n\n".join(blocks)
```

### 切片器 `agent/knowledge/chunker.py`

按段落 + 长度切片（~512 token，重叠 64），保留段落边界。简单实现，不引入 langchain text splitter。

### 存储抽象 `agent/knowledge/store.py`

```python
class KnowledgeStore(ABC):
    @abstractmethod
    def ingest(self, chunks: list[dict]) -> None: ...
    @abstractmethod
    def search(self, query_embedding: list[float], namespace: str, top_k: int) -> list[dict]: ...
    @abstractmethod
    def delete(self, namespace: str) -> int: ...
    @abstractmethod
    def count(self, namespace: str) -> int: ...

class PgVectorKnowledgeStore(KnowledgeStore):
    """委托 storage/postgres.py 的 ingest_knowledge/search_knowledge/delete_knowledge/count_knowledge。"""
```

---

## 6. 配置

`config/loader.py` 加 `_load_knowledge_config()`，注册到 `load_config()` 的 `config = {...}` dict（:247），env 覆盖用 `_get_env_str()` 同款：

```yaml
knowledge:
  enabled: true
  embedding_base_url: ""                    # 空=OpenAI 默认；可指向本地/兼容服务
  embedding_api_key: ${OPENAI_API_KEY}      # 独立 key（DeepSeek 无 embedding 端点）
  embedding_model: text-embedding-3-small
  embedding_dim: 1536
  top_k: 5
  table: knowledge_chunks
```

---

## 7. CLI

`cli/knowledge.py`（Typer 子命令，仿 `cli/db.py` 模式）：

```bash
python -m cli knowledge ingest <file|dir> --namespace buffett     # 导入文档
python -m cli knowledge ingest-news --namespace news --limit 1000 # 从 PG news_articles 批量导入
python -m cli knowledge search "查询语句" --namespace buffett      # 语义检索测试
python -m cli knowledge list --namespace buffett                  # 查看切片数
python -m cli knowledge clear --namespace buffett --force         # 清空命名空间
```

---

## 8. 与 ai-hedge-fund 的对应

| ai-hedge-fund | NewsRadar |
|---|---|
| `FundamentalsSnapshot`（point-in-time 数据，render 喂任意 persona） | `KnowledgeEngine.retrieve_render()`（检索知识，render 喂任意角色） |
| `snapshot.render()` -> user prompt | `retrieve_render()` -> `ctx.knowledge_context` -> `## 知识库` 块 |
| `snapshot.content_hash`（LLM 缓存 key） | （暂不做缓存，v1 直接检索） |
| `build_snapshot()` 只允许角色知道 as-of 数据 | namespace 限制角色只检索自己领域 |

---

## 实现检查清单

- [x] `docker-compose.yml` 换 `pgvector/pgvector:pg16`（Docker Hub 不可达时用镜像源 `docker.m.daocloud.io/pgvector/pgvector:pg16` 拉取后打 tag）
- [x] `storage/postgres.py`：`_init_agent_schema` 加 `CREATE EXTENSION vector` + `knowledge_chunks` 表 + HNSW 索引
- [x] `storage/postgres.py`：`ingest_knowledge` / `search_knowledge` / `delete_knowledge` / `count_knowledge` 方法（get_conn ctx manager）
- [x] `agent/knowledge/embedding.py`：`EmbeddingClient`
- [x] `agent/knowledge/chunker.py`：文本切片
- [x] `agent/knowledge/store.py`：`KnowledgeStore` ABC + `PgVectorKnowledgeStore`
- [x] `agent/knowledge/engine.py`：`KnowledgeEngine`
- [x] `config/loader.py`：`_load_knowledge_config()`
- [x] `cli/knowledge.py`：ingest / search / list / clear 子命令
- [x] `tests/test_knowledge_engine.py`：单元（mock embedding + mock store）
- [x] `tests/test_knowledge_integration.py -m integration`：真 PG + pgvector 往返
- [x] 验证：ingest 文档后语义检索返回正确片段（`test_search_retrieves_relevant_chunk` 通过）

> Phase A 完成（2026-07-14）：知识库模块覆盖率 97%，全量单测 723 passed，集成测试 6 passed。下一步见 [persona.md](persona.md) Phase B。
