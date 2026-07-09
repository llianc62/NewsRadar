# DefaultAgent v1 架构设计

> **版本**: v1.0-draft  
> **设计日期**: 2026-07-09  
> **状态**: 设计讨论稿，待实现  
> **目标**: 设计一个模块化、可组合的 Agent 基座，支持热插拔的能力模块，替代当前 Phase 0 的简单 Agent 实现

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
| **Shared Context** | 数据总线 | 所有模块之间传递数据的统一管道 |

### 1.2 核心原则

1. **模块独立构造，DI 注入** — 每个模块自己负责自己的初始化，通过构造器注入到 Agent 基座
2. **基座轻量** — `DefaultAgent` 只做编排，不做具体业务逻辑
3. **可选即不依赖** — Memory、Knowledge、Tools 都是可选项，不传就跳过
4. **Executor 是唯一的"流程拥有者"** — 只有 Executor 知道模块之间的调用顺序
5. **模块之间不直接依赖** — 所有跨模块通信通过 Shared Context 进行

---

## 2. 架构总览

```
                         ┌─────────────────────────────────┐
                         │         DefaultAgent             │
                         │   (轻量编排器 + 生命周期管理)      │
                         └──────────┬──────────────────────┘
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         │           │              │              │           │
         ▼           ▼              ▼              ▼           ▼
   ┌─────────┐ ┌────────┐ ┌──────────┐ ┌───────────┐ ┌──────────┐
   │  Brain  │ │ Memory │ │Knowledge │ │   Tools   │ │ Executor │
   │LLM Pool │ │ 可选   │ │  可选    │ │ MCP 桥接  │ │推理循环  │
   └────┬────┘ └────────┘ └──────────┘ └─────┬─────┘ └────┬─────┘
        │                                     │            │
        ▼                                     ▼            ▼
   ┌──────────┐                        ┌──────────┐ ┌──────────┐
   │ModelClient│                       │ MCP      │ │ ReAct /  │
   │Clients   │                        │ Servers  │ │ Plan     │
   └──────────┘                        └──────────┘ └──────────┘
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
| **Tools** | ❌ 可选 | 无（无状态） | 每次调用 | MCP Server |
| **Executor** | ✅ 是 | 无（纯逻辑） | 每次 execute | 无 |
| **Context** | ✅ 是 | 有（临时） | 单次 execute | 无 |

### 2.2 调用流程

```
agent.chat(user_input)
  │
  ├─ 1. Context 初始化（清空上次数据，写入 user_input）
  │
  ├─ 2. Executor.run(Context)
  │     │
  │     ├─ [可选] 2a. Memory.on_before(ctx)    → 检索历史 → 写入 ctx
  │     ├─ [可选] 2b. Knowledge.on_before(ctx) → 检索知识 → 写入 ctx
  │     │
  │     ├─ 2c. Brain.think(ctx)                → LLM 调用
  │     │     └─ LLM 返回: 最终回答 | 工具调用
  │     │
  │     ├─ [可选] 2d. 如果是工具调用:
  │     │     ├─ Tools.execute(ctx)           → 调 MCP
  │     │     └─ 回到 2c (继续推理)
  │     │
  │     ├─ [可选] 2e. Memory.on_after(ctx)    → 存储记忆
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

#### 3.1.2 ModelConfig

```python
@dataclass
class ModelConfig:
    """单个模型配置。"""
    name: str              # 别名，如 "default", "image_model", "cheap", "deepseek-v4-pro"
    protocol: str          # 供应商协议，如 "openai", "anthropic"
    model: str             # 实际模型 ID，如 "gpt-4o", "claude-sonnet-5"
    api_key: str           # API Key
    base_url: str = ""     # 可选自定义端点（代理、自部署用）
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
    
    async def chat(self, model: str, messages, temperature=0.7, top_p=1.0, **kwargs) -> str:
        resp = self._client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, top_p=top_p, **kwargs
        )
        return resp.choices[0].message.content
    
    async def chat_stream(self, model: str, messages, temperature=0.7, top_p=1.0, **kwargs) -> AsyncIterator[str]:
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

#### 3.1.4 ModelHub 设计

```python
_PROVIDER_MAP: dict[str, type[BaseClient]] = {
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
}


