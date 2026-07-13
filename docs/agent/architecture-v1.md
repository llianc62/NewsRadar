# DefaultAgent v1 架构设计

> **版本**: v1.2  
> **设计日期**: 2026-07-09 / 更新 2026-07-10  
> **状态**: Phase 1+2+3 已完成  
> **目标**: 设计一个模块化、可组合的 Agent 基座，支持热插拔的能力模块

---

## 1. 设计哲学

### 1.1 机甲比喻

Agent 是一台功能强大的**机甲**：

| 组件 | 角色 | 说明 |
|------|------|------|
| **Brain (LLM Pool)** | 驾驶员 | 可以随时更换，不同驾驶员擅长不同任务 |
| **Memory** | 飞行记录仪 | 可选，记录历史，短期或长期 |
| **Knowledge** | 导航数据库 | 可选，外部知识索引 |
| **Tools** | 外挂装备 | MCP 协议连接的外部能力 |
| **Executor** | 战斗程序 | 决定"先做什么后做什么"的推理循环 |
| **Context** | 数据总线 | 所有模块之间传递数据的统一管道 |

### 1.2 核心原则

1. **模块独立构造，DI 注入** — 每个模块自己负责自己的初始化，通过构造器注入到 Agent 基座
2. **基座轻量** — `DefaultAgent` 只做编排，不做具体业务逻辑
3. **可选即不依赖** — Memory、Knowledge、Tools 都是可选项，不传就跳过
4. **Executor 是唯一的"流程拥有者"** — 只有 Executor 知道模块之间的调用顺序
5. **模块之间不直接依赖** — 所有跨模块通信通过 Context 进行

---

## 2. 架构总览

```
                         ┌─────────────────────────────────────┐
                         │          DefaultAgent               │
                         │    (轻量编排器 + 生命周期管理)        │
                         └──────────┬──────────────────────────┘
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         │           │              │              │           │
         ▼           ▼              ▼              ▼           ▼
   ┌─────────┐ ┌────────┐ ┌──────────┐ ┌───────────┐ ┌──────────┐
   │  Brain  │ │ Memory │ │Knowledge │ │ToolRegistry│ │ Executor │
   │LLM Pool │ │ 可选   │ │  可选    │ │  工具中心  │ │推理循环  │
   └────┬────┘ └────────┘ └──────────┘ └─────┬─────┘ └────┬─────┘
        │                                     │            │
        ▼                                     ▼            ▼
   ┌──────────┐                    ┌──────────────────┐ ┌──────────┐
   │BaseClient│                    │  ToolRegistry    │ │ Direct / │
   │Clients   │                    │                  │ │ ReAct    │
   └──────────┘                    │ ├─FunctionTool   │ └──────────┘
                                   │ │  (内置函数)     │
                                   │ └─MCPTool        │
                                   │   (agent/mcp/)  │
                                   │    ├─MCPClient   │
                                   │    │  ├─stdio    │
                                   │    │  │  └─News  │
                                   │    │  │    MCP   │
                                   │    │  │    Server│
                                   │    │  └─SSE      │
                                   │    │     └─外部   │
                                   │    │       MCP    │
                                   │    │       Server │
                                   └────────┬─────────┘
                                            │
                                   ┌────────┴────────┐
                                   │  Shared Context  │
                                   │ (模块间数据管道)   │
                                   └─────────────────┘
```

### 2.1 模块职责矩阵

| 模块 | 是否必需 | 有无状态 | 生命周期 | 与外部交互 |
|------|---------|---------|---------|-----------|
| **Brain** | ✅ 是 | 有（Client 池） | Agent 生命周期 | LLM API |
| **Memory** | ❌ 可选 | 有 | 会话/持久化 | PG / 内存 |
| **Knowledge** | ❌ 可选 | 有（索引） | 持久化 | 向量库 |
| **Tools** | ❌ 可选 | 无（无状态） | 每次调用 | MCP Server（在 `agent/mcp/`） |
| **Executor** | ✅ 是 | 无（纯逻辑） | 每次 execute | 无 |
| **Context** | ✅ 是 | 有（临时） | 单次 execute | 无 |

### 2.2 调用流程（Phase 1 简化版）

```
agent.chat(user_input)
  │
  ├─ 1. Context 初始化（清空上次数据，写入 user_input）
  │
  ├─ 2. Executor.run(ctx, brain)
  │     │
  │     ├─ 2a. 构建 messages（system + user）
  │     ├─ 2b. brain.get_default().chat(model, messages)
  │     ├─ 2c. ctx.assistant_output = response
  │     └─ 3. 返回最终结果
  │
  └─ 4. 返回给调用方
```

### 2.3 调用流程（Phase 3 完整版）

```
agent.chat(user_input)
  │
  ├─ 1. Context 初始化（清空上次数据，写入 user_input）
  │
  ├─ 2. Executor.run(ctx, brain, memory, tools)
  │     │
  │     ├─ [可选] 2a. Memory.on_before(ctx)    → 检索历史 → 写入 ctx
  │     │
  │     ├─ 2b. 构建 messages（system + memory + user + history）
  │     ├─ 2c. Brain.chat(model, messages, tools=schemas)  → LLM 调用
  │     │     └─ LLM 返回 ChatResult(content, tool_calls)
  │     │
  │     ├─ 2d. 如果有 tool_calls:
  │     │     ├─ 遍历每个 tool_call
  │     │     ├─ 解析函数名 + 参数
  │     │     ├─ registry.execute(name, args)  → 执行工具
  │     │     ├─ ctx.tool_calls.append(tc)
  │     │     ├─ ctx.tool_results.append(result)
  │     │     ├─ ctx.history.append(工具结果消息)
  │     │     └─ 回到 2b（继续推理循环）
  │     │
  │     ├─ 2e. 无 tool_calls → 最终文本响应
  │     ├─ [可选] 2f. Memory.on_after(ctx)    → 存储记忆
  │     │
  │     └─ 3. 返回最终结果
  │
  └─ 4. 返回给调用方
```

---

## 3. 模块详细设计

### 3.1 Brain — ModelHub

#### 3.1.1 职责

- 管理所有 LLM Client 的配置和生命周期
- 惰性创建 Client：用到哪个创建哪个
- **不封装 LLM 调用** — 只负责返回 `BaseClient` 实例，调用方（Executor）直接调 `client.chat()`

