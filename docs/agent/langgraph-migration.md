# LangGraph 迁移设计：变化点与难点

> **状态**: 草案  
> **目标**: 评估将当前手写 Agent 架构（DefaultAgent + ReActExecutor + PersonaOrchestrator）迁移到 LangGraph 的可行性、变化范围和难点  
> **阅读前提**: 熟悉 [architecture-v1.md](architecture-v1.md) 和 [persona.md](persona.md)

---

## 1. 当前架构总览

```
agent/
├── agent.py            # DefaultAgent — 轻量编排器，持有 brain/executor/memory/tools
├── executor.py         # DirectExecutor + ReActExecutor — 手写推理循环（~460 行）
├── hub.py              # ModelHub — LLM Client 池，惰性创建
├── llm/
│   ├── base_client.py  # BaseClient + ChatResult — LLM 调用的门面 + 归一化返回
│   ├── openai_client.py
│   ├── anthropic_client.py
│   └── deepseek.py
├── models.py           # Context / Message / AgentResult — 数据模型
├── memory.py           # MemoryModule — hook 模式记忆系统
├── tools/
│   ├── base.py         # @tool 装饰器 / FunctionTool / BaseTool
│   ├── registry.py     # ToolRegistry — 工具注册中心
│   └── tools.py        # 内置工具集
├── mcp/
│   └── news_server.py  # MCP Server / MCPClient
├── knowledge/          # pgvector 知识库
├── persona/
│   ├── base.py          # PersonaAgent(DefaultAgent)
│   ├── orchestrator.py  # PersonaOrchestrator — 手写 fan-out + 主编聚合（~170 行）
│   ├── manager.py       # PersonaManager — 懒构建 + 缓存
│   ├── registry.py      # 角色注册中心
│   ├── signal.py        # PersonaSignal
│   ├── editor.py
│   ├── investors/       # 4 个投资人角色
│   └── experts/         # 5 个专家角色
└── factory.py          # create_agent / create_persona / create_persona_orchestrator
```

---

## 2. 目标架构（LangGraph 版）

```
agent/
├── graph/
│   ├── agent_graph.py      # 单 Agent graph（替代 DefaultAgent + ReActExecutor）
│   ├── team_graph.py       # 团队会诊 graph（替代 PersonaOrchestrator）
│   ├── state.py            # AgentState（替代 Context）
│   └── nodes/
│       ├── llm_node.py     # 调 LLM
│       ├── memory_node.py  # 检索/存储记忆
│       ├── knowledge_node.py # 检索知识
│       └── tool_node.py    # ToolNode（LangGraph 内置）
├── hub.py              # ModelHub 保留，但只返回 BaseChatModel 而不是 BaseClient
├── llm/                # 精简：去掉 ChatResult / _ai_to_chat_result / _REASON_MAP
│   ├── openai_client.py
│   ├── anthropic_client.py
│   └── deepseek.py
├── models.py           # AgentState + AgentResult（去掉 Context）
├── memory.py           # MemoryModule 保留，但改为 graph 节点调用
├── tools/              # 保持不动
├── mcp/                # 保持不动
├── knowledge/          # 保持不动
├── persona/
│   ├── base.py          # PersonaAgent 保留，但不再持有 LLM Client
│   ├── registry.py      # 保持不动
│   ├── signal.py        # 保持不动
│   ├── investors/       # 保持不动
│   └── experts/         # 保持不动
└── factory.py          # 改为构建 graph 而非 DefaultAgent
```

---

## 3. 变化点矩阵

### 3.1 需要移除的模块

| 文件 | 行数 | 移除原因 | 替代方案 |
|------|------|---------|---------|
| `agent/executor.py` | ~460 | 整个手写循环被 graph 替代 | `conditional_edges` + `ToolNode` |
| `agent/llm/base_client.py` | ~150 | `ChatResult` / `_ai_to_chat_result` / `_REASON_MAP` 不再需要 | LangChain 的 `AIMessage` 自带归一化 |
| `agent/agent.py` | ~150 | `DefaultAgent` 的 `chat()`/`chat_stream()` 被 graph 替代 | `graph.invoke()` / `graph.astream()` |
| `agent/models.py` | ~120 | `Context` 不再需要 | `AgentState`（TypedDict）|

### 3.2 需要保留但修改的模块

