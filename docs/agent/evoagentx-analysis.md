# EvoAgentX 深度拆解分析

> 基于源码阅读和架构分析，完整拆解 EvoAgentX 的"深度角色系统"实现。

---

## 目录

1. [项目概览](#1-项目概览)
2. [整体架构](#2-整体架构)
3. [Agent 角色定义系统](#3-agent-角色定义系统)
4. [提示词工程系统](#4-提示词工程系统)
5. [记忆系统](#5-记忆系统)
6. [RAG 引擎](#6-rag-引擎)
7. [工具系统](#7-工具系统)
8. [多角色辩论框架](#8-多角色辩论框架)
9. [存储后端](#9-存储后端)
10. [配置与加载](#10-配置与加载)
11. [完整调用流程](#11-完整调用流程)
12. [总结：EvoAgentX 能教给 NewsRadar 什么](#12-总结)

---

## 1. 项目概览

| 项目 | 信息 |
|------|------|
| **源码路径** | `/home/llianc62/ws/EvoAgentX/` |
| **包名** | `evoagentx` |
| **核心模块数量** | 10 个包目录，~170+ 个 Python 文件 |
| **设计哲学** | 声明式角色定义 + 可插拔组件（记忆/工具/RAG/存储） |
| **定位** | 通用 Agent 框架（不只是对话，包含工作流、辩论、优化、评估） |
| **依赖** | Pydantic, LlamaIndex, FastMCP, 各种 LLM SDK |

### 核心包结构

```
evoagentx/
├── agents/          # Agent 类体系（Agent, CustomizeAgent, MemoryAgent, ActionAgent）
├── actions/         # Action 基类和实现（Action, ContextExtraction, CustomizeAction）
├── memory/          # 记忆系统（ShortTermMemory, LongTermMemory, MemoryManager）
├── rag/             # RAG 引擎（Chunkers, Embeddings, Indexings, Retrievers）
├── tools/           # 工具系统（Tool/Toolkit, MCP, Search, Browser, DB...）
├── prompts/         # 提示词模板系统（StringTemplate, ChatTemplate）
├── frameworks/      # 高级框架（MultiAgentDebate）
├── workflow/        # 工作流引擎（ActionGraph, Workflow）
├── models/          # LLM 模型封装（OpenAI, OpenRouter, LiteLLM...）
├── storages/        # 存储后端（SQLite, FAISS, Neo4j...）
├── core/            # 核心基础设施（BaseModule, Message, Registry...）
├── config.py        # YAML 配置加载
└── optimizers/      # 提示词优化器（AFLOW, MiPro, TextGrad...）
```

---

## 2. 整体架构

EvoAgentX 采用**分层组合**架构：

```
┌─────────────────────────────────────────────────────────┐
│                   高级框架                                │
│   MultiAgentDebate │ Workflow │ Evaluators              │
├─────────────────────────────────────────────────────────┤
│                    Agent 层                               │
│   CustomizeAgent │ MemoryAgent │ ActionAgent │ Agent    │
├─────────────────────────────────────────────────────────┤
│                    Action 层                              │
│   Action │ CustomizeAction │ ContextExtraction           │
├─────────────────────────────────────────────────────────┤
│                   能力层                                  │
│   Memory │ RAG │ Tools │ Prompts │ Models │ Storage     │
├─────────────────────────────────────────────────────────┤
│                   核心基础设施                             │
│   BaseModule │ Message │ Registry │ Parser │ Metadata   │
└─────────────────────────────────────────────────────────┘
```

**关键设计原则：**

1. **Action 驱动** — Agent 不直接执行逻辑，而是包含多个 `Action`。调用时按名称选择 Action
2. **声明式配置** — `CustomizeAgent` 通过 inputs/outputs/prompt 声明式定义，无需写代码
3. **可插拔记忆** — 短期记忆（滑动窗口）+ 长期记忆（RAG 语义检索）可选
4. **组合优于继承** — `Toolkit` 组合 `Tool`；`Agent` 组合 `Action`、`Memory`、`Tools`
5. **注册表机制** — `MODULE_REGISTRY` 统一管理动态生成的类，支持序列化/反序列化

---

## 3. Agent 角色定义系统

这是**整个框架最核心的部分**。不同于 ai-hedge-fund 的硬编码函数式角色，EvoAgentX 提供了 4 种 Agent 类型：

### 3.1 类体系

```
BaseModule (Pydantic)
└── Agent                          # 基础 Agent（最通用）
    ├── CustomizeAgent             # 声明式角色（核心！）
    ├── MemoryAgent                # 带长期记忆的角色
    └── ActionAgent                # 纯函数角色（无 LLM）
```

### 3.2 Agent 基类（`agents/agent.py`）

**核心字段：**
- `name`, `description` — 角色名称和描述
- `llm_config`, `llm` — LLM 配置和实例
- `system_prompt` — 系统提示词
- `short_term_memory: ShortTermMemory` — 短期记忆（默认）
- `long_term_memory: LongTermMemory` — 长期记忆（可选）
- `long_term_memory_manager: MemoryManager` — 记忆管理器（可选）
- `actions: List[Action]` — 该 Agent 拥有的 Action 列表
- `is_human: bool` — 标记是否为人类（跳过 LLM 初始化）
- `storage_handler: StorageHandler` — 存储处理器

**关键方法：**

| 方法 | 功能 |
|------|------|
| `init_module()` | 初始化 LLM、记忆系统、Action 映射、ContextExtraction |
| `execute(action_name, ...)` | 同步执行：prepare → action.execute → 构建输出消息 |
| `async_execute(action_name, ...)` | 异步执行 |
| `get_action_inputs(action)` | 通过 ContextExtraction 从对话历史提取 Action 输入 |
| `add_action(action)` | 注册新的 Action |
| `save_module(path)` / `from_dict(data)` | 序列化/反序列化 |

**执行流程：**
```
1. Agent.__call__() → 检测事件循环 → 分派 execute/async_execute
2. _prepare_execution() → 将输入写入短期记忆
   → 若没提供 action_input_data，用 ContextExtraction 从对话推导
3. action.execute(llm, inputs, sys_msg) → 执行 Action
4. _create_output_message() → 包装结果为 Message，存入短期记忆
```

### 3.3 CustomizeAgent（`agents/customize_agent.py`）⭐ 核心角色定义方式

**这是你真正需要关注的类**——它实现了"声明式角色定义"。

#### 构造参数

```python
CustomizeAgent(
    name="MacroAnalyst",           # 角色名称
    description="宏观经济分析师",   # 角色描述
    # 方式 A：原始提示词（字符串）
    prompt="分析以下经济数据对{market}的影响...",
    # 方式 B：提示词模板（更结构化，推荐）
    prompt_template=StringTemplate(
        instruction="分析以下经济数据对{market}的影响",
        context="你是资深宏观经济分析师...",
        constraints=["只基于提供的经济数据分析", "给出明确的信号判断"],
        demonstrations=[...],  # Few-shot 示例
    ),
    # 输入输出定义
    inputs=[                      # 声明式输入
        {"name": "market", "type": "string", "description": "目标市场"},
        {"name": "data", "type": "string", "description": "经济数据"},
    ],
    outputs=[                     # 声明式输出
        {"name": "signal", "type": "string", "description": "判断信号"},
        {"name": "confidence", "type": "number", "description": "置信度"},
    ],
    # 工具
    tools=[SearchToolkit(), DataBaseToolkit()],
    # 解析模式（关键！）
    parse_mode="json",  # json | xml | title | str | custom
    # LLM 配置
    llm_config=llm_config,
    # 其他
    system_prompt="你是资深分析师...",
    max_steps=20,
    max_tool_call_concurrency=5,
)
```

#### 输入输出类型系统

`Parameter` 对象支持以下类型：

| 类型 | 说明 | JSON Schema 对应 |
|------|------|-----------------|
| `string` | 字符串 | `{"type": "string"}` |
| `number` | 浮点数 | `{"type": "number"}` |
| `integer` | 整数 | `{"type": "integer"}` |
| `boolean` | 布尔 | `{"type": "boolean"}` |
| `object` | 对象 | `{"type": "object"}`（自动切换 parse_mode="json"） |
| `array` | 数组 | `{"type": "array"}`（自动切换 parse_mode="json"） |

每个参数还可以带 `json_schema` 字段，支持嵌套结构。

#### 解析模式（parse_mode）

| 模式 | 说明 | 适用场景 |
|------|------|---------|
| `"json"` | 自动解析 JSON 输出 | 结构化数据提取 |
| `"xml"` | 解析 XML 标签 | 辩论中的多字段输出 |
| `"title"` | 按标题分割输出 | 自由格式但有章节 |
| `"str"` | 原始字符串 | 简单问答 |
| `"custom"` | 自定义解析函数 | 需要特殊解析逻辑 |

**自动修正逻辑**：当 outputs 包含 `object`/`array` 类型或 `json_schema` 时，且使用 `prompt_template`，自动将 parse_mode 修正为 `"json"`。

#### 内部实现机制

1. `__init__` 验证参数 → `validate_data()` 规范化
2. 调用 `create_customize_action()` → 动态创建 `CustomizeAction`
   - 使用 `pydantic.create_model` 动态生成 `ActionInput` / `ActionOutput` 子类
   - 生成 JSON Schema 注入到模型的 `model_config`
3. `super().__init__(actions=[customize_action])` → 注册 Action
4. 调用时：`agent(inputs={"market": "US", "data": "..."})` → 委托给 `CustomizeAction`

#### 序列化/反序列化

```python
# 保存角色配置到 JSON
agent.save_module("agents/macro_analyst.json")

# 从 JSON 加载角色
agent = CustomizeAgent.from_dict(agent_data, llm_config=config)
```

保存的内容包含：`class_name`, `name`, `description`, `prompt`, `inputs`, `outputs`, `parse_mode`, `tool_names`, `max_steps` 等。

### 3.4 ActionAgent（`agents/action_agent.py`）

纯函数式 Agent，无 LLM 调用，适合作为确定性计算节点：

```python
def calculate_heat_score(rank: int, total: int) -> float:
    return (1 - rank / total) * 100

heat_agent = ActionAgent(
    name="HeatCalculator",
    inputs=[{"name": "rank", "type": "number", ...}],
    outputs=[{"name": "score", "type": "number", ...}],
    execute_func=calculate_heat_score,
)
```

### 3.5 MemoryAgent（`agents/long_term_memory_agent.py`）

带长期记忆的 Agent，自动做检索增强生成：

```python
memory_agent = MemoryAgent(
    name="ResearchAgent",
    storage_handler=storage_handler,
    rag_config=rag_config,
    llm_config=config,
)
memory_agent.chat("What did we discuss about AI regulation last time?")
```

---

## 4. 提示词工程系统

### 4.1 PromptTemplate 类体系

```
PromptTemplate(BaseModule)
└── StringTemplate(PromptTemplate)      # → 输出纯文本
    └── ChatTemplate(StringTemplate)    # → 输出 Chat 消息列表
        └── MiproPromptTemplate(ChatTemplate)  # DSPy 兼容
```

### 4.2 PromptTemplate 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `instruction` | `str` | 核心指令 |
| `context` | `Optional[str]` | 背景知识/上下文 |
| `constraints` | `Optional[Union[List[str], str]]` | 约束条件列表 |
| `tools` | `Optional[List[Union[Tool, Toolkit]]]` | 可用工具 |
| `demonstrations` | `Optional[List[dict]]` | Few-shot 示例 |
| `history` | `Optional[List[Any]]` | 对话历史 |

### 4.3 StringTemplate 输出格式

```python
template = StringTemplate(
    instruction="分析{market}市场数据",
    context="你是资深分析师，专注于宏观经济研究",
    constraints=[
        "只基于提供的经济数据分析",
        "输出 bullish/bearish/neutral 判断",
    ],
    demonstrations=[  # Few-shot 示例
        {"market": "US", "data": "...", "signal": "bearish", "reason": "..."},
    ]
)

# 格式化输出（纯文本）
prompt = template.format(
    system_prompt="额外系统提示",
    values={"market": "US", "data": "..."},
    inputs_format=inputs_parser,
    outputs_format=outputs_parser,
    parse_mode="json",
    tools=[my_toolkit],
)
```

### 4.4 ChatTemplate 输出格式

```python
chat_template = ChatTemplate(instruction="...", constraints=[...])

# 格式化输出（Chat 消息列表）
messages = chat_template.format(
    system_prompt="...",
    values={"market": "US"},
    inputs_format=inputs_parser,
    outputs_format=outputs_parser,
    parse_mode="json",
)
# → [
#     {"role": "system", "content": "系统消息..."},
#     {"role": "user", "content": "用户输入..."},
#     {"role": "assistant", "content": "示例输出..."},  # demonstrations
#     {"role": "user", "content": "当前输入..."},
# ]
```

### 4.5 输出格式渲染

根据 `parse_mode` 自动生成输出格式说明：

- **json 模式**：生成 JSON Schema + 示例 JSON
- **xml 模式**：生成 XML 标签模板
- **title 模式**：生成 `## {title}` 章节模板

### 4.6 工具调用提示词

`prompts/tool_calling.py` 定义了完整的工具调用协议：

```
<tool_call>
[
    {"function_name": "web_search", "function_args": {"query": "..."}},
    ...
]
</tool_call>

<tool_result>
{tool_results}
</tool_result>
```

工具调用提示词通过 `PromptTemplate.render_tools()` 自动注入到提示词中。

---

## 5. 记忆系统

### 5.1 三层记忆架构

```
MemoryManager（LLM 决定何时 add/update/delete）
    │
    ▼
LongTermMemory（RAGEngine 向量+关键词索引）
    │
    ▼
BaseMemory（内存消息列表 + action/wf_goal 索引）
```

### 5.2 BaseMemory（`memory/memory.py`）

- 纯内存存储
- 数据结构：`messages: List[Message]` + 两个索引 `_by_action`、`_by_wf_goal`
- 检索方式：按时间顺序、按 Action 名称、按工作流目标
- 能力有限，主要用于消息历史跟踪

### 5.3 ShortTermMemory（`memory/memory.py`）

- 使用 `collections.deque(maxlen=N)` 实现滑动窗口
- 默认容量 5 条消息
- 纯内存，无持久化
- 用于最近的对话上下文

### 5.4 LongTermMemory（`memory/long_term_memory.py`）⭐ 核心

**存储方式**：RAG 引擎 + 内存双重存储

**数据流：**
```
Message → _create_memory_chunk() → Chunk → Corpus → RAGEngine.add()
                                                            ↓
                                                    向量索引 + 关键词索引
```

**关键方法：**

| 方法 | 功能 |
|------|------|
| `add(messages)` | 添加消息 → SHA-256 去重 → 写入 RAG 索引 |
| `get(memory_ids)` | 按 ID 检索 |
| `search(query)` | 语义搜索 → 返回 `(Message, memory_id)` |
| `delete(memory_ids)` | 删除并清理索引 |
| `update(updates)` | 更新（删除旧 + 添加新） |
| `save(path)` / `load(path)` | 持久化到文件/数据库 |
| `clear()` | 清空所有 |

**去重机制**：基于 SHA-256(content_hash)，避免重复索引相同记忆。

### 5.5 MemoryManager（`memory/memory_manager.py`）⭐ 核心

**LLM 管理的记忆系统**——这是区别于简单 RAG 的关键特性。

```python
memory_manager = MemoryManager(
    memory=long_term_memory,
    llm=llm,
    use_llm_management=True,  # LLM 决定记忆操作
)
```

**`handle_memory()` 统一入口：**

```python
result = await memory_manager.handle_memory(
    action="add",      # add | search | get | update | delete | clear | save | load | create_message
    user_prompt="...", # 搜索时使用
    data=...,          # 要添加/更新/删除的数据
    top_k=5,
    metadata_filters={"agent": "user"},
)
```

**LLM 管理流程（以 add 为例）：**

1. 用户请求添加记忆
2. 构造 `MANAGER_PROMPT`，包含输入数据和已有关联记忆
3. LLM 决定：哪些真正需要添加？哪些应该跳过？
4. 只执行 LLM 批准的添加操作

**`create_conversation_message()` 构建上下文：**

```python
msg = await memory_manager.create_conversation_message(
    user_prompt="What did we discuss about AI?",
    conversation_id="conv_123",
    top_k=10,
)
# 返回 Message(content="User Prompt: ...\nConversation History: ...")
```

### 5.6 ContextManager（`memory/context_manager.py`）

纯消息列表组装器，用于构建 OpenAI 风格的 Chat 消息列表：

```python
context = ContextManager(llm=llm, system_prompt="...")
context.add_prompt_template(prompt_template, values=inputs)
context.add_user_prompt(user_message)
context.add_llm_response(response, tool_calls=tool_calls)
context.add_tool_results(tool_results)

# 最终消息列表
messages = context.context
```

支持两种模式：
- **`native`**：LLM 原生支持工具调用（OpenAI 格式）
- **`default`**：通过提示词注入工具描述，解析 `<tool_call>` 标签

---

## 6. RAG 引擎

### 6.1 完整流水线

```
【写入流水线】
文件/文本 → Reader → Chunker → Embedding → Index → Store

【读取流水线】
Query → [HyDE 变换] → Retriever → Postprocessor → Result
```

### 6.2 架构

```
RAGEngine（编排器）
├── Reader           — LLamaIndexReader / MultimodalReader
├── Chunker          — SimpleChunker / SemanticChunker / HierarchicalChunker
├── Embedding        — OpenAI / HuggingFace / Ollama / Voyage / Azure
├── Indexing         — VectorIndex / GraphIndex / [Summary / Tree - 未实现]
├── Retriever        — VectorRetriever / GraphRetriever
└── Postprocessor    — SimpleReranker（相似度 + 关键词过滤）
```

### 6.3 RAGConfig 配置

```python
rag_config = RAGConfig(
    modality="text",  # text | multimodal
    num_workers=4,
    reader=ReaderConfig(recursive=True, exclude_hidden=True),
    chunker=ChunkerConfig(
        strategy="simple",    # simple | semantic | hierarchical
        chunk_size=1024,
        chunk_overlap=20,
    ),
    embedding=EmbeddingConfig(
        provider="openai",    # openai | huggingface | ollama | voyage
        model_name="text-embedding-ada-002",
        dimensions=1536,
    ),
    index=IndexConfig(index_type="vector"),  # vector | graph
    retrieval=RetrievalConfig(
        retrivel_type="vector",
        postprocessor_type="simple",
        top_k=5,
        similarity_cutoff=0.7,
    ),
)
```

### 6.4 分块策略

| 策略 | 类 | 说明 |
|------|-----|------|
| Simple | `SimpleChunker` | 固定大小分块（`chunk_size=1024`，`overlap=20`） |
| Semantic | `SemanticChunker` | 按语义相似度分割（`similarity_threshold=0.7`） |
| Hierarchical | `HierarchicalChunker` | 多层次分块（如 [2048, 512, 128]） |

### 6.5 Embedding 提供商

| 提供商 | 类 | 说明 |
|--------|-----|------|
| OpenAI | `OpenAIEmbeddingWrapper` | text-embedding-ada-002 / 3-small / 3-large |
| HuggingFace | `HuggingFaceEmbeddingWrapper` | 本地模型，如 bge-small-en-v1.5 |
| Ollama | `OllamaEmbeddingWrapper` | 本地 Ollama 服务 |
| Voyage | `VoyageEmbeddingWrapper` | Voyage AI API |
| Azure | `AzureOpenAIEmbeddingWrapper` | Azure OpenAI 服务 |

### 6.6 索引类型

| 类型 | 类 | 状态 |
|------|-----|------|
| 向量索引 | `VectorIndexing` | ✅ 已实现（包装 LlamaIndex VectorStoreIndex） |
| 图索引 | `GraphIndexing` | ✅ 已实现（包装 LlamaIndex PropertyGraphIndex） |
| 摘要索引 | `SummaryIndexing` | ❌ 未实现（存根） |
| 树索引 | `TreeIndexing` | ❌ 未实现（存根） |

### 6.7 图索引的实体提取

`GraphIndexing` 使用 `BasicGraphExtractLLM`（LLM 提取知识图谱实体）+ `ImplicitPathExtractor`，将文本中的实体和关系提取到知识图谱中。

### 6.8 检索器

| 类型 | 类 | 说明 |
|------|-----|------|
| 向量检索 | `VectorRetriever` | 包装 LlamaIndex VectorIndexRetriever |
| 图检索 | `GraphRetriever` | 包装 LlamaIndex PGRetriever，含 LLM 同义词扩展 + 向量上下文检索 |

### 6.9 HyDE（Hypothetical Document Embeddings）

`HyDETransform` 实现 HyDE 技术：用 LLM 生成假设答案文档 → 用该文档的 embedding 做检索 → 提升零样本检索质量。

### 6.10 Schema 数据模型

| 类 | 说明 |
|------|------|
| `Document` | 源文档（文本 + metadata） |
| `TextChunk` (alias `Chunk`) | 文本块（文本 + embedding + metadata） |
| `ImageChunk` | 图像块（图像路径 + embedding） |
| `Corpus` | 文档集合（corpus_id + chunks + 索引） |
| `Query` | 检索查询（query_str + top_k + filters） |
| `RagResult` | 检索结果（corpus + scores + metadata） |

---

## 7. 工具系统

### 7.1 工具类体系

```
Tool(BaseModule)              # 抽象工具基类
└── MCPTool(Tool)             # MCP 协议工具适配器

Toolkit(BaseModule)           # 工具包（Tool 的组合容器）
    ├── SearchToolkit         # 搜索工具集
    ├── FileToolkit           # 文件操作
    ├── BrowserToolkit        # 浏览器自动化
    ├── DatabaseToolkit       # 数据库
    ├── MCPToolkit            # MCP 服务器工具
    └── ... (20+ 种)
```

### 7.2 Tool 基类

```python
class Tool(BaseModule):
    name: str                                  # 工具名称
    description: str                           # 工具描述
    inputs: Dict[str, Dict[str, Any]]          # 输入参数定义
    required: Optional[List[str]]              # 必需参数列表

    def get_tool_schema(self) -> Dict:          # 生成 OpenAI function-calling schema
    def validate_attributes(cls):               # 类创建时自动验证（__init_subclass__）
    def __call__(self, **kwargs):               # 执行工具（子类实现）
```

`validate_attributes()` 在子类创建时自动执行：
- 验证所有 inputs 有 type + description
- 校验 input 类型在 `ALLOWED_TYPES` 中（string/number/integer/boolean/object/array）
- 检查 `__call__` 参数签名与 inputs 声明一致
- 验证 required 中的参数确实在 inputs 中

### 7.3 Toolkit 工具包

```python
toolkit = SearchToolkit()        # 包含 Google, Wikipedia, DDGS 等搜索工具
tools = toolkit.get_tools()      # → List[Tool]
schemas = toolkit.get_tool_schemas()  # → List[Dict] (OpenAI schemas)
```

### 7.4 可用工具一览

| 工具包 | 功能 |
|--------|------|
| `GoogleSearchToolkit` | Google 搜索（付费 API） |
| `GoogleFreeSearchToolkit` | Google 免费搜索 |
| `DDGSSearchToolkit` | DuckDuckGo 搜索 |
| `WikipediaSearchToolkit` | Wikipedia 查询 |
| `SerpAPIToolkit` | SerpAPI 搜索 |
| `SerperAPIToolkit` | Serper.dev 搜索 |
| `ExaSearchToolkit` | Exa 搜索 |
| `BrowserToolkit` | 浏览器自动化 |
| `BrowserUseToolkit` | Browser Use 集成 |
| `RequestToolkit` | HTTP 请求 |
| `ArxivToolkit` | Arxiv 论文检索 |
| `RSSToolkit` | RSS 订阅 |
| `FileToolkit` | 文件读写 |
| `PythonInterpreterToolkit` | Python 代码执行 |
| `DockerInterpreterToolkit` | Docker 隔离执行 |
| `CMDToolkit` | 命令行执行 |
| `MongoDBToolkit` | MongoDB 操作 |
| `PostgreSQLToolkit` | PostgreSQL 操作 |
| `GoogleMapsToolkit` | Google Maps API |
| `TelegramToolkit` | Telegram 消息 |
| `GmailToolkit` | Gmail 操作 |
| `ResearchToolkit` | 学术研究工具 |
| `OpenAIImageToolkit` | DALL-E 图像生成 |
| `FluxImageGenerationToolkit` | Flux 图像生成 |
| `StorageToolkit` | 存储操作 |
| **`MCPToolkit`** | **MCP 服务器工具（动态扩展）** |

### 7.5 MCP 集成（`tools/mcp.py`）⭐

**架构：**
```
MCPToolkit（用户入口）
    ↓
MCPClient × N（每个 MCP 服务器一个客户端）
    ↓
FastMCP Client（底层连接）
    ↓
MCPTool（适配为 Tool 子类）
```

**关键实现：**

```python
# 用法
mcp_toolkit = MCPToolkit(config_path="mcp.config.json")
toolkits = mcp_toolkit.get_toolkits()  # → List[Toolkit]

# 将 MCP 工具传递给 Agent
agent = CustomizeAgent(
    ...,
    tools=toolkits,  # MCP 工具和其他工具混合
)
```

**`MCPTool` 适配：**
- 继承 `Tool`，覆盖 `validate_attributes` 放宽签名检查（MCP 工具动态变化）
- `__call__` 委托给内部 `self.function`
- `_convert_result` 递归规范化 MCP 返回值为 JSON 可序列化格式

**`MCPClient` 连接管理：**
- 在后台线程运行独立事件循环
- 使用 `asyncio.run_coroutine_threadsafe` 将同步调用桥接到异步
- 支持多服务器并发连接
- 优雅关闭（`_disconnect` 信号 → 取消任务 → 关闭循环）
- 超时控制（默认 120s 连接超时，30s 调用超时）

**`MCPToolkit` 容错：**
- 连接失败的服务器自动移除（不阻塞整体）
- 调用超时的服务器标记失败（不影响其他服务器）

---

## 8. 多角色辩论框架

### 8.1 架构

```
MultiAgentDebateActionGraph(ActionGraph)
├── debater_agents: List[CustomizeAgent]  # 辩手池
├── judge_agent: CustomizeAgent           # 裁判
├── llm_config_pool: List[LLMConfig]      # 模型池
├── group_graphs: List[ActionGraph]        # 组图模式
└── _sc_ensemble: QAScEnsemble            # 自一致性集成
```

### 8.2 辩论流程

```
1. _setup_debate()
   ├── 验证参数（agents > 1, rounds > 0）
   ├── 解析 personas（或使用默认）
   └── _prepare_runtime_debaters() → 准备辩手

2. _run_debate_rounds()  [循环 rounds × agents]
   ├── 每个辩手看到当前对话记录（transcript_mode 控制可见性）
   ├── 辩手输出 {thought, argument, answer}
   └── 添加到 transcript

3. [可选] PruningPipeline
   ├── Quality Pruning（质量过滤：与问题的 TF-IDF 相似度）
   ├── Diversity Pruning（多样性过滤：去重相似论点）
   └── Misunderstanding Rebuttal（LLM 纠正误解）

4. _generate_consensus()
   ├── judge_mode="self_consistency" → 多数投票
   └── judge_mode="llm_judge" → 裁判 Agent 裁决
```

### 8.3 使用方式

**简单模式（自动生成辩手）：**
```python
debate = MultiAgentDebateActionGraph(
    name="PolicyDebate",
    description="政策辩论",
    llm_config=llm_config,
)
result = debate.execute(
    problem="Should we invest heavily in AI research?",
    num_agents=5,
    num_rounds=3,
    judge_mode="llm_judge",
)
```

**高级模式（注入专业角色）：**
```python
agents = [
    create_optimized_agent("Optimist", ..., gpt4o_config, +0.3),
    create_optimized_agent("Analyst", ..., llama_config, -0.1),
    create_optimized_agent("Skeptic", ..., gpt4o_config, 0.0),
]
debate = MultiAgentDebateActionGraph(
    debater_agents=agents,    # 预定义角色
    llm_config=agents[0].llm_config,
)
```

### 8.4 剪枝流水线（`frameworks/multi_agent_debate/pruning.py`）

| 阶段 | 功能 | 方法 |
|------|------|------|
| Quality Pruning | 用 TF-IDF 余弦相似度过滤与问题无关的论点 | `_quality_prune()` |
| Diversity Pruning | 贪心最远优先选择，去重相似论点（阈值 0.92） | `_diversity_prune()` |
| Misunderstanding Rebuttal | LLM 作为"批评者"审查并纠正误解 | `_misunderstanding_rebuttal()` |

保证最少保留 `max(1, round(num_agents * 0.3))` 个候选项。

### 8.5 辩论输出

```python
result = {
    "final_answer": "...",      # 最终答案
    "winner": 2,                # 胜者 agent_id
    "winner_answer": "...",     # 胜者答案
    "rationale": "...",         # 裁判理由
    "transcript": [             # 完整对话记录
        {"round": 0, "agent_id": 0, "role": "Optimist", "argument": "...", "answer": "..."},
        ...
    ],
}
```

---

## 9. 存储后端

### 9.1 三层存储

```
StorageHandler（统一入口）
├── DBStore（SQLite / PostgreSQL）      — 结构化数据
├── VectorStore（FAISS / Qdrant）       — 向量索引
└── GraphStore（Neo4j）                 — 知识图谱
```

### 9.2 配置

```python
store_config = StoreConfig(
    path="/tmp/evoagentx",          # 索引缓存路径
    dbConfig=DBConfig(
        db_name="sqlite",           # sqlite | posgre_sql
        path="evoagentx.db",
    ),
    vectorConfig=VectorStoreConfig(
        vector_name="faiss",        # faiss | qdrant（未实现）
        dimensions=768,
        index_type="flat_l2",       # flat_l2 | ivf_flat
    ),
    graphConfig=None,               # 可选
)
```

### 9.3 StorageHandler（`storages/base.py`）

统一的 CRUD 接口，支持 5 种表类型：

| 表类型 | Pydantic 模型 | 用途 |
|--------|---------------|------|
| `agent` | `AgentStore` | Agent 持久化 |
| `workflow` | `WorkflowStore` | 工作流持久化 |
| `memory` | `MemoryStore` | 记忆持久化 |
| `history` | `HistoryStore` | 历史记录 |
| `indexing` | `IndexStore` | 索引持久化 |

### 9.4 SQLite 实现（`storages/db_stores/sqlite.py`）

- 线程安全（`threading.Lock`）
- 自动表创建（`CREATE TABLE IF NOT EXISTS`）
- Pydantic 验证装饰器（`@check_db_format`）
- 非字符串字段自动 `json.dumps` 序列化

### 9.5 FAISS 向量存储（`storages/vectore_stores/faiss.py`）

- 包装 `FaissMapVectorStore`
- 支持 `flat_l2`（精确搜索）和 `ivf_flat`（近似搜索）
- 加载时自动校验维度

### 9.6 Neo4j 图存储（`storages/graph_stores/neo4j.py`）

- `BasicNeo4jStore` 修复了 LlamaIndex 的 entity-upsert bug
- `Neo4jGraphStoreWrapper` 支持双向图 <-> KV 转换
- 版本检测（>= 5.23 支持向量索引）
- 完整的 Cypher 查询：`MERGE` + `UNWIND` 批量操作

---

## 10. 配置与加载

### 10.1 YAML 配置文件

```yaml
# config.yaml
llm_config:
  llm_type: "openai"         # 通过 MODEL_REGISTRY 解析
  model: "gpt-4o"
  temperature: 0.3

agents:
  - name: "Analyst"
    description: "分析师"
    prompt: "分析{data}"
    inputs:
      - {name: "data", type: "string", description: "数据"}
    outputs:
      - {name: "signal", type: "string", description: "信号"}
```

### 10.2 配置加载

```python
config = Config.from_file("config.yaml")
# config.llm_config → 已解析的 LLMConfig 字典
# config.agents → 已注入 llm_config 的 agent 定义列表
```

**特性：**
- Agent 未指定自己的 `llm_config` 时继承全局配置
- `MODEL_REGISTRY` 解耦配置解析和具体 LLM 类
- `extra="allow"` 保持向前兼容

---

## 11. 完整调用流程

### 11.1 CustomizeAgent 完整流程

```
用户代码
  │
  ├── CustomizeAgent(inputs=[...], outputs=[...], prompt=...)
  │     │
  │     ├── validate_data() → 验证输入输出参数
  │     ├── create_customize_action() → 动态创建 CustomizeAction
  │     │     ├── create_action_input() → pydantic.create_model(ActionInput)
  │     │     ├── create_action_output() → pydantic.create_model(ActionOutput)
  │     │     └── CustomizeAction(inputs_format, outputs_format, tools)
  │     │
  │     └── Agent.__init__(actions=[customize_action])
  │           ├── init_llm() → 从 llm_config 创建 LLM 实例
  │           ├── init_long_term_memory() → [可选]
  │           └── init_context_extractor() → 注册 ContextExtraction Action
  │
  ├── agent(inputs={"market": "US"})
  │     │
  │     ├── Agent.__call__()
  │     ├── Agent.execute(action_name="customize_action")
  │     │     ├── _prepare_execution()
  │     │     │     ├── 将 inputs 构建为 Message，存入 short_term_memory
  │     │     │     └── 返回 action + action_input_data
  │     │     │
  │     │     ├── CustomizeAction.execute(llm, inputs, sys_msg)
  │     │     │     ├── PromptTemplate.format() → 构建完整提示词
  │     │     │     │     ├── 系统消息（system_prompt + instruction + context + constraints）
  │     │     │     │     ├── 工具描述（TOOL_CALLING_TEMPLATE）
  │     │     │     │     ├── Few-shot 示例（demonstrations）
  │     │     │     │     ├── 输入值渲染
  │     │     │     │     └── 输出格式说明（JSON Schema / XML / Title）
  │     │     │     │
  │     │     │     ├── llm.generate(prompt, parser=outputs_format, parse_mode="json")
  │     │     │     │     └── [工具循环] → LLM 返回工具调用
  │     │     │     │         ├── 执行工具 → 结果注入 → LLM 再次调用
  │     │     │     │         └── 直到 LLM 返回最终答案
  │     │     │     │
  │     │     │     └── 返回 ActionOutput（结构化输出）
  │     │     │
  │     │     └── _create_output_message() → Message + 存入 STM
  │     │
  │     └── 返回 Message(content=ActionOutput)
  │
  └── message.content.signal  → "bearish"
      message.content.confidence → 0.85
```

### 11.2 MemoryAgent 完整流程

```
用户输入 → MemoryAgent.chat("What did we discuss?")
  │
  ├── MemoryAction.async_execute(llm, inputs, memory_manager)
  │     │
  │     ├── memory_manager.create_conversation_message()
  │     │     ├── LongTermMemory.search(query) → 语义检索相关记忆
  │     │     └── 构建消息：User Prompt + Conversation History
  │     │
  │     ├── llm.async_generate(prompt + context)
  │     │     └── LLM 基于历史生成回答
  │     │
  │     ├── memory_manager.handle_memory("add", response)
  │     │     └── LongTermMemory.add() → SHA-256 去重 → RAG 索引
  │     │
  │     └── 返回 MemoryActionOutput
  │
  └── 返回响应文本
```

### 11.3 辩论完整流程

```
debate.execute(problem="...", num_agents=3, num_rounds=3)
  │
  ├── _setup_debate()
  │     ├── 验证参数
  │     ├── 解析 personas（或生成默认：Optimist, Pessimist, Analyst...）
  │     └── _prepare_runtime_debaters() → 准备 3 个 CustomizeAgent
  │
  ├── _run_debate_rounds()  [Round 0]
  │     ├── Agent 0 发言 → 追加到 transcript
  │     ├── Agent 1 发言 → 追加到 transcript
  │     └── Agent 2 发言 → 追加到 transcript
  │
  ├── _run_debate_rounds()  [Round 1]
  │     ├── Agent 0（看到 Round 0 的对话）→ 回应并补充
  │     ├── Agent 1 → 回应
  │     └── Agent 2 → 回应
  │
  ├── _run_debate_rounds()  [Round 2]
  │     └── ... 最后一轮
  │
  ├── [可选] PruningPipeline
  │     ├── Quality Prune → 移除无关论点
  │     ├── Diversity Prune → 去重相似论点
  │     └── Misunderstanding Rebuttal → LLM 修正误解
  │
  └── _generate_consensus()
        ├── judge_mode="self_consistency" → 多数投票
        └── judge_mode="llm_judge" → 裁判 Agent 裁决
```

---

## 12. 总结：EvoAgentX 能教给 NewsRadar 什么

### 12.1 值得借鉴的设计模式

| 模式 | EvoAgentX 实现 | NewsRadar 适用场景 |
|------|---------------|-------------------|
| **声明式角色定义** | CustomizeAgent 的 inputs/outputs/prompt 声明 | 定义新闻分析师、情感分析员等角色 |
| **Action 驱动架构** | Agent 包含多个 Action，按名称调用 | Agent 可以有 analyze/summarize/classify 等 Action |
| **LLM 管理记忆** | MemoryManager 让 LLM 决定记忆操作 | Agent 自动判断哪些新闻值得记住 |
| **RAG 知识库** | RAGEngine 的完整管道 | 每个角色专属的领域知识库 |
| **工具系统** | Tool + Toolkit + MCP 集成 | 新闻搜索、RSS、数据库查询工具 |
| **多角色辩论** | MultiAgentDebateActionGraph | 多个 Agent 对同一新闻事件的不同解读 |
| **提示词模板** | StringTemplate / ChatTemplate | 结构化的分析提示词，含约束和示例 |
| **存储抽象** | StorageHandler 统一接口 | 统一的 Agent/记忆/工作流持久化 |

### 12.2 关键差异：EvoAgentX vs NewsRadar 需求

| 维度 | EvoAgentX | NewsRadar 实际需求 |
|------|-----------|-------------------|
| 框架复杂度 | 非常高（~170 文件） | 中等 |
| 学习成本 | 高（需要理解完整框架） | 较低 |
| LLM 模型支持 | 丰富（OpenAI/OpenRouter/LiteLLM） | 需要适配 |
| 中文支持 | 有限（提示词模板为英文设计） | 需要中文化 |
| 部署复杂度 | 高（需向量 DB、图 DB） | 适中 |
| 灵活度 | 非常高 | 较高 |

### 12.3 建议的迁移策略

**方案 A：直接基于 EvoAgentX 开发**
- 优点：功能最完整，开箱即用
- 缺点：依赖重，学习曲线陡
- 适合：如果 NewsRadar Agent 系统规模大、角色多

**方案 B：提取核心模式到 NewsRadar**
- 提取 `CustomizeAgent` 的声明式角色定义
- 提取 `MemoryManager` 的 LLM 管理记忆模式
- 提取 `Tool`/`Toolkit` 的工具体系
- 自行实现轻量版（去掉 Workflow/Optimizer/Benchmark 等不需要的模块）

**方案 C：混合方案（推荐）**
- Agent 定义层：参考 `CustomizeAgent` 的声明式设计
- 记忆系统：参考 `MemoryManager` 的 LLM 管理模式，但用更轻量的存储
- 工具系统：直接使用或适配 EvoAgentX 的 `Toolkit`
- 辩论框架：直接使用 `MultiAgentDebateActionGraph`
- 存储：复用 NewsRadar 已有的 PostgreSQL

---

> **文档版本**：v1.0 | **基于源码**：EvoAgentX (commit: latest) | **分析日期**：2026-07-08