#### 3.1.2 配置格式

模型配置使用原始 `dict` 格式，不再封装 `ModelConfig` dataclass：

```python
{
    "default": {
        "protocol": "openai",      # 供应商协议
        "model": "gpt-4o",         # 实际模型 ID
        "api_key": "...",          # API Key
        "base_url": "",            # 可选自定义端点
    },
    "cheap": {
        "protocol": "openai",
        "model": "gpt-4o-mini",
        "api_key": "...",
    },
}
```

#### 3.1.3 BaseClient 设计

```python
class BaseClient(ABC):
    """所有 LLM Client 的基类。
    
    构造只收连接级参数，model 在每次调用时传入。
    同一个 Client 实例可切换不同模型。
    """
    
    def __init__(self, api_key: str, base_url: str = ""):
        self.api_key = api_key
        self.base_url = base_url
    
    @abstractmethod
    async def chat(self, model: str, messages: list[dict],
                   temperature: float = 0.7, top_p: float = 1.0,
                   **kwargs) -> str:
        """非流式调用，返回完整文本。"""
        ...
    
    @abstractmethod
    async def chat_stream(self, model: str, messages: list[dict],
                          temperature: float = 0.7, top_p: float = 1.0,
                          **kwargs) -> AsyncIterator[str]:
        """流式调用，逐 token 返回。"""
        ...


class OpenAIClient(BaseClient):
    def __init__(self, api_key: str, base_url: str = ""):
        self._client = OpenAI(api_key=api_key, base_url=base_url or None)
    
    async def chat(self, model, messages, temperature=0.7, top_p=1.0, **kwargs) -> str:
        resp = self._client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, top_p=top_p, **kwargs
        )
        return resp.choices[0].message.content
    
    async def chat_stream(self, model, messages, temperature=0.7, top_p=1.0, **kwargs) -> AsyncIterator[str]:
        stream = self._client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, top_p=top_p,
            stream=True, **kwargs
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


class AnthropicClient(BaseClient):
    def __init__(self, api_key: str, base_url: str = ""):
        self._client = Anthropic(api_key=api_key, base_url=base_url or None)
    
    async def chat(self, model, messages, temperature=0.7, top_p=1.0, **kwargs) -> str:
        ...
    
    async def chat_stream(self, model, messages, temperature=0.7, top_p=1.0, **kwargs) -> AsyncIterator[str]:
        ...
```

#### 3.1.4 ModelHub 设计（Phase 1 实际实现）

```python
_PROVIDER_MAP: dict[str, type[BaseClient]] = {
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
}


class ModelHub:
    """LLM Client 管理器。
    
    职责:
    - 管理模型配置（原始 dict 格式）
    - 惰性创建 BaseClient 实例
    - 按名称（别名）返回 Client
    
    不负责:
    - ❌ 封装 chat() / chat_stream() 调用
    - ❌ 模型选择逻辑（交给 Executor）
    
    使用方式:
        hub = ModelHub(config={
            "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "..."},
            "cheap":   {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "..."},
        })
        
        client = hub.get_default()
        resp = await client.chat(model="gpt-4o", messages=[...])
    """
    
    def __init__(self, config: dict):
        self._config = config
        self._clients: dict[str, BaseClient] = {}
    
    def get_default(self) -> BaseClient:
        return self.get("default")
    
    def get(self, name: str) -> BaseClient:
        if name not in self._clients:
            cfg = self._config[name]           # KeyError 自动传播
            client_cls = _PROVIDER_MAP.get(cfg["protocol"])
            if not client_cls:
                raise ValueError(
                    f"Unsupported protocol: {cfg['protocol']!r} "
                    f"(supported: {list(_PROVIDER_MAP)})"
                )
            self._clients[name] = client_cls(
                api_key=cfg["api_key"],
                base_url=cfg.get("base_url", ""),
            )
        return self._clients[name]
    
    def get_model_version(self, name: str) -> str:
        return self._config[name]["model"]
```

#### 3.1.5 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| Client 创建时机 | 启动时全量 / 惰性 | **惰性** | 配置可能包含 10+ 模型，但实际只用 2-3 个 |
| 模型参数传递 | 构造 / 调用参数 | **调用参数** | 同一 Client 实例可切不同模型，无需重建 |
| Manager 职责 | 管理+封装调用 / 仅管理 | **仅管理** | 只返回 BaseClient，职责清晰 |
| 供应商适配 | 继承 + 多态 / if-else | **继承 + 多态** | 新增供应商只需要新加一个子类 |
| 配置类型 | ModelConfig dataclass / 原始 dict | **原始 dict** | 减少一层包装，ModelConfig 只是透传 dict |
| 配置来源 | 全局 config / 构造注入 | **构造注入** | 无全局依赖，测试友好 |
| 配置解析 | ModelHub 内部 / DefaultAgent 内部 | **ModelHub 内部** | 配置是本模块的职责，不应由 Agent 代劳 |

---

### 3.2 Memory — 记忆模块

#### 3.2.1 职责

- 提供记忆存储和检索能力
- 支持两级：ShortTerm（内存） / LongTerm（持久化）
- 对外暴露 hook 接口，由 Executor 在合适的时机调用

#### 3.2.2 类设计