class ModelHub:
    """
    LLM Client 管理器。
    
    职责:
    - 管理 ModelConfig 列表
    - 惰性创建 BaseClient 实例
    - 按名称（别名）返回 Client
    
    不负责:
    - ❌ 封装 chat() / chat_stream() 调用
    - ❌ 模型选择逻辑（交给 Executor）
    
    使用方式:
        manager = ModelHub(models=[
            ModelConfig(name="default",      protocol="openai",    model="gpt-4o",        api_key="..."),
            ModelConfig(name="image_model",  protocol="openai",    model="gpt-4o-vision",  api_key="..."),
            ModelConfig(name="cheap",        protocol="openai",    model="gpt-4o-mini",    api_key="..."),
            ModelConfig(name="claude",       protocol="anthropic", model="claude-sonnet-5", api_key="..."),
        ])
        
        # Executor 中调用:
        client = manager.get_default()
        resp = await client.chat(model="gpt-4o", messages=msgs)
        
        # 按别名取:
        client = manager.get_by_name("image_model")
        async for token in client.chat_stream(model="gpt-4o-vision", messages=msgs):
            ...
    """
    
    def __init__(self, models: list[ModelConfig]):
        self._configs = {cfg.name: cfg for cfg in models}
        self._clients: dict[str, BaseClient] = {}  # 惰性填充
    
    def get_default(self) -> BaseClient:
        """获取默认模型 Client（name='default' 的配置）。"""
        return self.get_by_name("default")
    
    def get_by_name(self, name: str) -> BaseClient:
        """
        按别名获取或创建 Client。
        
        name 可以是:
        - 语义别名: "image_model", "cheap", "reasoning"
        - 模型名: "deepseek-v4-pro", "gpt-4o"（前提是配置里有这个 name）
        
        同一 protocol 共用同一个 Client 实例，切换模型在 chat() 参数中指定。
        """
        if name not in self._clients:
            cfg = self._configs.get(name)
            if not cfg:
                raise KeyError(f"Unknown model name: {name}")
            client_cls = _PROVIDER_MAP[cfg.protocol]
            self._clients[name] = client_cls(
                api_key=cfg.api_key,
                base_url=cfg.base_url,
            )
        return self._clients[name]
```

#### 3.1.5 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| Client 创建时机 | 启动时全量 / 惰性 | **惰性** | 配置可能包含 10+ 模型，但实际只用 2-3 个 |
| 模型参数传递 | 构造 / 调用参数 | **调用参数** | 同一 Client 实例可切不同模型，无需重建 |
| Manager 职责 | 管理+封装调用 / 仅管理 | **仅管理** | 只返回 BaseClient，调用方直接调 chat()，职责清晰 |
| 供应商适配 | 继承 + 多态 / if-else | **继承 + 多态** | 新增供应商只需要新加一个子类 |
| Manager 是否封装调用 | 是 / 否 | **否** | 只返回 BaseClient，调用方直接调 chat()，职责清晰 |
| model 放哪 | 构造 / 调用参数 | **调用参数** | 同一 Client 可切不同模型，无需重建 |
| 配置来源 | 全局 config / 构造注入 | **构造注入** | 无全局依赖，测试友好 |

---

### 3.2 Memory — 记忆模块

#### 3.2.1 职责

- 提供记忆存储和检索能力
- 支持多个级别：None / InMemory / ShortTerm / LongTerm
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


class InMemory(MemoryModule):
    """
    内存记忆——session 级别，重启即失。
    适用于：不需要跨会话记忆的场景（单页 AI 助手、一次性对话）。
    """
    
    def __init__(self, max_turns: int = 50):
        self._turns: list[Turn] = []
        self._max_turns = max_turns
    
    async def on_before_execute(self, ctx: Context):
        # 将历史消息写入 ctx 作为上下文
        ctx.memory_context = self._format_history()
    
    async def on_after_execute(self, ctx: Context):
        self._turns.append(Turn(
            user=ctx.user_input,
            assistant=ctx.assistant_output,
        ))
        # 超出最大轮数时丢弃最旧的
        if len(self._turns) > self._max_turns:
            self._turns.pop(0)
    
    def _format_history(self) -> str:
        return "\n".join(
            f"User: {t.user}\nAssistant: {t.assistant}"
            for t in self._turns[-10:]  # 最多给最近 10 轮
        )


class ShortTermMemory(MemoryModule):
    """
    短期记忆——滑动窗口。
    与 InMemory 类似但更关注"最近的 N 条消息"。
    """
    def __init__(self, window_size: int = 20):
        self._window: list[dict] = []
        self._window_size = window_size
    
    async def on_before_execute(self, ctx):
        ctx.memory_context = self._window
    
    async def on_after_execute(self, ctx):
        self._window.append({"role": "user", "content": ctx.user_input})
        self._window.append({"role": "assistant", "content": ctx.assistant_output})
        if len(self._window) > self._window_size * 2:
            self._window = self._window[-(self._window_size * 2):]


class LongTermMemory(MemoryModule):
    """
    长期记忆——RAG 持久化。
    适用于：角色扮演、个人助手等需要跨会话记忆的场景。
    
    记忆的存储策略:
    - 周期性: 每 N 轮对话后自动合并记忆
    - 触发性: 检测到关键信息（用户偏好、决策、承诺）时即时存储
    """
    
    def __init__(self, storage: StorageHandler, extract_interval: int = 5):
        self._storage = storage
        self._extract_interval = extract_interval
        self._turn_count = 0
    
    async def on_before_execute(self, ctx):
        # 语义检索相关记忆
        memories = await self._storage.search(ctx.user_input, top_k=5)
        ctx.memory_context = memories
    
    async def on_after_execute(self, ctx):
        self._turn_count += 1
        # 触发性：检查是否有需要记住的信息
        if self._should_extract(ctx):
            await self._extract_and_store(ctx)
        # 周期性：每 N 轮合并一次
        elif self._turn_count % self._extract_interval == 0:
            await self._batch_merge(ctx)
    
    def _should_extract(self, ctx) -> bool:
        """判断本次对话是否需要触发记忆提取。"""
        # 策略 1: LLM 判断是否包含"可记住"的信息
        # 策略 2: 规则判断（长度、关键词）
        # 策略 3: 总是存（简单但成本高）
        return len(ctx.assistant_output) > 100
```