| 模块 | 修改内容 | 难度 |
|------|---------|------|
| **`agent/hub.py`** | `ModelHub.get()` 返回 `BaseChatModel` 而非 `BaseClient`；去掉 `get_model_version()`（LangChain 内部管理 model） | ⭐ 低 |
| **`agent/llm/*.py`** | 去掉 `BaseClient` 基类，子类直接继承 `ChatOpenAI`/`ChatAnthropic`；去掉 `ChatResult` 返回 | ⭐⭐ 中 |
| **`agent/memory.py`** | `MemoryModule` hook 接口保留，但改为 graph 节点调用（不再是 executor hook） | ⭐ 低 |
| **`agent/persona/base.py`** | `PersonaAgent` 不再继承 `DefaultAgent`，改为"角色配置 + graph 节点工厂" | ⭐⭐ 中 |
| **`agent/persona/orchestrator.py`** | 手写 `asyncio.gather` + 主编聚合改为子 graph + supervisor 模式 | ⭐⭐⭐ 高 |
| **`agent/factory.py`** | `create_agent()` 返回 `CompiledGraph` 而非 `DefaultAgent` | ⭐⭐ 中 |
| **`web/agent.py`** | WebSocket 处理从 `agent.chat_stream()` 改为 `graph.astream()` | ⭐⭐ 中 |

### 3.3 保持不动的模块

| 模块 | 原因 |
|------|------|
| **`agent/tools/`** | `@tool` 装饰器、`FunctionTool`、`Registry` 可以继续用，LangGraph 的 `ToolNode` 也接受 `BaseTool` |
| **`agent/mcp/`** | `MCPClient` 无状态，继续用 |
| **`agent/knowledge/`** | `KnowledgeEngine` 无 LangGraph 依赖，继续用 |
| **`agent/persona/investors/` + `experts/`** | 子类只定义 `get_system_prompt()` 和 `_pre_analyze()`，不受框架影响 |
| **`agent/persona/signal.py`** | `PersonaSignal` 纯数据模型 |

---

## 4. 核心变化点详解

### 4.1 变化点 1：Executor 消失

**当前**：`ReActExecutor` 手写 `for step in range(max_steps)` 循环，管理 LLM 调用、工具执行、消息拼接、退出判断。

**LangGraph**：graph 的 `conditional_edges` 自动做路由：

```python
# 当前：460 行手写循环
class ReActExecutor(Executor):
    async def run(self, ctx, brain, memory, tools):
        for step in range(max_steps):
            result = await client.chat(model, messages, tools=schemas)
            if result.stop_reason == "tool_use":
                for tc in result.tool_calls:
                    tool_msg = await self._execute_tool(tc, tools, ctx)
                    ctx.messages.append(tool_msg)
                continue
            elif result.stop_reason == "stop":
                return result.content

# LangGraph：3 行配置
workflow.add_edge("agent", "tools")           # 有 tool_calls → 去 ToolNode
workflow.add_conditional_edges("agent", tools_condition, ...)
workflow.add_edge("tools", "agent")           # 工具执行完 → 回 agent
```

**难点**：`_check_policy` 审批通道无法直接映射到 `ToolNode`，需要自定义 wrapper 或 `interrupt`。`stop_reason == "length"` 截断处理在 `ToolNode` 中没有原生支持。

### 4.2 变化点 2：ChatResult 消失

**当前**：`BaseClient.chat()` 返回 `ChatResult`，携带 `content` + `tool_calls` + `stop_reason` + `usage` + `reasoning_content`。

**LangGraph**：直接调 `llm.invoke()` 返回 `AIMessage`，`AIMessage.content` 和 `AIMessage.tool_calls` 已经归一化。

```python
# 当前
result = await client.chat(model, messages, tools=schemas)
# result = ChatResult(content="", tool_calls=[...], stop_reason="tool_use")

# LangGraph
result = await llm.invoke(messages)
# result = AIMessage(content="", tool_calls=[{"name":..., "args":..., "id":...}])
```

**难点**：`ChatResult` 目前承载了：
- `stop_reason` → LangGraph 没有 `stop_reason`，用 `AIMessage.tool_calls` 是否非空来判断
- `reasoning_content` → 需要从 `AIMessage.additional_kwargs` 读取，DeepSeek 的 `reasoning_content` 回传需要自定义 `AIMessage` 子类

### 4.3 变化点 3：Context 消失

**当前**：`Context` 是模块间的数据总线，`Executor` 读写它，`Memory`/`Knowledge` 通过 hook 写入它。

**LangGraph**：`AgentState`（TypedDict）替代 Context。所有节点读写同一个 state dict。

```python
# 当前
class Context:
    user_input: str
    system_prompt: str
    memory_context: Any
    knowledge_context: Any
    messages: list[Message]
    ...

# LangGraph
class AgentState(TypedDict):
    messages: list[BaseMessage]    # 消息历史（LangChain 原生）
    user_input: str
    session_id: str
    memory_context: str | None
    knowledge_context: str | None
    persona_name: str | None
    analysis_context: str | None
```

**难点**：`Context` 是 dataclass（类型安全、IDE 补全），`AgentState` 是 TypedDict（运行时无类型检查）。所有引用 `ctx.field` 的代码需要改为 `state["field"]`。