```python
class MemoryModule(ABC):
    """记忆模块基类。"""
    
    @abstractmethod
    async def on_before_execute(self, ctx: Context):
        """执行前：检索相关记忆，注入到 Context。"""
        ...
    
    @abstractmethod
    async def on_after_execute(self, ctx: Context):
        """执行后：存储本次交互到记忆。"""
        ...


class NullMemory(MemoryModule):
    """空记忆——什么都不做，用于关闭记忆功能。"""
    async def on_before_execute(self, ctx): pass
    async def on_after_execute(self, ctx): pass


class ShortTermMemory(MemoryModule):
    """
    短期记忆——内存级滑动窗口，重启即失。
    适用于：不需要跨会话记忆的场景（单页 AI 助手、一次性对话）。
    
    合并了原 InMemory 的设计，统一为消息列表滑动窗口。
    """
    
    def __init__(self, window_size: int = 20):
        self._window: list[dict] = []
        self._window_size = window_size
    
    async def on_before_execute(self, ctx: Context):
        """将历史消息列表注入 Context。"""
        ctx.memory_context = self._window  # 直接传消息列表
    
    async def on_after_execute(self, ctx: Context):
        self._window.append({"role": "user", "content": ctx.user_input})
        self._window.append({"role": "assistant", "content": ctx.assistant_output})
        if len(self._window) > self._window_size * 2:
            self._window = self._window[-(self._window_size * 2):]


class LongTermMemory(MemoryModule):
    """
    长期记忆——持久化存储，跨会话保留。
    适用于：角色扮演、个人助手等需要跨会话记忆的场景。

    记忆提取策略:
    - 周期性: 每 N 轮对话后自动合并记忆
    - 触发性: 检测到关键信息（命名实体、长输出）时即时存储
    """

    def __init__(self, storage: "MemoryStorage", extract_interval: int = 5,
                 extractor: callable | None = None):
        self._storage = storage
        self._extract_interval = extract_interval
        self._extractor = extractor
        self._turn_count = 0

    async def on_before_execute(self, ctx: Context):
        """语义检索相关记忆（jieba TF-IDF 提取关键词 → PG 搜索）。"""
        query = self._build_search_query(ctx.user_input)
        if not query:
            return
        memories = await self._storage.search(query, top_k=5)
        if memories:
            ctx.memory_context = self._format_memories(memories)

    async def on_after_execute(self, ctx: Context):
        self._turn_count += 1
        if self._should_extract(ctx):
            await self._extract_and_store(ctx)
        elif self._turn_count % self._extract_interval == 0:
            await self._batch_merge(ctx)

    def _should_extract(self, ctx) -> bool:
        # 触发条件：用户输入含命名实体 OR 助手输出 > 100 字符
        if self._has_notable_entities(ctx.user_input):
            return True
        return len(ctx.assistant_output) > 100

    @staticmethod
    def _has_notable_entities(text: str) -> bool:
        """用 jieba POS 标签检测人名/地名/机构名。"""
        if not text or len(text) < 3:
            return False
        words = jieba.posseg.lcut(text)
        for w in words:
            if w.flag in ("nr", "ns", "nt") and len(w.word) >= 2:
                return True
        return False

    @staticmethod
    def _build_search_query(user_input: str) -> str:
        """中文用 jieba TF-IDF 提取关键词，英文原样传给 PG FTS。"""
        ...
```

#### 3.2.3 存储层设计（LongTermMemory 专用）

```python
class MemoryStorage(ABC):
    """记忆持久化存储基类。"""

    @abstractmethod
    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """语义搜索相关记忆。"""
        ...

    @abstractmethod
    async def save(self, session_id: str, content: str, memory_type: str = "summary"):
        """保存一条记忆。"""
        ...

    @abstractmethod
    async def batch_save(self, records: list[dict]):
        """批量保存。"""
        ...


class PgMemoryStorage(MemoryStorage):
    """
    PostgreSQL 全文搜索实现的记忆存储。

    不使用 pgvector（无 embedding 依赖），使用双策略搜索：
    - 中文：jieba 分词 + ILIKE ANY + pg_trgm GIN 索引
    - 英文：PG FTS (tsvector / english 配置)

    表结构:
        agent_memories (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id  TEXT NOT NULL,
            agent_name  TEXT NOT NULL DEFAULT '',
            memory_type TEXT NOT NULL DEFAULT 'summary',
            content     TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT now(),
            updated_at  TIMESTAMPTZ DEFAULT now()
        )

    索引:
        - idx_memories_session: (session_id, created_at DESC)
        - idx_memories_search: GIN (to_tsvector('english', content))
        - idx_memories_search_trgm: GIN (content gin_trgm_ops)
    """
    
    def __init__(self, db, agent_name: str = ""):
        self._db = db
        self._agent_name = agent_name

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        return await asyncio.to_thread(self._search_sync, query, top_k)

    def _search_sync(self, query: str, top_k: int) -> list[dict]:
        with self._db.get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if _is_cjk(query):
                    keywords = query.split()
                    patterns = [f"%{kw}%" for kw in keywords]
                    cur.execute("""
                        SELECT id, session_id, memory_type, content, created_at
                        FROM agent_memories
                        WHERE agent_name = %s AND content ILIKE ANY(%s)
                        ORDER BY created_at DESC LIMIT %s
                    """, (self._agent_name, patterns, top_k))
                else:
                    tsq = " | ".join(query.split())
                    cur.execute("""
                        SELECT id, session_id, memory_type, content, created_at
                        FROM agent_memories
                        WHERE agent_name = %s
                          AND to_tsvector('english', content) @@ to_tsquery('english', %s)
                        ORDER BY ts_rank(...) DESC LIMIT %s
                    """, (self._agent_name, tsq, tsq, top_k))
                return [dict(r) for r in cur.fetchall()]

    async def save(self, session_id, content, memory_type="summary"):
        return await asyncio.to_thread(self._save_sync, session_id, content, memory_type)
    ```

#### 3.2.4 记忆存储策略

```
周期性存储:
  turn 1  turn 2  ...  turn 5  →  合并对话 → LLM 生成摘要 → 存到 PG
  turn 6  turn 7  ...  turn 10 →  合并对话 → LLM 生成摘要 → 存到 PG

触发性存储:
  用户: "我叫张三"
    ↓ LLM 判断：这是个人信息，需要记住
  立即提取 → 存到 PG (memory_type = 'fact')

  用户: "帮我分析一下昨天的新闻"
    ↓ LLM 判断：普通查询，不需要记住
  跳过
```

#### 3.2.5 记忆层级对比

| 维度 | ShortTermMemory | LongTermMemory |
|------|----------------|----------------|
| 存储介质 | 内存（列表） | PostgreSQL + pgvector |
| 持久性 | 重启即失 | 跨会话持久 |
| 检索方式 | 全量返回滑动窗口 | 语义向量检索 top-K |
| 大小限制 | 窗口大小（默认 20 轮） | 无限制 |
| 适用场景 | 单次对话上下文 | 跨会话记忆 |
| 是否需要 LLM | 不需要 | 需要（提取摘要） |
| 成本 | 零 | 每次存储/检索有计算成本 |