#### 3.2.3 记忆存储策略

```
周期性存储:
  turn 1  turn 2  ...  turn 5  →  合并记忆  →  存到 PG
  turn 6  turn 7  ...  turn 10 →  合并记忆  →  存到 PG

触发性存储:
  用户: "我叫张三"
    ↓ LLM 判断：这是个人信息，需要记住
  立即提取 → 存到 PG

  用户: "帮我分析一下昨天的新闻"
    ↓ LLM 判断：普通查询，不需要记住
  跳过
```

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

- 管理所有可用的外部工具
- 通过 MCP 协议连接工具服务器
- 提供工具 schema 给 LLM function calling
- 执行工具调用并返回结果

#### 3.4.2 类设计

```python
class ToolModule:
    """
    工具模块——管理所有外部工具。
    
    设计思路:
    - 工具注册在 MCP Server 中定义
    - Agent 只管理"连接了哪些 MCP Server"
    - 每个 MCP Server 提供一组工具
    
    使用方式:
        tools = ToolModule(
            servers=["news-mcp", "finance-mcp"],
            defaults=["calculator", "current_time"],  # 内置工具
        )
    """
    
    def __init__(self, servers: list[str] | None = None, defaults: list[str] | None = None):
        self._mcp_clients: dict[str, MCPClient] = {}
        self._server_names = servers or []
        self._default_tools = defaults or []
    
    async def connect_all(self):
        """启动时连接所有配置的 MCP Server。"""
        for name in self._server_names:
            client = MCPClient(name)
            await client.connect()
            self._mcp_clients[name] = client
    
    def get_tool_schemas(self) -> list[dict]:
        """返回所有工具的 OpenAI function calling schema。"""
        schemas = []
        for client in self._mcp_clients.values():
            schemas.extend(client.get_tool_schemas())
        return schemas
    
    async def execute(self, tool_name: str, args: dict) -> str:
        """执行工具调用。"""
        for client in self._mcp_clients.values():
            if client.has_tool(tool_name):
                return await client.call(tool_name, args)
        raise KeyError(f"Tool '{tool_name}' not found")
```

#### 3.4.3 工具来源分层

```
内置工具（Agent 自带，无需 MCP）:
  ├─ calculator       — 计算器
  ├─ current_time     — 当前时间
  └─ ...              — 其他纯函数工具

外部工具（通过 MCP 连接）:
  ├─ news_search      — MCP Server A
  ├─ stock_price      ─
  ├─ web_fetch        — MCP Server B
  └─ ...              — 任意自定义 MCP Server
```

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
    async def run(self, ctx: Context, brain: Brain, memory: MemoryModule,
                  knowledge: KnowledgeModule, tools: ToolModule) -> str:
        """执行一次完整的推理循环。"""
        ...


