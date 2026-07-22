# Agent LLM 层精简设计：去掉 BaseClient + ChatResult

> **状态**: 草案  
> **目标**: 保留现有架构（DefaultAgent + ReActExecutor + PersonaOrchestrator），去掉冗余的 `BaseClient` 和 `ChatResult` 包装层，直接使用 LangChain 的 `AIMessage` 作为 LLM 调用的统一返回类型  
> **核心理念**: `langchain-openai` / `langchain-anthropic` 只做 SDK 接入层，不引入 LangGraph 的编排模型

---

## 1. 现状问题

### 1.1 当前 LLM 调用链路

```
ReActExecutor
  │
  ├─ client.chat(model, messages, tools=schemas)
  │     │
  │     ├─ convert_to_messages(messages)       # dict → LangChain 对象
  │     ├─ self.bind_tools(tools)               # 绑定工具
  │     ├─ bound.ainvoke(lc_messages)           # 调 LLM → LangChain AIMessage
  │     └─ _ai_to_chat_result(ai)              # AIMessage → ChatResult（反向转换！）
  │
  └─ result = ChatResult(content, tool_calls, stop_reason, usage, reasoning_content)
        │
        ├─ result.content → 文本
        ├─ result.tool_calls → 需要重新格式化成 OpenAI 旧格式
        ├─ result.stop_reason → 从 _REASON_MAP 映射
        └─ result.usage → 从 usage_metadata 拆包
```

**问题**：`AIMessage` → `ChatResult` 是多余的反向转换。`AIMessage` 已经包含了所有需要的信息，`ChatResult` 只是重新包装了一遍。

### 1.2 冗余代码量

| 文件 | 行数 | 实际价值 |
|------|------|---------|
| `agent/llm/base_client.py` | ~150 | `ChatResult` + `_ai_to_chat_result` + `_REASON_MAP` — 全部是转换代码 |
| `agent/llm/__init__.py` | ~20 | 导出，精简 |
| `agent/llm/openai_client.py` | ~30 | 继承 `ChatOpenAI`，有价值 |
| `agent/llm/anthropic_client.py` | ~30 | 继承 `ChatAnthropic`，有价值 |
| `agent/llm/deepseek.py` | ~80 | 继承 `ChatOpenAI` + `reasoning_content` 定制，有价值 |
| **合计** | **~310** | 其中 ~150 行是冗余的 |

---

## 2. 目标架构

### 2.1 新的 LLM 调用链路

```
ReActExecutor
  │
  ├─ client.chat(messages, tools=schemas)
  │     │
  │     ├─ self.bind_tools(tools)               # 绑定工具（不变）
  │     └─ bound.ainvoke(messages)              # 调 LLM → 直接返回 AIMessage
  │
  └─ result = AIMessage(content, tool_calls, response_metadata, usage_metadata)
        │
        ├─ result.content → 文本（不变）
        ├─ result.tool_calls → 直接可用，无需转换
        ├─ result.response_metadata["finish_reason"] → 替代 stop_reason
        └─ result.usage_metadata → 替代 usage
```

### 2.2 依赖变化

```toml
# pyproject.toml
dependencies = [
    "langchain-core>=0.3.0",          # BaseChatModel, AIMessage, ToolCall
    "langchain-openai>=0.3.0",        # ChatOpenAI
    "langchain-anthropic>=0.3.0",     # ChatAnthropic（可选）
]
```

不加 `langgraph`，不加 `langchain-community`。

---

## 3. 具体变化点

### 3.1 移除 `agent/llm/base_client.py`（~150 行 → 0 行）

**移除内容**：

| 代码 | 行数 | 替代方案 |
|------|------|---------|
| `ChatResult` 类 | ~30 | 直接使用 `AIMessage` |
| `_REASON_MAP` 字典 | ~10 | 直接用 `len(result.tool_calls) > 0` 判断 |
| `_ai_to_chat_result()` 函数 | ~40 | 不再需要 |
| `BaseClient.chat()` 方法 | ~30 | 移到各子类，直接返回 `AIMessage` |
| `BaseClient.chat_stream()` 方法 | ~20 | 移到各子类 |