---

### 3.3 Knowledge — 知识库模块

#### 3.3.1 职责

- 管理外部知识库的索引和检索
- 与 Memory 的区分：Knowledge 的数据**不是对话产生的**，是外部注入的
- 可选模块：不传就不启用

#### 3.3.2 类设计

```python
class KnowledgeModule(ABC):
    """知识库模块基类。"""
    
    @abstractmethod
    async def on_before_execute(self, ctx: Context):
        """执行前：检索相关知识，注入 Context。"""
        ...


class NullKnowledge(KnowledgeModule):
    """无知识库。"""
    async def on_before_execute(self, ctx): pass


class RAGKnowledge(KnowledgeModule):
    """
    基于向量检索的知识库。
    
    使用方式:
        kb = RAGKnowledge(
            name="news_knowledge",
            embedding=OpenAIEmbedding(),
            vector_store=PgVector(connection=pg),
        )
    """
    
    def __init__(self, name: str, embedding, vector_store):
        self._name = name
        self._embedding = embedding
        self._store = vector_store
    
    async def on_before_execute(self, ctx: Context):
        if not ctx.user_input:
            return
        results = await self._store.similarity_search(
            query=ctx.user_input,
            top_k=3,
        )
        ctx.knowledge_context = results
```

---

### 3.4 Tools — 工具模块

#### 3.4.1 职责

- 管理 Agent 可用的所有工具
- 提供**两种工具来源**：内置函数工具（FunctionTool）和 MCP 工具（MCPTool）
- 将工具统一转换为 OpenAI 格式的 tool schema，供 LLM function calling 使用
- 根据 LLM 的 tool_call 请求执行对应工具并返回结果

#### 3.4.2 核心数据结构

```python
@dataclass
class ToolDef:
    """工具定义——用于生成 LLM 可识别的 tool schema。"""
    name: str
    description: str
    input_schema: dict  # JSON Schema


@dataclass
class ToolCall:
    """一次工具调用的记录——存入 Context.tool_calls。"""
    name: str
    args: dict
    result: str = ""
    error: str = ""
```

工具 schema 统一输出为 **OpenAI tool format**（事实标准，Anthropic 也兼容）：

```python
# ToolRegistry.get_schemas() 的输出格式
[
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "四则运算计算器",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                    "op": {"type": "string", "enum": ["+", "-", "*", "/"]},
                },
                "required": ["a", "b", "op"],
            },
        },
    },
    ...
]
```

#### 3.4.3 工具基类与实现

```python
class BaseTool(ABC):
    """所有工具的基类。"""

    @abstractmethod
    def get_def(self) -> ToolDef:
        """返回工具定义（name + description + input_schema）。"""
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """执行工具调用，返回文本结果。"""
        ...


class FunctionTool(BaseTool):
    """内置函数工具——包装一个纯 Python 函数。

    无需外部服务，零网络开销。适用于：
    - 计算器、时间、日期等无状态工具
    - 直接操作项目内部数据（DB 查询、文件读取）
    """

    def __init__(self, name: str, description: str,
                 fn: callable, input_schema: dict):
        self._name = name
        self._description = description
        self._fn = fn
        self._schema = input_schema

    def get_def(self) -> ToolDef:
        return ToolDef(self._name, self._description, self._schema)

    async def execute(self, **kwargs: Any) -> str:
        if asyncio.iscoroutinefunction(self._fn):
            result = await self._fn(**kwargs)
        else:
            result = await asyncio.to_thread(self._fn, **kwargs)
        return str(result)


class MCPTool(BaseTool):
    """MCP 工具——通过 MCP Client 代理执行。

    不直接持有连接，通过 MCPClient 的 session 转发调用。
    """

    def __init__(self, client: MCPClient, tool_info: dict):
        self._client = client
        self._name = tool_info["name"]
        self._description = tool_info.get("description", "")
        self._input_schema = tool_info.get("inputSchema", {})

    def get_def(self) -> ToolDef:
        return ToolDef(self._name, self._description, self._input_schema)

    async def execute(self, **kwargs: Any) -> str:
        return await self._client.call_tool(self._name, kwargs)
```

#### 3.4.4 MCP Client

负责连接 MCP Server，管理会话和工具列表：

```python
class MCPClient:
    """MCP 协议客户端——连接到一个 MCP Server。

    支持两种传输方式：
    - stdio：服务端作为子进程运行（stdin/stdout 管道）
    - SSE：  服务端作为 HTTP 服务运行（Server-Sent Events）
    """

    def __init__(self, name: str = ""):
        self._name = name
        self._session: ClientSession | None = None
        self._tools: list[dict] = []
        self._proc: asyncio.SubprocessProcess | None = None

    async def connect_stdio(self, command: str, *args: str) -> None:
        """通过子进程 stdio 连接 MCP Server。

        Agent 内部启动的子进程 MCP Server，零网络延迟：
            client = MCPClient("news-radar")
            await client.connect_stdio("python", "-m", "agent.mcp.news_server")
        """
        self._proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        read_stream = self._proc.stdout  # StreamReader
        write_stream = self._proc.stdin  # StreamWriter
        self._session = await ClientSession(read_stream, write_stream).__aenter__()
        await self._session.initialize()
        self._tools = (await self._session.list_tools()).tools

    async def connect_sse(self, url: str) -> None:
        """通过 SSE 连接远程 MCP Server。

        直连外部提供的 MCP 服务：
            client = MCPClient("external-news")
            await client.connect_sse("http://some-host:8000/mcp")
        """
        self._session = await ClientSession.sse_connect(url).__aenter__()
        await self._session.initialize()
        self._tools = (await self._session.list_tools()).tools

    async def call_tool(self, name: str, args: dict) -> str:
        if not self._session:
            raise RuntimeError(f"MCPClient '{self._name}' not connected")
        result = await self._session.call_tool(name, args)
        return str(result.content[0].text) if result.content else ""

    async def close(self) -> None:
        if self._session:
            await self._session.__aexit__(None, None, None)
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            await self._proc.wait()

    def has_tool(self, name: str) -> bool:
        return any(t["name"] == name for t in self._tools)

    def get_schemas(self) -> list[dict]:
        """返回本连接所有工具的 OpenAI format schema。"""
        schemas = []
        for t in self._tools:
            schemas.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {}),
                },
            })
        return schemas
```