class ReActExecutor(Executor):
    """
    ReAct 风格的推理循环。
    
    流程:
    1. 检索记忆 → 注入 Context
    2. 检索知识库 → 注入 Context
    3. 构建 messages（system + context + history + user）
    4. 调 LLM
    5. 解析 LLM 输出:
       - 如果是工具调用 → 执行工具 → 回到 4
       - 如果是最终回答 → 存记忆 → 返回
    6. 最大步数限制（默认 10 步）
    """
    
    def __init__(self, max_steps: int = 10, max_retries: int = 3):
        self.max_steps = max_steps
        self.max_retries = max_retries
    
    async def run(self, ctx, brain, memory, knowledge, tools) -> str:
        client = brain.get_default()  # 获取 BaseClient
        for step in range(self.max_steps):
            # Phase 1: 检索上下文
            await memory.on_before_execute(ctx)
            await knowledge.on_before_execute(ctx)
            
            # Phase 2: 构建 prompt 并调用 LLM
            messages = self._build_messages(ctx)
            response = await client.chat(
                model=ctx.model_name,
                messages=messages,
                tools=tools.get_tool_schemas() if tools else None,
            )
            
            # Phase 3: 解析响应
            if self._is_tool_call(response):
                ctx.add_tool_call(response.tool_call)
                tool_result = await tools.execute(
                    response.tool_call.name,
                    response.tool_call.args,
                )
                ctx.add_tool_result(tool_result)
                continue  # 继续循环
            
            # Phase 4: 最终回答
            ctx.assistant_output = response.content
            await memory.on_after_execute(ctx)
            return response.content
        
        # 超步数限制，返回已生成的内容
        return ctx.assistant_output or "我在处理过程中遇到了限制，请简化您的请求。"
    
    def _build_messages(self, ctx: Context) -> list[dict]:
        messages = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        if ctx.memory_context:
            messages.append({"role": "system", "content": f"## 记忆上下文\n{ctx.memory_context}"})
        if ctx.knowledge_context:
            messages.append({"role": "system", "content": f"## 知识库\n{ctx.knowledge_context}"})
        if ctx.history:
            messages.extend(ctx.history)
        messages.append({"role": "user", "content": ctx.user_input})
        return messages


class DirectExecutor(Executor):
    """
    简单直调执行器——没有 ReAct 循环，没有工具调用。
    适用于：简单问答、分类、不需要工具的纯文本场景。
    """
    async def run(self, ctx, brain, memory, knowledge, tools) -> str:
        await memory.on_before_execute(ctx)
        await knowledge.on_before_execute(ctx)
        
        messages = self._build_messages(ctx)
        client = brain.get_default()
        response = await client.chat(model=ctx.model_name, messages=messages)
        
        ctx.assistant_output = response
        await memory.on_after_execute(ctx)
        return response


class PlanExecutor(Executor):
    """
    计划式执行器——先规划再执行。
    适用于：复杂任务，需要先分解子任务再逐步执行。
    Phase 4+ 预留。
    """
    async def run(self, ctx, brain, memory, knowledge, tools) -> str:
        # 1. LLM 生成计划
        # 2. 按计划逐步执行（可能涉及多次工具调用）
        # 3. 汇总结果
        # 4. 返回最终回答
        raise NotImplementedError("PlanExecutor: Phase 4+")
```

#### 3.5.3 Executor 的选择策略

```python
# 简单场景 → DirectExecutor
agent = DefaultAgent(
    executor=DirectExecutor(),
)

# 需要工具调用 → ReActExecutor
agent = DefaultAgent(
    model_configs=[ModelConfig(...)],
    executor=ReActExecutor(max_steps=15),
    tools=ToolModule(servers=["news"]),
)