**BaseClient 变为纯接口协议（Protocol）**：

```python
# agent/llm/protocol.py（新增，~20 行）
class LLMClient(Protocol):
    """LLM Client 接口协议——只定义签名，不包装返回值。"""
    async def chat(self, messages: list, tools: list | None = None, **kwargs) -> AIMessage:
        ...
    
    def chat_stream(self, messages: list, **kwargs) -> AsyncIterator[AIMessageChunk]:
        ...
```

### 3.2 简化 `agent/llm/openai_client.py`（~30 行 → <20 行）

```python
# 当前：继承 ChatOpenAI + BaseClient，通过 BaseClient 暴露 chat()
class OpenAIClient(ChatOpenAI, BaseClient):
    ...

# 改为：只继承 ChatOpenAI，实现 LLMClient 协议
class OpenAIClient(ChatOpenAI):
    """OpenAI / OpenAI-compatible LLM Client。
    
    继承 ChatOpenAI 获得所有 provider 兼容性（OpenAI、DeepSeek、Ollama 等），
    不额外包装返回值，直接返回 AIMessage。
    """
    
    async def chat(self, messages: list, tools: list | None = None, **kwargs) -> AIMessage:
        bound = self.bind_tools(tools) if tools else self
        return await bound.ainvoke(messages)
    
    def chat_stream(self, messages: list, **kwargs) -> AsyncIterator[AIMessageChunk]:
        return self.astream(messages)
```

### 3.3 简化 `agent/llm/anthropic_client.py`（~30 行 → <20 行）

同理，继承 `ChatAnthropic`，实现 `chat()` / `chat_stream()` 直接返回。

### 3.4 保留 `agent/llm/deepseek.py`（~80 行）

**不动**。`DeepSeekClient` 的 `reasoning_content` 回传定制是真实价值：

```python
class DeepSeekClient(ChatOpenAI):
    """DeepSeek 思考模式：override _get_request_payload / _create_chat_result 处理 reasoning_content 回传。
    
    这部分代码不是冗余，是 LangChain 没有提供的定制逻辑。
    """
    ...
```

### 3.5 简化 `agent/hub.py`（~70 行 → ~40 行）

```python
# 当前：返回 BaseClient
def _build_client(cfg: dict) -> BaseClient:
    ...

# 改为：返回 ChatOpenAI / ChatAnthropic 实例
def _build_client(cfg: dict) -> ChatOpenAI | ChatAnthropic | DeepSeekClient:
    ...
```

### 3.6 修改 `agent/executor.py`（~460 行 → ~430 行）

**核心改动**：`ReActExecutor` 中 `result.stop_reason` 判断改为 `AIMessage` 原生判断。

```python
# 当前：
result = await client.chat(model=model_version, messages=llm_messages, tools=tool_schemas)
# result = ChatResult(content, tool_calls, stop_reason)

if result.stop_reason == "stop":
    ...
elif result.stop_reason == "tool_use":
    for tc in result.tool_calls:
        ...
elif result.stop_reason == "length" and result.tool_calls:
    ...

# 改为：
result = await client.chat(messages=llm_messages, tools=tool_schemas)
# result = AIMessage(content, tool_calls, response_metadata, usage_metadata)

if result.tool_calls:
    # 同时检查是否被截断（length 场景）
    if result.response_metadata.get("finish_reason") == "length":
        # 截断 + 有工具调用 → 当作不完整工具调用处理
        for tc in result.tool_calls:
            ...
    else:
        # 正常工具调用
        for tc in result.tool_calls:
            ...
else:
    # 正常文本回答
    ...
```

**`tool_calls` 格式变化**：

```python
# 当前 ChatResult.tool_calls 格式（OpenAI 旧格式）
tc = {
    "id": "call_xxx",
    "type": "function",
    "function": {"name": "get_weather", "arguments": {"city": "北京"}},
}

# 改为 AIMessage.tool_calls 格式（LangChain 归一化格式）
tc = {
    "name": "get_weather",
    "args": {"city": "北京"},
    "id": "call_xxx",
    "type": "tool_call",
}
```

**影响范围**：`_execute_tool()` 方法中的参数提取逻辑需要改：