#### 3.4.5 ToolRegistry——工具注册中心

```python
class ToolRegistry:
    """工具注册中心——管理 Agent 的所有可用工具。

    同时持有 FunctionTool 和 MCPTool，对外输出统一的 schema。
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def add_tool(self, tool: BaseTool) -> None:
        """注册一个工具（FunctionTool 或 MCPTool）。"""
        self._tools[tool.get_def().name] = tool

    def add_mcp(self, client: MCPClient) -> None:
        """将一个 MCP Client 的所有工具批量注册。"""
        for t in client._tools:
            self._tools[t["name"]] = MCPTool(client, t)

    def get_schemas(self) -> list[dict]:
        """返回所有工具的 OpenAI format schema 列表。"""
        return [self._to_openai_schema(tool) for tool in self._tools.values()]

    async def execute(self, name: str, args: dict) -> str:
        """执行工具调用。"""
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"Tool '{name}' not found")
        return await tool.execute(**args)

    @staticmethod
    def _to_openai_schema(tool: BaseTool) -> dict:
        d = tool.get_def()
        return {
            "type": "function",
            "function": {
                "name": d.name,
                "description": d.description,
                "parameters": d.input_schema,
            },
        }
```

#### 3.4.6 使用方式

```python
# ── 组装工具集合 ──

registry = ToolRegistry()

# 内置函数工具
registry.add_tool(FunctionTool(
    name="calculator",
    description="四则运算计算器",
    fn=lambda a, b, op: {"+": a+b, "-": a-b, "*": a*b, "/": a/b}[op],
    input_schema={
        "type": "object",
        "properties": {
            "a": {"type": "number"},
            "b": {"type": "number"},
            "op": {"type": "string", "enum": ["+", "-", "*", "/"]},
        },
        "required": ["a", "b", "op"],
    },
))

# MCP 工具：连接内部 NewsRadar MCP Server（子进程）
news_mcp = MCPClient("news-radar")
await news_mcp.connect_stdio("python", "-m", "agent.mcp.news_server")
registry.add_mcp(news_mcp)

# MCP 工具：直连外部 MCP Server（HTTP SSE）
ext_mcp = MCPClient("external")
await ext_mcp.connect_sse("http://some-service:8000/mcp")
registry.add_mcp(ext_mcp)

# ── 传给 Agent ──
agent = DefaultAgent(
    config=config["models"],
    executor=ReActExecutor(tools=registry),
)
```

#### 3.4.7 MCP Server（NewsRadar 自身暴露的工具）

除了消费外部 MCP 工具，Agent 也需要把自己内部的能力包装成 MCP Server，供自己或其他 Agent 调用。

NewsRadar 自带的 MCP Server 暴露新闻系统的核心能力：

```python
# agent/tools/news_server.py
from mcp.server import Server
from mcp.server.stdio import stdio_server

app = Server("news-radar")


@app.tool()
async def search_news(query: str, limit: int = 10) -> str:
    """搜索新闻。"""
    ...


@app.tool()
async def get_hot_topics(tier: str = "T1") -> str:
    """获取当前热门话题（T1-T4）。"""
    ...


@app.tool()
async def get_news_detail(news_id: str) -> str:
    """获取单条新闻的完整内容。"""
    ...


@app.tool()
async def analyze_sentiment(text: str) -> str:
    """分析文本的情感倾向（0-100）。"""
    ...


@app.tool()
async def get_source_stats(source_id: str = "") -> str:
    """获取新闻来源统计。"""
    ...


if __name__ == "__main__":
    app.run(stdio_server())
```

用 `mcp` Python SDK（`pip install mcp`）实现，`@app.tool()` 装饰器自动生成 MCP 协议的 `list_tools` / `call_tool` 响应。Server 通过 stdio 运行，零网络开销。

#### 3.4.8 工具来源对比

| 维度 | FunctionTool | MCPTool（stdio） | MCPTool（SSE） |
|------|-------------|-----------------|---------------|
| 传输方式 | 函数调用 | 子进程管道 | HTTP 网络 |
| 延迟 | ~0 | 微秒级 | 网络延迟 |
| 工具来源 | 内置函数 | 内部 MCP Server | 外部 MCP 服务 |
| 部署 | 无依赖 | 同进程内子进程 | 独立服务 |
| 典型场景 | calculator, time, DB 查询 | NewsRadar 内部能力 | 第三方 API 桥接 |
| 故障隔离 | 无（同进程） | 子进程崩溃不影响 Agent | 网络中断不影响 Agent |

---

### 3.5 Executor — 推理循环

#### 3.5.1 职责

这是整个设计中**最关键**的模块。Executor 决定：
- 什么时候调 LLM，什么时候调工具
- LLM 的输出是最终回答还是需要继续调用工具
- 模块的调用顺序（先查记忆还是先查知识库）
- 失败处理和重试策略
- 最大迭代步数

#### 3.5.2 类设计