# 复杂任务 → PlanExecutor (future)
agent = DefaultAgent(
    model_configs=[ModelConfig(...)],
    executor=PlanExecutor(),
    tools=ToolModule(servers=["news", "finance", "research"]),
    memory=LongTermMemory(storage=pg),
    knowledge=RAGKnowledge(name="research_docs", ...),
)
```

---

### 3.6 Shared Context — 数据管道

#### 3.6.1 职责

- 单次 `execute()` 调用内的数据传递
- 所有模块读写同一个 Context 对象
- execute 结束后释放

#### 3.6.2 类设计

```python
@dataclass
class Context:
    """单次 execute 调用的共享上下文。"""
    
    # 输入
    user_input: str
    session_id: str
    system_prompt: str = ""
    model_name: str = "default"  # 使用的模型 name，传给 brain.get_by_name()
    
    # 跨 Agent 工作流（可选）
    session: "Session | None" = None  # 外部 Session 引用，用于多 Agent 共享数据
    
    # 模块写入（由 Memory/Knowledge 在 on_before_execute 中填充）
    memory_context: Any = None       # 记忆检索结果
    knowledge_context: Any = None    # 知识库检索结果
    
    # 执行过程
    history: list[dict] = field(default_factory=list)   # 历史消息
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    tool_results: list[ToolResultRecord] = field(default_factory=list)
    
    # 输出
    assistant_output: str = ""
    
    # 元数据
    step_count: int = 0
    model_used: str = ""
    total_tokens: int = 0
    
    def add_tool_call(self, name: str, args: dict):
        self.tool_calls.append(ToolCallRecord(name=name, args=args))
    
    def add_tool_result(self, result: str):
        self.tool_results.append(ToolResultRecord(result=result))
```

---

## 4. DefaultAgent 基座

### 4.1 类设计

```python
class DefaultAgent:
    """
    模块化 Agent 基座。
    
    ModelHub 在内部自动初始化，可通过 model_configs 传入配置。
    不传任何参数时，会读取默认配置文件（如 models.yaml）。
    
    使用方式:
        # 最小构造——自动读取配置
        agent = DefaultAgent(
            executor=DirectExecutor(),
        )
        
        # 完整构造
        agent = DefaultAgent(
            model_configs=[                           # 可选，内部创建 ModelHub
                ModelConfig(name="default", protocol="openai", model="gpt-4o", api_key="..."),
            ],
            executor=ReActExecutor(max_steps=10),
            memory=InMemory(max_turns=50),            # 可选
            tools=ToolModule(servers=["news-mcp"]),   # 可选
            system_prompt="你是一个新闻分析助手。",
        )
        
        result = await agent.chat("今天的头条是什么？")
    """
    
    def __init__(
        self,
        executor: Executor,
        model_configs: list[ModelConfig] | None = None,
        memory: MemoryModule | None = None,
        knowledge: KnowledgeModule | None = None,
        tools: ToolModule | None = None,
        system_prompt: str = "",
    ):
        self.brain = ModelHub(models=model_configs or self._load_default_configs())
        self.executor = executor
        self.memory = memory or InMemory()  # None → InMemory
        self.knowledge = knowledge or NullKnowledge()
        self.tools = tools
        self.system_prompt = system_prompt
    
    @staticmethod
    def _load_default_configs() -> list[ModelConfig]:
        """从配置文件（models.yaml）加载默认模型列表。
        配置文件不存在时返回空列表，由 ModelHub 抛清晰错误。"""
        ...
    
    async def chat(
        self,
        user_input: str,
        session_id: str = "",
        model_name: str = "",
        session: "Session | None" = None,
    ) -> AgentResult:
        """
        执行一次完整的 Agent 调用。
        
        Args:
            user_input: 用户输入
            session_id: 会话 ID（用于记忆检索）
            model_name: 指定模型 name（如 "cheap", "image_model"），
                        空字符串则用 brain.get_default()
            session: 可选的外部 Session 对象，用于多 Agent 工作流中
                    跨 Agent 共享数据。详见「多 Agent 工作流」章节。
        
        Returns:
            AgentResult: 包含回答 + 元数据
        """
        ctx = Context(
            user_input=user_input,
            session_id=session_id,
            system_prompt=self.system_prompt,
            model_name=model_name or "default",
            session=session,  # None 时忽略
        )
        
        result_text = await self.executor.run(
            ctx=ctx,
            brain=self.brain,
            memory=self.memory,
            knowledge=self.knowledge,
            tools=self.tools,
        )
        
        return AgentResult(
            content=result_text,
            model_used=ctx.model_used,
            total_tokens=ctx.total_tokens,
            tool_calls=ctx.tool_calls,
        )
    
    async def chat_stream(
        self,
        user_input: str,
        session_id: str = "",
        model_name: str = "",
        session: "Session | None" = None,
    ) -> AsyncIterator[str]:
        """
        流式版本——逐 token 返回 LLM 输出。
        """
        ctx = Context(
            user_input=user_input,
            session_id=session_id,
            system_prompt=self.system_prompt,
            model_name=model_name or "default",
            session=session,
        )
        
        async for token in self.executor.run_stream(
            ctx=ctx,
            brain=self.brain,
            memory=self.memory,
            knowledge=self.knowledge,
            tools=self.tools,
        ):
            yield token
