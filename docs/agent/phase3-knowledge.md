# Phase 3：知识库 RAG

> **父文档**: [index.md](index.md)  
> **产出**: 知识库支撑角色扮演  
> **可验证**: 问知识库事实，答案正确

---

## 1. 设计思路

**原则**：Phase 3 前不引入知识库。先在 Phase 0-2 把 agent 骨架跑通，再用知识库赋予它"角色知识"。

**选型建议**：
- 起步：**LlamaIndex `KnowledgeGraphIndex` + pgvector**（利用现有 PG，零新组件）
- 进阶：**Neo4j + pgvector**（图遍历 + 向量检索混合，用于巴菲特 agent 类场景）
- 大型：**Microsoft GraphRAG**（自动建图+社区摘要，量大时考虑）

---

## 2. 知识库检索策略

**Phase 3 知识库内容通过 system prompt 前缀注入，不经过 tool call。**

```
用户消息
  │
  ▼
1. 知识检索：对用户消息做语义检索，找到 Top-5 相关片段
  │
  ▼
2. 前缀注入：将检索结果拼接到 system prompt 中
  │
  ▼
3. LLM 回复：模型在上下文中看到知识，直接回答
```

这种方式的好处是简单可靠，模型不会"忘记"去调工具。Phase 4 再升级为 tool 形式。

---

## 3. KnowledgeEngine

**文件**: `agent/knowledge/engine.py`

```python
class KnowledgeEngine:
    """知识库引擎：文档注入 → 索引 → 检索。"""

    def __init__(self, db):
        self.db = db

    def ingest_documents(self, docs: list[str]) -> None:
        """将文档切片 → embedding → 存入 pgvector。"""
        ...

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """检索与 query 最相关的知识片段。"""
        ...

    def build_system_prompt(self, query: str) -> str:
        """生成带知识上下文的 system prompt 前缀。"""
        results = self.search(query)
        if not results:
            return ""
        context = "\n\n".join(r["content"] for r in results)
        return f"以下知识可供参考：\n{context}"
```

---

## 4. Phase 4 时转为工具

在 Phase 4 的 tools 框架下，`search_knowledge` 被注册为 agent 可调用的工具：

```python
@tool
def search_knowledge(query: str) -> str:
    """Search the knowledge base for information relevant to the query."""
    results = knowledge_engine.search(query)
    return "\n\n".join(r["content"] for r in results)
```

---

## 实现检查清单

- [ ] `agent/knowledge/engine.py` → `KnowledgeEngine`
- [ ] 知识库文档切片 + embedding 入库
- [ ] 验证：问知识库事实，答案正确