```python
class Executor(ABC):
    """执行器基类——定义 Agent 的执行策略。"""
    
    @abstractmethod
    async def run(self, ctx: Context, brain: ModelHub, **kwargs) -> str:
        """执行一次完整的推理循环。"""
        ...
    
    @abstractmethod
    async def run_stream(self, ctx: Context, brain: ModelHub, **kwargs) -> AsyncIterator[str]:
        """流式版本。"""
        ...


class DirectExecutor(Executor):
    """
    简单直调执行器——没有 ReAct 循环，没有工具调用。
    适用于：简单问答、分类、不需要工具的纯文本场景。
    
    Phase 1 实现：只接受 ctx 和 brain，不依赖 memory/knowledge/tools。
    Phase 2+ 扩展：加入 hook 调用点。
    """
    
    async def run(self, ctx: Context, brain: ModelHub, **kwargs) -> str:
        model_name = ctx.model_name or "default"
        client = brain.get(model_name)
        
        messages = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        # Phase 2+: memory.on_before → memory_context 注入
        # Phase 2+: knowledge.on_before → knowledge_context 注入
        messages.append({"role": "user", "content": ctx.user_input})
        
        response = await client.chat(
            model=brain.get_model_version(model_name),
            messages=messages,
        )
        
        ctx.assistant_output = response
        ctx.model_used = model_name
        # Phase 2+: memory.on_after → 存储记忆
        return response
    
    async def run_stream(self, ctx: Context, brain: ModelHub, **kwargs) -> AsyncIterator[str]:
        model_name = ctx.model_name or "default"
        client = brain.get(model_name)
        
        messages = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        messages.append({"role": "user", "content": ctx.user_input})
        
        chunks: list[str] = []
        async for token in client.chat_stream(
            model=brain.get_model_version(model_name),
            messages=messages,
        ):
            chunks.append(token)
            yield token
        
        ctx.assistant_output = "".join(chunks)
        ctx.model_used = model_name


class ReActExecutor(Executor):
    """
    ReAct 风格的推理循环——Agent 自主决定调工具还是回答。

    流程:
    1. memory.on_before   → 检索相关记忆，注入 Context
    2. tools.get_schemas  → 获取所有工具 schema
    3. 构建 messages（system + memory_context + user）
    4. 调 LLM（带 tools 参数）
    5. 解析 LLM 响应:
       - 如果是 tool_call → 执行工具 → 记录到 Context → 回到 3
       - 如果是 text       → 存记忆 → 返回
    6. 超过 max_steps → 终止并返回当前输出

    Phase 3 实现。
    """

    def __init__(self, max_steps: int = 10, max_retries: int = 3):
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        self.max_steps = max_steps
        self.max_retries = max_retries

    async def run(
        self,
        ctx: Context,
        brain: ModelHub,
        memory: MemoryModule | None = None,
        tools: ToolRegistry | None = None,
        **kwargs: Any,
    ) -> str:
        _memory = memory or NullMemory()
        await _memory.on_before_execute(ctx)

        tool_schemas = tools.get_schemas() if tools else None
        model_name = ctx.model_name or "default"
        client = brain.get(model_name)
        model = brain.get_model_version(model_name)

        for step in range(self.max_steps):
            messages = self._build_messages(ctx)
            response = await client.chat(
                model=model,
                messages=messages,
                tools=tool_schemas,
            )

            tool_calls = self._parse_tool_calls(response)
            if not tool_calls:
                # LLM 直接返回文本 → 完成
                ctx.assistant_output = response
                ctx.model_used = model_name
                ctx.step_count = step + 1
                await _memory.on_after_execute(ctx)
                return response

            # 执行工具调用
            for tc in tool_calls:
                result = await self._execute_tool_safe(tools, tc)
                ctx.tool_calls.append(tc)
                ctx.tool_results.append(result)
                # 注入工具结果作为新一轮的 user 消息
                ctx.history.append({
                    "role": "user",
                    "content": f"工具 {tc.name} 返回: {result}",
                })

        # 超过 max_steps：返回最后的内容
        ctx.assistant_output = ctx.history[-1]["content"] if ctx.history else "已达最大步数"
        ctx.step_count = self.max_steps
        return ctx.assistant_output

    async def run_stream(self, ...) -> AsyncIterator[str]:
        # Phase 3 流式场景：非流式调工具，流式输出最终回答
        result = await self.run(ctx, brain, memory, tools, **kwargs)
        yield result

    @staticmethod
    def _parse_tool_calls(response: str) -> list[ToolCall]:
        """解析 LLM 响应中的 tool_call。

        不同 API 返回格式不同：
        - OpenAI: 响应中的 choices[0].message.tool_calls
        - Anthropic: 响应中的 content 块含 tool_use

        这里需要 Client 层的支持，目前 Client.chat() 返回 str，
        Phase 3 实现时需要扩展 Client 返回结构化结果。
        """
        ...

    @staticmethod
    def _build_messages(ctx: Context) -> list[dict]:
        messages = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        if ctx.memory_context:
            messages.append({"role": "system", "content": f"## 相关记忆\n{ctx.memory_context}"})
        messages.append({"role": "user", "content": ctx.user_input})
        messages.extend(ctx.history)  # 工具调用的历史
        return messages


class PlanExecutor(Executor):
    """
    计划式执行器——先规划再执行。
    适用于：复杂任务，需要先分解子任务再逐步执行。
    Phase 4 预留。
    """
    async def run(self, ctx, brain, **kwargs) -> str:
        raise NotImplementedError("PlanExecutor: Phase 4")
```

#### 3.5.3 Executor 的选择策略

```python
# 简单场景 → DirectExecutor
agent = DefaultAgent(
    config={
        "default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "..."},
    },
    executor=DirectExecutor(),
)

# 需要工具调用 → ReActExecutor (Phase 3)
agent = DefaultAgent(
    config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "..."},
    },
    executor=ReActExecutor(max_steps=15),
    tools=registry,  # ToolRegistry 实例
)

# 复杂任务 → PlanExecutor (Phase 4)
agent = DefaultAgent(
    config={
        "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "..."},
    },
    executor=PlanExecutor(),
    tools=ToolModule(servers=["news", "finance", "research"]),
    memory=LongTermMemory(storage=pg_storage),
    knowledge=RAGKnowledge(name="research_docs", ...),
)
```

---

### 3.6 Context — 数据管道

#### 3.6.1 职责

- 单次 `execute()` 调用内的数据传递
- 所有模块读写同一个 Context 对象
- execute 结束后释放

#### 3.6.2 类设计（Phase 1 实际实现）

```python
@dataclass
class Context:
    """单次 chat() 调用的共享上下文。"""
    
    # 输入
    user_input: str
    session_id: str = ""
    system_prompt: str = ""
    model_name: str = "default"
    
    # 输出
    assistant_output: str = ""
    
    # 元数据
    step_count: int = 0
    model_used: str = ""
    total_tokens: int = 0


@dataclass
class AgentResult:
    """Agent 调用的返回结果。"""
    
    content: str
    model_used: str = ""
    total_tokens: int = 0
```

#### 3.6.3 Phase 2+ Context 扩展计划