```python
# 当前：
fn_info = tc.get("function", tc)
fn_name = fn_info.get("name", "")
raw_args = fn_info.get("arguments", "{}")

# 改为：
fn_name = tc["name"]
raw_args = tc["args"]  # 已经是 dict，不需要 json.loads
```

### 3.7 修改 `agent/models.py` 中的 `Context`

**`stop_reason` 字段不再需要**，但为兼容保留默认值：

```python
@dataclass
class Message:
    role: str
    content: str | None = None
    tool_calls: list[dict] | None = None
    stop_reason: str | None = None  # 保留，改为从 response_metadata 按需读取
    usage: dict | None = None
    reasoning_content: str | None = None
    tool_call_id: str | None = None
    name: str | None = None
    timestamp: float = 0.0
```

**`Context` 类**：`total_tokens` 字段保留，改为从 `AIMessage.usage_metadata` 读取。

### 3.8 修改 `_messages_to_dicts()` 方法

```python
# 当前：
def _messages_to_dicts(self, messages):
    d = {"role": msg.role}
    if msg.role == "assistant" and msg.tool_calls:
        d["content"] = None
        d["tool_calls"] = msg.tool_calls  # 旧格式
    ...

# 改为：
def _messages_to_dicts(self, messages):
    d = {"role": msg.role}
    if msg.role == "assistant" and msg.tool_calls:
        d["content"] = None
        d["tool_calls"] = msg.tool_calls  # 已改为新格式，直接透传
    ...
```

### 3.9 修改 `agent/agent.py` 中的 `DefaultAgent`

**小改动**：`chat()` 方法中 `AgentResult` 构造时 `total_tokens` 从 `ctx` 累加值读取（逻辑不变，数据来源变了）。

---

## 4. 改动汇总

### 4.1 文件变更清单

| 文件 | 操作 | 行数变化 | 说明 |
|------|------|---------|------|
| `agent/llm/base_client.py` | ❌ 删除 | -150 | 整个文件移除 |
| `agent/llm/__init__.py` | ✏️ 修改 | -5 | 不再导出 `BaseClient`、`ChatResult` |
| `agent/llm/protocol.py` | ✨ 新增 | +20 | `LLMClient` Protocol |
| `agent/llm/openai_client.py` | ✏️ 修改 | -10 | 简化，去掉 `BaseClient` 继承 |
| `agent/llm/anthropic_client.py` | ✏️ 修改 | -10 | 同上 |
| `agent/llm/deepseek.py` | ◀️ 保留 | 0 | 不动 |
| `agent/hub.py` | ✏️ 修改 | -30 | 返回类型改为 `ChatOpenAI`/`ChatAnthropic` |
| `agent/executor.py` | ✏️ 修改 | -30 | `stop_reason` 判断逻辑调整 + `tool_calls` 格式调整 |
| `agent/models.py` | ◀️ 保留 | 0 | 不动 |
| `agent/agent.py` | ◀️ 保留 | 0 | 不动 |
| `agent/persona/` | ◀️ 保留 | 0 | 不动 |
| `agent/factory.py` | ◀️ 保留 | 0 | 不动 |
| `agent/tools/` | ◀️ 保留 | 0 | 不动 |
| `tests/` | ✏️ 修改 | 待评估 | Mock 逻辑需要适配 `AIMessage` 返回 |
| **合计** | | **-~215** | 净减少约 215 行代码 |

### 4.2 代码量变化

```
当前: ~310 行（LLM 层）
改为: ~95 行（Protocol + 3 个 client 实现）
净减少: ~215 行（主要是转换代码）
```

---

## 5. 关键变化点详解

### 5.1 变化点 1：`stop_reason` 判断逻辑

**当前**：

```python
# base_client.py — _REASON_MAP 映射
_REASON_MAP = {
    "stop": "stop", "end_turn": "stop",
    "tool_calls": "tool_use", "tool_use": "tool_use",
    "length": "length", "max_tokens": "length",
    "content_filter": "error", "stop_sequence": "stop",
}

# executor.py — 使用 stop_reason
if result.stop_reason == "stop":
    ...
elif result.stop_reason == "tool_use":
    ...
elif result.stop_reason == "length" and result.tool_calls:
    ...
```

