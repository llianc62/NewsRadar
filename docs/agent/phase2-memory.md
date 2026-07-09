# Phase 2：记忆功能

> **父文档**: [index.md](index.md)  
> **产出**: 跨会话记忆  
> **可验证**: 关页重开，问"我之前说过什么？"能回忆起

---

## 1. 核心概念

记忆系统负责**跨会话的长期事实留存**，与上下文管理（会话内的短期工作记忆）正交：

```
会话内：ContextManager（滑动窗口 + 摘要）  ← 短期
会话间：MemoryManager（提取 + 合并 + 检索） ← 长期
```

## 2. 记忆生命周期

```
每条消息回复后（异步触发，不阻塞用户）
  │
  ▼
1. 提取（Extract）
   LLM 分析最近一轮对话，输出"值得记的事实"列表
   每条事实 = 自然语言短句（如"用户偏好价值投资，关注巴菲特"）
  │
  ▼
2. 合并（Merge，批量）
   将所有新事实与已有记忆一起发送给 LLM
   一次 prompt 完成所有 ADD / UPDATE / DELETE / NOOP 决策
  │
  ▼
3. 存储（Store）
   合并后的结果写入持久化存储
  │
  ▼
下一次消息发送时（检索注入）
  │
  ▼
4. 检索（Retrieve）
   用户当前消息 → 语义检索 Top-3 相关记忆
   注入到 system prompt 作为"已知信息"
```

**设计要点**：
- 记忆提取**每条消息后异步执行**，不阻塞用户等待回复
- 如果前一条消息的记忆提取尚未完成，跳过本轮（不堆积）
- `Agent.close()` 仍作为保底触发（会话关闭时强制提取剩余记忆）

---

## 3. PgVector 安装

记忆系统依赖 pgvector 扩展。在 Phase 2 实施前，需要在 PostgreSQL 中安装：

```bash
# 在 Docker / 服务器上安装 pgvector
git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git
cd pgvector && make && make install

# 在数据库中启用
psql -U newsradar -d newsradar -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

---

## 4. 存储设计

**文件**: `agent/memory/store.py`

```python
from abc import ABC, abstractmethod

class MemoryStore(ABC):
    @abstractmethod
    def save_memory(self, key: str, content: str) -> None: ...
    @abstractmethod
    def search_memories(self, query: str, top_k: int = 3) -> list[dict]: ...
    @abstractmethod
    def delete_memory(self, key: str) -> None: ...
    @abstractmethod
    def list_memories(self) -> list[dict]: ...

class PgVectorStore(MemoryStore):
    """基于 pgvector 的记忆存储实现。

    复用 NewsRadar 已有的 PostgreSQL 连接池。
    记忆全局共享——所有 session 可检索到相同记忆。

    表结构：
      agent_memories (
        id SERIAL PRIMARY KEY,
        key TEXT UNIQUE NOT NULL,    -- 事实的唯一标识
        content TEXT NOT NULL,       -- 事实文本
        embedding vector(1536),      -- OpenAI embedding 维度
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
      )

    需要在数据库中预先安装 pgvector 扩展：
      CREATE EXTENSION IF NOT EXISTS vector;
    """
    ...
```

---

## 5. MemoryManager

**文件**: `agent/memory/manager.py`

```python
class MemoryManager:
    def __init__(self, store: MemoryStore, llm):
        self.store = store
        self.llm = llm

    def load(self, query: str = "") -> str:
        """检索相关记忆，返回注入 system prompt 的文本。"""
        if not query:
            return ""
        memories = self.store.search_memories(query, top_k=3)
        if not memories:
            return ""
        lines = [f"- {m['content']}" for m in memories]
        return "以下是你已知的信息：\n" + "\n".join(lines)

    async def extract_and_merge(self, turns: list[Turn]) -> None:
        """每条消息回复后异步执行：提取 → 批量合并 → 存储。

        设计要点：
        - 记忆全局共享，所有 session 可见
        - 一次 prompt 处理所有新事实，而非每条事实单独调 LLM
        - 不阻塞用户——调用方在后台任务中执行
        - 如果前一次提取尚未完成，跳过本次
        """
        # 1. 提取：LLM 从对话中抽取事实
        facts = await self._extract_facts(turns)
        if not facts:
            return

        # 2. 批量合并：一次 LLM 调用处理所有新事实
        existing = self.store.list_memories()
        actions = await self._batch_decide_actions(facts, existing)
        # actions = [("ADD", fact), ("UPDATE", fact), ("DELETE", key), ("NOOP", None)]

        # 3. 逐条执行
        for action, payload in actions:
            if action == "ADD":
                self.store.save_memory(_key(payload), payload)
            elif action == "UPDATE":
                self.store.save_memory(_key(payload), payload)
            elif action == "DELETE":
                self.store.delete_memory(payload)
            # NOOP → 跳过

    async def _extract_facts(self, turns: list[Turn]) -> list[str]:
        """LLM 从对话中提取值得记的事实。"""
        ...

    async def _batch_decide_actions(
        self, facts: list[str], existing: list[dict]
    ) -> list[tuple[str, str | None]]:
        """一次 LLM 调用，批量决定每条新事实的处理动作。

        输入：所有新事实 + 已有记忆列表
        输出：[(action, payload), ...]
          action: "ADD" | "UPDATE" | "DELETE" | "NOOP"
          payload: fact text (ADD/UPDATE) | key (DELETE) | None (NOOP)
        """
        ...
```

---

## 6. Agent 类更新

```python
class Agent:
    def __init__(self, llm_cfg: LlmConfig, db, session_id: int = 0,
                 window_size: int = 10):
        self.llm = build_llm(llm_cfg)
        self.ctx = ContextManager(window_size=window_size, llm=self.llm)
        store = PgVectorStore(db)
        self.memory = MemoryManager(store, self.llm)
        self.session_id = session_id
        self._memory_task: asyncio.Task | None = None  # 后台记忆提取任务

    async def chat_stream(self, message: str) -> AsyncIterator[str]:
        memory_prefix = self.memory.load(message)
        context = memory_prefix + "\n" + self.ctx.build_context(message)

        full_reply = ""
        async for chunk in self.llm.astream(context):
            content = chunk.content
            if content:
                full_reply += content
                yield content

        self.ctx.add_turn(message, full_reply)

        # 异步触发记忆提取（不阻塞回复）
        self._trigger_memory_extract()

    def _trigger_memory_extract(self) -> None:
        """后台执行记忆提取。如果前一次尚未完成则跳过。"""
        if self._memory_task and not self._memory_task.done():
            return  # 前一次还在跑，跳过
        self._memory_task = asyncio.create_task(
            self.memory.extract_and_merge(self.ctx.history)
        )

    async def close(self):
        """保底触发：等待记忆提取完成 + 强制提取剩余轮次。"""
        if self._memory_task:
            await self._memory_task
        # 再次提取，确保 close 前的最新轮次也被处理
        await self.memory.extract_and_merge(self.ctx.history)
```

---

## 实现检查清单

- [ ] 安装 pgvector 扩展
- [ ] `agent/memory/store.py` → `PgVectorStore`
- [ ] `agent/memory/manager.py` → `MemoryManager`（批量合并）
- [ ] `agent/agent.py` → 集成 MemoryManager（异步提取）
- [ ] 验证：关页重开，问"我之前说过什么？"能回忆起