```python
@dataclass
class Context:
    """单次 execute 调用的共享上下文。"""
    
    # 输入
    user_input: str
    session_id: str = ""
    system_prompt: str = ""
    model_name: str = "default"
    
    # 模块写入（由 Memory/Knowledge 在 on_before 中填充）
    memory_context: Any = None       # 记忆检索结果 ← Phase 2
    knowledge_context: Any = None    # 知识库检索结果 ← Phase 4
    
    # 执行过程
    history: list[dict] = field(default_factory=list)  # ← Phase 2
    tool_calls: list = field(default_factory=list)      # ← Phase 3
    tool_results: list = field(default_factory=list)    # ← Phase 3
    
    # 输出
    assistant_output: str = ""
    
    # 元数据
    step_count: int = 0
    model_used: str = ""
    total_tokens: int = 0


@dataclass
class ToolCall:
    """一次工具调用的记录。"""
    name: str
    args: dict
    result: str = ""
    error: str = ""
```

---

## 4. DefaultAgent 基座

### 4.1 类设计（Phase 1 实际实现）

```python
class DefaultAgent:
    """模块化 Agent 基座。
    
    config 接收总配置文件（config.yaml）中关于模型配置的子项 dict
    （即 config["models"]），直接传递给 ModelHub。
    config 是必传参数。
    
    使用方式:
        # 最小构造——只传必填参数
        agent = DefaultAgent(
            config={
                "default": {"protocol": "openai", "model": "gpt-4o-mini", "api_key": "..."},
            },
            executor=DirectExecutor(),
        )
        
        # 带系统提示词
        agent = DefaultAgent(
            config={
                "default": {"protocol": "openai", "model": "gpt-4o", "api_key": "..."},
            },
            system_prompt="你是一个新闻分析助手。",
        )
        
        result = await agent.chat("今天的头条是什么？")
    """
    
    def __init__(
        self,
        config: dict,
        executor: Executor | None = None,
        memory: MemoryModule | None = None,
        system_prompt: str = "",
    ):
        self.brain = ModelHub(config=config)
        self.executor = executor or DirectExecutor()
        self.memory = memory or NullMemory()
        self.system_prompt = system_prompt
    
    async def chat(
        self,
        user_input: str,
        session_id: str = "",
        model_name: str = "",
    ) -> AgentResult:
        ctx = Context(
            user_input=user_input,
            session_id=session_id,
            system_prompt=self.system_prompt,
            model_name=model_name or "default",
        )
        result_text = await self.executor.run(
            ctx=ctx, brain=self.brain,
            memory=self.memory,
        )
        return AgentResult(
            content=result_text,
            model_used=ctx.model_used,
            total_tokens=ctx.total_tokens,
        )
    
    async def chat_stream(
        self,
        user_input: str,
        session_id: str = "",
        model_name: str = "",
    ) -> AsyncIterator[str]:
        ctx = Context(
            user_input=user_input,
            session_id=session_id,
            system_prompt=self.system_prompt,
            model_name=model_name or "default",
        )
        async for token in self.executor.run_stream(ctx=ctx, brain=self.brain):
            yield token
```

### 4.2 设计要点

| 特性 | 说明 |
|------|------|
| executor 可选 | 不传则自动使用 DirectExecutor |
| config 必传 | 没有默认配置，必须显式传入 |
| memory | Phase 2 已接入（memory 构造参数） |
| tools | Phase 3 已接入（tools 构造参数 + ReActExecutor） |
| knowledge | Phase 4 预留 |
| _parse_model_config | 已移除，ModelHub 自己负责解析配置 |

---

## 5. 模块间通信协议

### 5.1 核心规则

1. **模块之间不直接调用** — 所有通信通过 Context 进行
2. **Executor 是唯一的编排者** — 它决定什么时候调哪个模块
3. **模块只暴露 hook 方法** — `on_before_execute` / `on_after_execute`
4. **Context 是临时对象** — 每次 execute 创建，结束后丢弃

### 5.2 数据流图（Phase 3 完整版）

```
ReActExecutor.run(ctx, brain, memory, tools)
  │
  ├─ ctx.user_input = "帮我查一下 AAPL 的股价"
  │
  ├─ memory.on_before_execute(ctx)
  │     └─ ctx.memory_context = "用户昨天问过 AAPL 的财报..."
  │
  ├─ schemas = tools.get_schemas()  # ToolRegistry → OpenAI format
  ├─ response = await client.chat(
  │     model=brain.get_model_version("default"),
  │     messages=[system, memory_context, user],
  │     tools=schemas,
  │   )
  │     └─ response = ChatResult(
  │           tool_calls=[{
  │             "function": {"name": "get_stock_price", "arguments": '{"ticker": "AAPL"}'}
  │           }]
  │         )
  │
  ├─ [step 1] ctx.tool_calls.append(...)
  ├─ [step 1] result = await tools.execute("get_stock_price", {"ticker": "AAPL"})
  │     ├─ ToolRegistry → MCPTool → MCPClient.call_tool()
  │     └─ ctx.tool_results.append("$198.50")
  │
  ├─ [step 2] ctx.history.append("工具 get_stock_price 返回: $198.50")
  ├─ [step 2] response = await client.chat(...)  # 带工具结果
  │     └─ response = ChatResult(content="AAPL 当前股价为 $198.50...")
  │
  ├─ ctx.assistant_output = "AAPL 当前股价为 $198.50..."
  ├─ memory.on_after_execute(ctx)
  │     └─ 存储本次交互到记忆
  │
  └─ return ctx.assistant_output
```

---

## 6. 与旧架构的对比

| 维度 | v0.3 (Phase 0) | v1.0 DefaultAgent |
|------|----------------|-------------------|
| Agent 定义 | `Agent(llm_cfg)` 一个类 | `DefaultAgent(config, executor, ...)` 模块化组合 |
| LLM 管理 | `build_llm()` 工厂，单模型 | `ModelHub` 多模型管理，惰性创建 Client |
| 模型配置 | 函数参数传递 | 原始 dict 直接注入 |
| 记忆 | 无 | `MemoryModule` 体系：Null / ShortTerm / LongTerm |
| 知识库 | 预留目录 | `KnowledgeModule` 可选注入 |
| 工具 | `agent/tools/` | `ToolRegistry` + MCP Client/Server |
| 执行策略 | 硬编码在 `chat_stream()` | `Executor` 策略模式：Direct / ReAct / Plan |
| 数据传递 | 函数参数 | `Context` 共享对象 |
| 扩展方式 | 改 Agent 类 | 新模块 / 新 Executor / 新 Memory 实现 |
| 测试难度 | 需要 mock LLM | 每个模块可独立测试，mock 接口明确 |