```

### 4.2 构造示例

```python
# ──────────────────────────────────────────────
# 场景 1: 简单聊天机器人（无记忆、无工具、直调）
# ──────────────────────────────────────────────
quick_chat = DefaultAgent(
    model_configs=[
        ModelConfig(name="default", protocol="openai", model="gpt-4o-mini", api_key="..."),
    ],
    executor=DirectExecutor(),
)

# ──────────────────────────────────────────────
# 场景 2: 角色扮演助手（有长期记忆、有知识库）
# ──────────────────────────────────────────────
role_agent = DefaultAgent(
    model_configs=[
        ModelConfig(name="default",      protocol="openai",    model="gpt-4o",       api_key="..."),
        ModelConfig(name="cheap",        protocol="openai",    model="gpt-4o-mini",   api_key="..."),
        ModelConfig(name="claude",       protocol="anthropic", model="claude-sonnet-5", api_key="..."),
    ],
    executor=ReActExecutor(max_steps=15),
    memory=LongTermMemory(storage=pg_vector, extract_interval=5),
    knowledge=RAGKnowledge(name="character_lore", embedding=emb, vector_store=pg_vector),
    system_prompt="你是巴菲特的投资助手，用价值投资理念回答问题。",
)

# ──────────────────────────────────────────────
# 场景 3: 工具型 Agent（有工具、有短期记忆）
# ──────────────────────────────────────────────
tool_agent = DefaultAgent(
    model_configs=[
        ModelConfig(name="default", protocol="openai", model="gpt-4o", api_key="..."),
    ],
    executor=ReActExecutor(max_steps=20),
    memory=ShortTermMemory(window_size=10),
    tools=ToolModule(servers=["news-mcp", "finance-mcp", "web-mcp"]),
)

# ──────────────────────────────────────────────
# 场景 4: 单页 AI 助手提示框（无记忆、无工具，最小构造）
# ──────────────────────────────────────────────
page_assistant = DefaultAgent(
    executor=DirectExecutor(),
    # 不传 model_configs → 读默认配置文件
    # 不传 memory → InMemory，不传 tools → 无工具
)
```

---

## 5. 模块间通信协议

### 5.1 核心规则

1. **模块之间不直接调用** — 所有通信通过 Context 进行
2. **Executor 是唯一的编排者** — 它决定什么时候调哪个模块
3. **模块只暴露 hook 方法** — `on_before_execute` / `on_after_execute`
4. **Context 是临时对象** — 每次 execute 创建，结束后丢弃

### 5.2 数据流图

```
Executor.run(ctx)
  │
  ├─ ctx.user_input = "帮我查一下 AAPL 的股价"
  │
  ├─ memory.on_before_execute(ctx)
  │     └─ ctx.memory_context = "用户昨天问过 AAPL 的财报..."
  │
  ├─ knowledge.on_before_execute(ctx)
  │     └─ ctx.knowledge_context = "AAPL 是 Apple Inc. 的股票代码..."
  │
  ├─ client = brain.get_default()          # ModelHub 返回 BaseClient
  ├─ response = await client.chat(
  │     model=ctx.model_name,
  │     messages=[system, memory_context, knowledge_context, user],
  │     tools=tools.get_tool_schemas(),
  │   )
  │     └─ response = ToolCall("get_stock_price", {"ticker": "AAPL"})
  │
  ├─ ctx.add_tool_call("get_stock_price", {"ticker": "AAPL"})
  ├─ tools.execute("get_stock_price", {"ticker": "AAPL"})
  │     └─ ctx.add_tool_result("$198.50")
  │
  ├─ response = await client.chat(         # 第二轮：带工具结果
  │     model=ctx.model_name,
  │     messages=[..., tool_result],
  │   )
  │     └─ response = "AAPL 当前股价为 $198.50..."
  │
  ├─ ctx.assistant_output = "AAPL 当前股价为 $198.50..."
  ├─ memory.on_after_execute(ctx)
  │     └─ 存储本次交互到记忆
  │
  └─ return ctx.assistant_output