**改为**：

```python
# executor.py — 直接判断 AIMessage
if result.tool_calls:
    # 有工具调用
    finish_reason = result.response_metadata.get("finish_reason", "")
    if finish_reason in ("length", "max_tokens"):
        # 截断 + 不完整工具调用
        _handle_truncated_tool_calls(result.tool_calls)
    else:
        # 正常工具调用
        _execute_tool_calls(result.tool_calls)
else:
    # 无工具调用 -> 文本回答
    ...
```

**关键区别**：不再依赖 `_REASON_MAP` 映射，直接用 `AIMessage` 原生信息判断。

### 5.2 变化点 2：`tool_calls` 格式变化

**当前**（ChatResult 格式，兼容旧 `_execute_tool`）：

```python
tc = {
    "id": "call_xxx",
    "type": "function",
    "function": {
        "name": "get_weather",
        "arguments": {"city": "北京"},  # 注意：不是 JSON 字符串，已经是 dict
    },
}
```

**改为**（AIMessage 原生格式）：

```python
tc = {
    "name": "get_weather",
    "args": {"city": "北京"},
    "id": "call_xxx",
    "type": "tool_call",
}
```

**`_execute_tool` 中的参数提取**：

```python
# 当前（需要处理两种格式兼容）：
fn_info = tc.get("function", tc)
fn_name = fn_info.get("name", "") if isinstance(fn_info, dict) else ""
raw_args = fn_info.get("arguments", "{}") if isinstance(fn_info, dict) else "{}"
if isinstance(raw_args, str):
    fn_args = json.loads(raw_args)
else:
    fn_args = raw_args

# 改为（统一格式，无需兼容）：
fn_name = tc["name"]
fn_args = tc["args"]  # 直接是 dict，无需 json.loads
```

### 5.3 变化点 3：`usage` 读取方式

**当前**：

```python
# executor.py
if result.usage:
    ctx.total_input_tokens += result.usage.get("prompt_tokens", 0)
    ctx.total_output_tokens += result.usage.get("completion_tokens", 0)
```

**改为**：

```python
# executor.py
if result.usage_metadata:
    ctx.total_input_tokens += result.usage_metadata.get("input_tokens", 0)
    ctx.total_output_tokens += result.usage_metadata.get("output_tokens", 0)
```

**注意字段名差异**：`prompt_tokens` → `input_tokens`，`completion_tokens` → `output_tokens`。

### 5.4 变化点 4：`reasoning_content` 读取

**当前**：

```python
# _ai_to_chat_result 中
reasoning = (getattr(ai, "additional_kwargs", None) or {}).get("reasoning_content", "") or ""
```

**改为**（直接在 executor 中读取）：

```python
# executor.py
reasoning = result.additional_kwargs.get("reasoning_content", "")
```

### 5.5 变化点 5：`chat_stream` 的 tool_call 处理

**当前**：`BaseClient.chat_stream()` 只 yield `chunk.content`，忽略 `tool_call_chunks`。

**改为**：直接返回 `astream` 的原始 chunk，由调用方决定是否处理工具调用：

```python
# 当前
async def chat_stream(self, ...):
    async for chunk in self.astream(lc_messages):
        if chunk.content:
            yield chunk.content

# 改为
async def chat_stream(self, messages, **kwargs):
    async for chunk in self.astream(messages):
        yield chunk  # 返回原始 AIMessageChunk
```

**影响**：`DirectExecutor.run_stream()` 中需要调整：

```python
# 当前
async for token in client.chat_stream(...):
    chunks.append(token)
    yield token

# 改为
async for chunk in client.chat_stream(...):
    text = chunk.content or ""
    chunks.append(text)
    yield text
```

---

## 6. 测试改动

### 6.1 Mock 策略变化

**当前**：`MockClient(BaseClient)`，返回 `ChatResult`。

```python
class MockClient(BaseClient):
    def __init__(self, ...):
        self.tool_calls_to_return = []
    
    async def chat(self, model, messages, **kwargs):
        if self.tool_calls_to_return:
            return ChatResult(content="", tool_calls=self.tool_calls_to_return)
        return ChatResult(content="mock response")
```