### 4.4 变化点 4：PersonaOrchestrator 变为子 graph

**当前**：`PersonaOrchestrator` 手写 `asyncio.gather` 并行 fan-out + 主编聚合。

```python
# 当前
class PersonaOrchestrator:
    async def _fanout(self, message, persona_names, model_name):
        tasks = [self._run_one(name, message, model_name) for name in names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # 解析 PersonaSignal...
```

**LangGraph**：用 `Send` API 做并行 fan-out + supervisor 模式聚合。

```python
# LangGraph
def fanout_to_analysts(state):
    return [Send(f"analyst_{name}", state) for name in state["persona_names"]]

workflow.add_conditional_edges("router", fanout_to_analysts)
# 每个 analyst 节点独立运行，结果自动聚合到 state
workflow.add_edge("analyst_buffett", "editor")
workflow.add_edge("analyst_macro", "editor")
```

**难点**：
- 每个角色需要独立的 LLM 实例（因为 system prompt 不同）→ 需要 `Send` + 每个角色一个 node
- 主编需要看到所有角色的 `PersonaSignal` 才能聚合
- 当前 `PersonaManager` 的懒构建缓存需要适配 graph 的节点工厂

### 4.5 变化点 5：审批通道

**当前**：`ReActExecutor._exec_tool_with_policy()` 在工具执行前插入策略检查，通过 WebSocket 审批回调异步等待用户决策。

```python
# 当前
async def _exec_tool_with_policy(self, tools, name, args, running_mode):
    result = self._check_policy(tool, running_mode)
    if result.decision == PolicyDecision.APPROVAL_REQUIRED:
        decision = await self._approval_callback(tool.get_def(), args)
        if decision.get("approved"):
            return await tools.execute(name, args)
```

**LangGraph**：有 `interrupt` 机制，但模式不同。

```python
# LangGraph 的 interrupt 模式
def tool_node(state, config):
    tool_call = state["messages"][-1].tool_calls[0]
    if needs_approval(tool_call):
        interrupt({                     # ← 暂停 graph，等待用户输入
            "tool_call": tool_call,
            "question": "批准此工具调用？",
        })
    return {"messages": [execute_tool(tool_call)]}
```

**难点**：
- `interrupt` 暂停的是整个 graph，不是单个工具调用
- 前端 WebSocket 审批流程需要重构：当前是 `tool_approval_response` 消息 → future.set_result 的异步模式，LangGraph 的 `interrupt` 需要 `Command` 恢复
- 多个工具调用并发时，`interrupt` 的行为与当前串行审批不同

### 4.6 变化点 6：流式输出

**当前**：`ReActExecutor.run_stream()` 用 `client.chat()`（非流式）做工具判断，最后一步 `re.split` 模拟流式。

**LangGraph**：`graph.astream()` 原生支持流式，但**流式事件类型不同**：

```python
# LangGraph 流式事件
async for event in graph.astream(input):
    # event 类型：
    # {"type": "values", "data": {"messages": [...]}}  ← 每个节点执行完
    # {"type": "messages", "data": {"chunk": AIMessageChunk("...")}}  ← LLM 流式 token
```

**难点**：
- 当前前端 WebSocket 期望 `{"type": "token", "content": "..."}` 事件
- LangGraph 的流式事件格式不同，需要做适配层
- 团队会诊场景下，需要区分"哪个角色在输出"（Phase 1 静默、Phase 2 主编流式）

---

## 5. 迁移路线图

### Phase 0：基础替换（1-2 周）

```
目标：单 Agent 场景用 LangGraph 跑通，保留手写代码作为对照
```

| 步骤 | 文件 | 说明 |
|------|------|------|
| 1 | `agent/graph/state.py` | 定义 `AgentState`（替代 `Context`） |
| 2 | `agent/graph/nodes/llm_node.py` | 定义 LLM 调用节点 |
| 3 | `agent/graph/nodes/memory_node.py` | 定义记忆检索/存储节点 |
| 4 | `agent/graph/nodes/knowledge_node.py` | 定义知识库检索节点 |
| 5 | `agent/graph/agent_graph.py` | 组装单 Agent graph |
| 6 | `agent/factory.py` | 新增 `create_agent_graph()` 工厂方法 |
| 7 | `web/agent.py` | 新增 WebSocket 处理路径，走 `graph.astream()` |
| 8 | 测试 | 验证单 Agent 对话、工具调用、记忆、知识库全部正常 |

**关键决策点**：审批通道是走 `interrupt` 还是自定义 ToolNode wrapper。

### Phase 1：多角色编排（1 周）

```
目标：团队会诊用 LangGraph 的 Send API 跑通
```