```

---

### 5.3 多 Agent 工作流的数据共享

#### 5.3.1 问题

`Context` 是单次 `chat()` 调用内部创建、用完即弃的临时对象。当多个 Agent 协同工作时（如 A 搜索 → B 分析 → C 汇总），Agent 之间需要共享中间结果。

#### 5.3.2 分层设计

```
Session（外部，跨 Agent，跨调用持久）
  ├── shared_data: dict        ← Agent 之间共享的中间结果
  ├── conversation_history     ← 全局对话流
  └── agent_outputs: dict      ← 每个 Agent 的最终输出
        │
        ├── AgentA.chat(...) → 内部创建临时 Context
        │     └── ctx.session.shared_data["search_results"] = ...
        │
        ├── AgentB.chat(..., session=session) → 内部创建临时 Context
        │     └── 读取 ctx.session.shared_data["search_results"]
        │
        └── AgentC.chat(..., session=session)
              └── 汇总 A + B 的结果
```

#### 5.3.3 设计原则

| 原则 | 说明 |
|------|------|
| **Context 内部独占** | 单次 `chat()` 内的模块通信走 Context，外部不可见 |
| **Session 外部共享** | 跨 Agent 共享数据走 Session，Agent 之间不直接调用 |
| **Session 可选** | 简单场景不传 session，不影响单 Agent 使用 |
| **Phase 4 实现** | Session 的具体实现留到多 Agent 工作流阶段 |

```python
@dataclass
class Session:
    """跨 Agent 工作流的共享上下文。
    
    Phase 4 完整实现，当前只预留接口。
    """
    shared_data: dict = field(default_factory=dict)
    agent_outputs: dict = field(default_factory=dict)
```

---

## 6. 与现有架构的对比

| 维度 | 当前 v0.3 (Phase 0) | v1.0 DefaultAgent |
|------|-------------------|-------------------|
| Agent 定义 | `Agent(llm_cfg)` 一个类 | `DefaultAgent(executor, model_configs, ...)` 模块化组合 |
| LLM 管理 | `build_llm()` 工厂，单模型 | `ModelHub` 多模型管理，惰性创建 Client |
| 记忆 | 无 | `MemoryModule` 体系：None / InMemory / ShortTerm / LongTerm |
| 知识库 | 预留目录 | `KnowledgeModule` 可选注入 |
| 工具 | 预留目录 | `ToolModule` MCP 桥接 |
| 执行策略 | 硬编码在 `chat_stream()` | `Executor` 策略模式：Direct / ReAct / Plan |
| 数据传递 | 函数参数 | `Context` 共享对象 |
| 扩展方式 | 改 Agent 类 | 新模块 / 新 Executor / 新 Memory 实现 |
| 测试难度 | 需要 mock LLM | 每个模块可独立测试，mock 接口明确 |

---

## 7. 实现路线

### Phase 1：骨架搭建

```
目标: DefaultAgent + Brain + Context + DirectExecutor 跑通
```

- [ ] `BaseClient` + `OpenAIClient` + `AnthropicClient`
- [ ] `ModelHub` + `ModelConfig` + 惰性创建
- [ ] `Context` dataclass
- [ ] `Executor` ABC + `DirectExecutor`
- [ ] `MemoryModule` ABC + `InMemory`
- [ ] `DefaultAgent` 基座（`chat()` / `chat_stream()`）
- [ ] 验证: 替换现有 Phase 0 Agent，功能不变

### Phase 2：记忆系统

```
目标: InMemory / ShortTerm / LongTerm 完整实现
```

- [ ] `ShortTermMemory` — 滑动窗口
- [ ] `LongTermMemory` — pgvector 存储 + 检索
- [ ] 记忆提取策略（周期性 + 触发性）
- [ ] `ReActExecutor` 集成记忆 hook
- [ ] 验证: 跨会话对话，Agent 能记住之前的信息

### Phase 3：工具系统

```
目标: MCP 桥接 + ReActExecutor 工具循环
```

- [ ] `ToolModule` — MCP 客户端管理
- [ ] `ReActExecutor` — 工具调用循环
- [ ] WebSocket 推送 `tool_call` / `tool_result` 事件
- [ ] 验证: Agent 自主决定调工具并基于结果回答

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