---

## 7. 实现路线

### Phase 1：骨架搭建 ✅ 已完成

```
目标: DefaultAgent + Brain + Context + DirectExecutor 跑通
```

- [x] `BaseClient` + `OpenAIClient` + `AnthropicClient` — LLM 客户端基类及实现
- [x] `ModelHub` + dict 配置 + 惰性创建 Client — 多模型管理
- [x] `Context` / `AgentResult` dataclass — 共享上下文
- [x] `Executor` ABC + `DirectExecutor` — 直调执行器
- [x] `DefaultAgent` 基座（`chat()` / `chat_stream()`）— 轻量编排
- [x] 29 个单元测试覆盖 — 验证所有核心路径

**Phase 1 关键决策：** 简化了文档设计中的 ModelConfig dataclass，直接使用原始 dict；`executor` 改为可选参数（默认 DirectExecutor）；配置解析从 DefaultAgent 移入 ModelHub。

### Phase 2：记忆系统 ✅ 已完成

```
目标: ShortTermMemory + LongTermMemory + PgMemoryStorage 完整实现
```

- [x] `NullMemory` — 关闭记忆时的空实现
- [x] `ShortTermMemory` — 内存级滑动窗口（合并原 InMemory）
- [x] `MemoryStorage` ABC + `PgMemoryStorage` — PostgreSQL 全文搜索实现（非 pgvector）
- [x] `LongTermMemory` — 周期性 + 触发性记忆提取（jieba POS 实体检测 + TF-IDF 关键词）
- [x] `Context` 扩展 — 添加 `memory_context` 字段
- [x] `DirectExecutor` 扩展 — 接入 memory hook
- [x] `DefaultAgent` 扩展 — 添加 `memory` 构造参数
- [x] `agent_memories` 表 DDL — GIN (tsvector english) + pg_trgm 索引
- [x] 31 个单元测试 + 10 个集成测试覆盖

**设计要点：**
- 使用 PG 全文搜索替代 pgvector，无需 embedding 依赖
- 中文搜索：jieba 分词 + ILIKE ANY + pg_trgm 索引（OR 语义）
- 英文搜索：PG FTS（`to_tsvector('english', ...)`，OR 语义）

### Phase 3：工具系统 ✅ 已完成

```
目标: ToolRegistry + MCP Client/Server + ReActExecutor 工具循环
```

- [x] `ToolDef` / `BaseTool` / `FunctionTool` — 内置函数工具基类及实现
- [x] `MCPClient` — MCP 协议客户端（支持 stdio + SSE 传输，轻量 JSON-RPC 实现）
- [x] `MCPTool` — MCP 工具包装器
- [x] `ToolRegistry` — 工具注册中心，统一管理 FunctionTool + MCPTool
- [x] `ReActExecutor` — ReAct 风格推理循环（带 tool_call 解析和循环控制）
- [x] NewsRadar MCP Server — 暴露新闻搜索、热点、情感分析等内部能力（5 个工具）
- [x] `Context` 扩展 — 添加 `tool_calls` / `tool_results` / `history` 字段
- [x] `BaseClient.chat()` 扩展 — 返回 `ChatResult`（含 content + tool_calls）
- [x] `DefaultAgent` 扩展 — 添加 `tools` 构造参数，透传到 Executor
- [x] `OpenAIClient` 支持 tool_calls — 解析 OpenAI API 的 tool_calls 响应
- [x] 42 个单元测试覆盖（工具基类、MCPClient、ToolRegistry、ReActExecutor、MCP Server）
- [x] 验证: Agent 自主决定调工具并基于结果回答

**目录结构：**
- `agent/tools/` — FunctionTool + ToolRegistry（无状态，纯逻辑）
- `agent/mcp/` — MCPClient + MCPTool + NewsRadar MCP Server（有状态连接）

**设计要点：**
- MCP 协议使用轻量 JSON-RPC 2.0 实现，无需 `mcp` Python SDK
- stdio 传输通过 `asyncio.create_subprocess_exec` 子进程管道
- SSE 传输通过 HTTP POST 请求（`httpx`）
- FunctionTool 支持同步/异步函数，错误安全包装
- ReActExecutor 通过 `max_steps` 防止无限循环
- 工具 schema 统一输出为 OpenAI format（兼容 Anthropic function calling）

### Phase 4：知识库 + 高级 Executor

### Phase 4：知识库 + 高级 Executor

```
目标: 完整模块化 Agent
```

- [ ] `RAGKnowledge` — 向量检索知识库
- [ ] `PlanExecutor` — 先规划再执行
- [ ] Executor 热切换（不同 session 用不同策略）
- [ ] 验证: 角色扮演 Agent 回答知识库内事实 + 调工具

---

## 8. 附录：设计决策记录

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 模块通信方式 | 直接调用 / 事件总线 / Context 对象 | **Context 对象** | 简单直观，类型安全，不需要事件系统 |
| Executor 是否可替换 | 固定 / 策略模式 | **策略模式** | 不同场景需要不同执行策略（直调 vs ReAct vs Plan） |
| Memory 触发时机 | Executor 内部 / Hook 回调 | **Hook 回调** | Memory 不需要知道 Executor 的存在 |
| 工具来源 | 内置实现 / MCP | **MCP** | 内置基础工具，MCP 做外部工具桥接 |
| LLM Client 创建 | 启动全量 / 惰性 | **惰性** | 配置可能有很多模型但只用少数 |
| 模型选择权 | Agent 硬编码 / Brain 自动 / Executor 决定 | **Brain 提供路由，Executor 决定** | Executor 知道当前任务复杂度 |
| 知识库与记忆的关系 | 合并 / 分离 | **分离** | 数据来源不同（对话 vs 外部），生命周期不同 |
| 配置类型 | ModelConfig dataclass / 原始 dict | **原始 dict** | 减少一层包装，ModelConfig 只是透传 dict |
| 配置解析位置 | DefaultAgent / ModelHub | **ModelHub** | 配置是本模块的职责 |
| InMemory vs ShortTerm | 分开 / 合并 | **合并为 ShortTermMemory** | 两者都是内存列表，无本质区别 |