**改为**：`MockClient(ChatOpenAI)`，返回 `AIMessage`。

```python
from langchain_core.messages import AIMessage

class MockLLM(ChatOpenAI):
    """Mock LLM 用于测试，直接返回 AIMessage。"""
    
    def __init__(self, **kwargs):
        super().__init__(model="mock", api_key="test")
        self.tool_calls_to_return = []
    
    async def ainvoke(self, messages, **kwargs):
        if self.tool_calls_to_return:
            return AIMessage(
                content="",
                tool_calls=self.tool_calls_to_return,  # 直接传 [{name, args, id}]
            )
        return AIMessage(content="mock response")
```

### 6.2 受影响测试文件

| 文件 | 改动范围 |
|------|---------|
| `tests/test_agent_agent.py` | Mock 适配 `AIMessage` 返回 |
| `tests/test_agent_tools.py` | `tool_calls` 格式调整 |
| `tests/test_agent_memory.py` | `usage` 字段名调整 |
| `tests/test_persona_agent.py` | Mock 适配 |
| `tests/test_persona_orchestrator.py` | Mock 适配 |

---

## 7. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| `AIMessage.tool_calls` 格式在不同 provider 间不一致 | 低 | 中 | LangChain 已经归一化：`[{name, args, id, type}]`，所有 provider 一致 |
| `reasoning_content` 在 `AIMessage` 中读取方式不同 | 中 | 低 | `additional_kwargs` 在所有 provider 中通用，DeepSeek 的定制代码不动 |
| `usage_metadata` 字段名在旧版 langchain-core 中不同 | 低 | 低 | 锁定 `langchain-core>=0.3.0` |
| 现有测试需要大量修改 | 中 | 中 | 策略：Mock 返回 `AIMessage`，保持测试逻辑不变，只改 Mock 构造 |

---

## 8. 实施路线

### Phase 0：代码修改（1 天）

```
1. 创建 agent/llm/protocol.py（LLMClient Protocol）
2. 修改 agent/llm/openai_client.py（去掉 BaseClient 继承）
3. 修改 agent/llm/anthropic_client.py（去掉 BaseClient 继承）
4. 删除 agent/llm/base_client.py
5. 修改 agent/llm/__init__.py（去掉 BaseClient/ChatResult 导出）
6. 修改 agent/hub.py（返回类型调整）
7. 修改 agent/executor.py（stop_reason 判断 + tool_calls 格式）
8. 修改 agent/executor.py（usage 字段名调整）
```

### Phase 1：测试适配（1 天）

```
1. 修改测试 Mock 类
2. 运行全部测试，修复失败
3. 确保覆盖率 >= 80%
```

### Phase 2：验证（半天）

```
1. 启动 daemon，验证单 Agent 对话
2. 验证工具调用（MCP news 工具）
3. 验证团队会诊（多角色 fan-out + 主编聚合）
4. 验证审批通道（WebSocket 工具审批）
5. 验证流式输出
```

---

## 9. 总结

**改动本质**：去掉 ~150 行冗余的 `ChatResult` 转换代码，直接使用 LangChain 的 `AIMessage` 作为 LLM 调用的统一返回类型。

**收益**：
- 净减少 ~215 行代码
- 去掉 `_REASON_MAP`、`_ai_to_chat_result`、`ChatResult` 三个冗余概念
- 不再需要 `convert_to_messages` 的 dict → 对象转换
- 保留所有现有架构（DefaultAgent、ReActExecutor、PersonaOrchestrator）
- 保留所有定制化能力（`reasoning_content` 回传、审批通道、流式）
- 不加任何新依赖（`langgraph` 等）

**不改变**：
- ✅ 当前架构设计（DefaultAgent + Executor + Memory + Tools）
- ✅ 审批通道（WebSocket 回调）
- ✅ 团队会诊（PersonaOrchestrator）
- ✅ 知识库（KnowledgeEngine）
- ✅ 工具系统（Registry + MCP）
- ✅ 配置格式（config.yaml models 段）
- ✅ 数据库表结构
- ✅ 前端 WebSocket 协议