| 步骤 | 说明 |
|------|------|
| 1 | 定义各角色子 graph 或 node 工厂（每个角色独立 LLM 实例） |
| 2 | 实现 `fanout_to_analysts` 路由函数（使用 `Send` API） |
| 3 | 实现主编聚合 node |
| 4 | 验证多角色并行 fan-out + 主编聚合的流式输出 |

### Phase 2：清理旧代码（1 周）

```
目标：移除手写代码，确认不再需要
```

| 步骤 | 说明 |
|------|------|
| 1 | 移除 `agent/executor.py` 中的 `ReActExecutor` |
| 2 | 移除 `agent/llm/base_client.py` 中的 `ChatResult` 和 `_ai_to_chat_result` |
| 3 | 移除 `agent/agent.py` 中的 `DefaultAgent`（或改为 graph 的薄包装） |
| 4 | 移除 `agent/models.py` 中的 `Context` |
| 5 | 更新所有测试文件 |

---

## 6. 难点汇总

| 难度 | 变化点 | 核心问题 | 可行方案 |
|------|--------|---------|---------|
| ⭐⭐⭐ | **审批通道** | 当前 `_exec_tool_with_policy` 的 WebSocket 异步审批模式与 LangGraph `interrupt` 不匹配 | 方案 A：自定义 ToolNode wrapper 接管审批；方案 B：用 `interrupt` + 前端适配 |
| ⭐⭐⭐ | **角色 graph 独立 LLM** | 每个角色需要不同的 system prompt 和知识库，但 LangGraph 的 node 共享同一个 LLM 实例 | 用 `Send` + 每个角色一个 node + 独立的 LLM 实例 |
| ⭐⭐ | **流式事件适配** | LangGraph 的事件格式与前端 WebSocket 协议不同 | 写适配层：`graph.astream()` → `{"type": "token", "content": "..."}` |
| ⭐⭐ | **`reasoning_content` 回传** | DeepSeek 思考模式的 `reasoning_content` 需要在多轮对话中回传 | 自定义 `AIMessage` 子类，在 `additional_kwargs` 中传递 |
| ⭐⭐ | **PersonaAgent 重构** | `PersonaAgent` 当前继承 `DefaultAgent`，改为 graph 后继承关系不再有意义 | 改为"角色配置 dataclass + 节点工厂函数" |
| ⭐ | **`Context` → `AgentState`** | 所有 `ctx.field` 改为 `state["field"]` | 机械替换，但量大 |
| ⭐ | **`ModelHub` 适配** | 返回 `BaseChatModel` 而非 `BaseClient` | 小改动 |
| ⭐ | **测试更新** | 当前测试 mock `MockClient(BaseClient)`，需要改为 mock `AIMessage` | 测试框架需要更新 |

---

## 7. 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| LangGraph 的 `interrupt` 审批体验不如当前 WebSocket 回调 | 中 | 高 | Phase 0 做原型验证，保留手写代码作为备选 |
| LangGraph 版本升级破坏 API | 低 | 高 | 锁定 `langgraph` 版本 |
| 团队会诊的并行性能不如手写 `asyncio.gather` | 低 | 中 | 基准测试对比 |
| 测试覆盖下降 | 中 | 高 | 新旧代码并行运行期间，双轨测试 |
| 知识库/记忆/工具的集成调试时间长 | 中 | 中 | 每个模块独立 graph 节点，可单独测试 |

---

## 8. 不做的事情

- **不改 `agent/tools/`**：`@tool` 装饰器和 `Registry` 保持不动，LangGraph 的 `ToolNode` 接受 `BaseTool` 实例
- **不改 `agent/mcp/`**：`MCPClient` 无状态，继续用
- **不改 `agent/knowledge/`**：`KnowledgeEngine` 无 LangGraph 依赖
- **不改角色子类**：`investors/buffett.py` 等只定义 `get_system_prompt()` 和 `_pre_analyze()`，不受影响
- **不改 `config.yaml`**：模型配置格式不变，`ModelHub` 适配即可
- **不改数据库表结构**：`agent_sessions` / `agent_messages` / `agent_memories` / `knowledge_chunks` 不变

---

## 9. 决策待办

- [ ] **审批通道方案选择**：自定义 ToolNode wrapper vs `interrupt`
- [ ] **`DefaultAgent` 保留还是移除**：如果保留，作为 graph 的薄包装（向后兼容 vs 清理旧代码）
- [ ] **LangGraph 版本锁定**：需要确认 `langgraph` 和 `langchain-core` 的兼容版本
- [ ] **`reasoning_content` 方案**：自定义 `AIMessage` 子类 vs `additional_kwargs`
- [ ] **新旧代码并行期**：双轨运行多久，何时移除旧代码