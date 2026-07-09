# Vibe-Trading 核心设计分析报告

> **版本**：v0.1.10 | **代码量**：~140,000 行（Python 126k + TypeScript 14k）
> **定位**：个人 AI 交易代理 — 一条命令赋予 Agent 全面的交易能力
> **团队**：HKUDS（香港大学）

---

## 目录

1. [整体架构](#1-整体架构)
2. [Agent 循环与上下文管理](#2-agent-循环与上下文管理)
3. [LLM 供应商抽象](#3-llm-供应商抽象)
4. [工具体系](#4-工具体系)
5. [Swarm 多智能体系统](#5-swarm-多智能体系统)
6. [数据架构](#6-数据架构)
7. [安全设计](#7-安全设计)
8. [关键子系统](#8-关键子系统)
9. [设计决策评注](#9-设计决策评注)

---

## 1. 整体架构

### 1.1 分层架构图

```
┌──────────────────────────────────────────────────────────────┐
│                      接入层 (Surfaces)                         │
│  CLI (vibe-trading)  │  Web UI (React 19)  │  MCP Server      │
│  REST API (FastAPI)  │  IM Channels (16种) │  PyPI 包          │
├──────────────────────────────────────────────────────────────┤
│                    Agent 核心 (ReAct 循环)                      │
│  AgentLoop: 5 层上下文压缩 + 读/写工具批处理 + 心跳/进度        │
│  ContextBuilder: 系统提示词 + 技能摘要 + 记忆注入               │
│  ToolRegistry: 54 个 MCP 工具的注册 / 执行 / 去重               │
├──────────────────────────────────────────────────────────────┤
│                     能力层 (Capabilities)                      │
│  ┌─────────────┬──────────────┬──────────────┬─────────────┐ │
│  │ 7 回测引擎   │ Alpha Zoo    │ Shadow Acct  │ 29 Swarm    │ │
│  │ + 基准面板   │ 452 因子     │ 提取→回测→报告│ 预设团队     │ │
│  ├─────────────┼──────────────┼──────────────┼─────────────┤ │
│  │ 10+ 券商接口 │ 因子分析 IC/IR│ 期权定价      │ 79 技能     │ │
│  │ 风控边界     │ 多平台导出    │ BS + Greeks  │ 技术/量化/风控│ │
│  └─────────────┴──────────────┴──────────────┴─────────────┘ │
├──────────────────────────────────────────────────────────────┤
│                   基础设施层 (Infrastructure)                   │
│  ┌──────────────────────┬─────────────────────────────────┐  │
│  │ 13+ LLM 供应商        │ 18 数据源 + 有序 fallback         │  │
│  │ 能力矩阵 + 适配层      │ 本地缓存 + OHLC 完整性守卫         │  │
│  ├──────────────────────┼─────────────────────────────────┤  │
│  │ 持久化                │ 安全边界                          │  │
│  │ Session/记忆/Goal/    │ 路径沙箱 / API 认证 / CSRF /      │  │
│  │ Swarm 存储 + FTS5     │ 交易风控 (mandate + kill switch)   │  │
│  └──────────────────────┴─────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 1.2 核心设计哲学

与 TradingAgents 的 LangGraph StateGraph 不同，Vibe-Trading 选择了**自研 ReAct 循环**作为核心编排机制。这个选择带来了几个关键差异：

| 维度 | TradingAgents (LangGraph) | Vibe-Trading (自研 ReAct) |
|------|--------------------------|--------------------------|
| 编排模型 | 声明式图（节点 + 条件边） | 命令式循环（for + while） |
| 状态管理 | AgentState (TypedDict) | WorkspaceMemory (dataclass) |
| 工具调用 | ToolNode 批量执行 | 手动批量执行（read/write 分离） |
| 上下文压缩 | 无 | 5 层级联压缩 |
| 流式支持 | LangGraph stream mode | 自研 SSE + 心跳 + 进度事件 |
| 断点续跑 | LangGraph SqliteSaver | 无（每次 run() 独立） |

---

## 2. Agent 循环与上下文管理

这是整个项目中最精妙的部分。`AgentLoop`（`agent/src/agent/loop.py`，~1500 行）实现了一个生产级的 ReAct 循环，核心创新在于 **5 层上下文压缩的级联触发机制**。

### 2.1 ReAct 循环生命周期

```python
# agent/src/agent/loop.py — AgentLoop.run() 简化伪代码

def run(self, user_message: str, history=None, session_id="") -> dict:
    self._cancel_event.clear()
    run_dir = state_store.create_run_dir(RUNS_DIR)

    # 1. 构建消息上下文（系统提示词 + 技能 + 记忆 + 目标）
    context = ContextBuilder(registry, memory, persistent_memory=...)
    messages = context.build_messages(user_message, history)

    # 2. ReAct 循环
    for iteration in range(self.max_iterations):
        if self._cancel_event.is_set():
            break

        # 2a. 层级上下文压缩（递增触发）
        tokens = estimate_tokens(messages)
        if tokens > MICROCOMPACT_THRESHOLD:   # L1: 50% 阈值
            _microcompact(messages)
        if tokens > COLLAPSE_THRESHOLD:        # L2: 70% 阈值
            _context_collapse(messages)
        if tokens > TOKEN_THRESHOLD:           # L3: 100% 阈值
            self._auto_compact(messages, ...)

        # 2b. 注入 wrap-up 提示（80% 迭代预算时）
        if iteration == wrap_up_at:
            messages.append({"role": "user", "content": "[SYSTEM] ..."})

        # 2c. LLM 流式调用（最后一次迭代去掉工具定义）
        tool_defs = None if is_last_iteration else registry.get_definitions()
        response = llm.stream_chat(messages, tools=tool_defs, ...)

        # 2d. 无工具调用 → 最终答案
        if not response.has_tool_calls:
            final_content = response.content
            break

        # 2e. 工具批处理执行：
        #     连续只读工具 → ThreadPoolExecutor 并行
        #     写工具 → 串行执行
        self._process_tool_calls(response.tool_calls, ...)

    return {"status": final_status, "content": final_content, ...}
```

### 2.2 五层上下文压缩

```
L1: microcompact     (50% 阈值) — 清除旧工具结果，保留最近 N 个
L2: context_collapse (70% 阈值) — 折叠长文本，零 API 成本
L3: auto_compact     (100% 阈值) — LLM 结构化摘要 + token-budget 尾部保护
L4: compact 工具     (模型主动调用) — 模型可自行触发 L3
L5: iterative_update (第 N 次压缩) — 更新前次摘要，零信息衰减
```

**L1 — microcompact**：最轻量的压缩。当 token 估算超过 50% 阈值（默认 40000 * 0.5 = 20000 token）时触发。将旧工具结果替换为 `"[cleared]"`，保留最近 `KEEP_RECENT`（默认 3）个结果。

```python
# agent/src/agent/loop.py
def _microcompact(messages: list) -> None:
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if len(tool_msgs) <= KEEP_RECENT:
        return
    for msg in tool_msgs[:-KEEP_RECENT]:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 100:
            msg["content"] = "[cleared]"
```

**L2 — context_collapse**：零 API 成本的纯字符串操作。对较早的消息中的长文本（>2400 字符），保留头部 900 字符和尾部 500 字符，中间折叠。

```python
# agent/src/agent/loop.py
COLLAPSE_THRESHOLD = int(TOKEN_THRESHOLD * 0.7)  # 70%
COLLAPSE_HEAD = 900
COLLAPSE_TAIL = 500
COLLAPSE_TEXT_MIN = 2400

def _context_collapse(messages: list) -> None:
    for msg in messages[1:-COLLAPSE_PRESERVE_RECENT]:
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= COLLAPSE_TEXT_MIN:
            continue
        head = content[:COLLAPSE_HEAD]
        tail = content[-COLLAPSE_TAIL:]
        trimmed = len(content) - COLLAPSE_HEAD - COLLAPSE_TAIL
        msg["content"] = f"{head}\n\n...[{trimmed} chars collapsed]...\n\n{tail}"
```

**L3 — auto_compact**：真正的 LLM 压缩。使用结构化摘要模板（11 段：Goal / Constraints / Progress / Key Decisions / Resolved Questions / Pending User Asks / Relevant Files / Remaining Work / Critical Context / Tools & Patterns / Focus Topic），保留最后的 ~20000 token 作为"尾部"。

关键设计细节：
- **尾部保护**：不是按固定消息数保留，而是按 token 预算从后向前遍历，保留最近的一批消息直到累计达到 `TAIL_TOKEN_BUDGET`。
- **工具对修复**：压缩后会调用 `_fix_tool_pairs()` 修复孤立的 tool_call/tool_result 对——移除没有对应 tool_call 的 tool_result，为没有对应 tool_result 的 tool_call 插入 stub。
- **转录保存**：压缩前将完整转录保存到 `transcript_{timestamp}.jsonl`，确保信息不丢失。

```python
# agent/src/agent/loop.py — auto_compact 核心逻辑（简化）

def _auto_compact(self, messages, run_dir, trace, focus_topic="", iteration=0):
    # 1. 保存完整转录
    transcript_path = trace.dir_path / f"transcript_{int(time.time())}.jsonl"
    # ... 写入所有消息 ...

    # 2. Token-budget 尾部：从后向前遍历
    cut_idx = len(body)
    for i in range(len(body) - 1, -1, -1):
        msg_tokens = len(str(body[i].get("content", ""))) // 4 + 10
        if accumulated + msg_tokens > TAIL_TOKEN_BUDGET:  # 20000
            cut_idx = i + 1
            break
        accumulated += msg_tokens

    # 3. 不在 tool_call/tool_result 对中间切割
    while 0 < cut_idx < len(body) and body[cut_idx].get("role") == "tool":
        cut_idx += 1

    # 4. LLM 摘要（结构化模板 或 迭代更新）
    if self._previous_summary:
        prompt = _ITERATIVE_UPDATE_PROMPT.format(
            previous_summary=self._previous_summary,
            new_turns=conv_text,
        )
    else:
        prompt = _STRUCTURED_SUMMARY_PROMPT + conv_text

    summary = self.llm.chat([{"role": "user", "content": prompt}]).content
    self._previous_summary = summary

    # 5. 重建消息列表
    messages[:] = [system_msg,
                   {"role": "user", "content": f"[Compressed]\n\n{summary}\n\n<system>Continue from the summary above.</system>"},
                   *tail]

    # 6. 修复孤立的工具调用对
    _fix_tool_pairs(messages)
```

**L4 — compact 工具**：模型可以主动调用 `compact` 工具，AgentLoop 检测到后执行 L3 压缩。这让模型有能力在感知到上下文过长时主动请求压缩。

**L5 — iterative_update**：第 N 次压缩时使用 `_ITERATIVE_UPDATE_PROMPT`，将前次摘要和新对话片段拼接，要求 LLM 更新摘要而非从零开始。这保证了多次压缩之间的信息连续性，避免信息衰减。

### 2.3 工具执行策略

```
工具调用列表
    │
    ├── 按 is_readonly 属性分组
    │
    ├── 连续只读工具 → ThreadPoolExecutor(max_workers=8) 并行执行
    │   └── 每个工具独立心跳 + 进度发射器
    │
    └── 写工具 → 串行执行
        └── 超时告警（不强制终止，等待完成）
```

核心代码在 `_process_tool_calls()` → `_batch_execute()`：

```python
# agent/src/agent/loop.py
def _batch_execute(self, tool_calls, ...):
    batches = []
    current_ro = []
    for tc in tool_calls:
        tool_def = self.registry.get(tc.name)
        if tool_def and tool_def.is_readonly:
            current_ro.append(tc)
        else:
            if current_ro:
                batches.append(("parallel", current_ro))
                current_ro = []
            batches.append(("serial", [tc]))
    if current_ro:
        batches.append(("parallel", current_ro))

    for mode, batch in batches:
        if mode == "parallel" and len(batch) > 1:
            self._execute_parallel(batch, ...)
        else:
            for tc in batch:
                self._execute_single(tc, ...)
```

**工具去重**：同一个工具成功执行一次后被加入 `_called_ok` 集合。除非工具声明 `repeatable=True`，否则后续同名调用被阻止，返回 skip 消息。

**工具超时**：只读工具有硬超时（默认 1800s），超时后返回 `tool_timeout` error 并丢弃后续结果；写工具只告警不终止（因为无法安全中断）。

**流式重试**：LLM 流式调用遇到瞬态错误（如连接重置）自动重试一次；确定性的 4xx 错误立即失败。

### 2.4 ContextBuilder：系统提示词的渐进式构建

`ContextBuilder`（`agent/src/agent/context.py`）将系统提示词分为几层组装：

```
┌─ 角色框架：finance research agent with N skills, M tools, K data sources
├─ 工具描述：每个工具的 name + description + parameters（自动生成）
├─ 技能摘要：79 个技能的一行描述（progressive disclosure）
├─ 工作区状态：run_dir + tool counters
├─ 持久记忆：跨 session 的快照（冻结在 session 启动时以保持 prompt cache）
├─ 任务路由：Backtest / Swarm / Analysis / Document / Trade Journal / Shadow Account
├─ 指南：markdown table 优先、不使用 ---、文件路径相对于 run_dir
└─ 当前日期时间
```

关键设计：**记忆分为两层**
- **WorkspaceMemory**（`agent/src/agent/memory.py`）：运行时状态，单次 `run()` 存活。存 run_dir 和工具调用计数器，压缩时作为状态摘要注入。
- **PersistentMemory**（`agent/src/memory/persistent.py`）：跨 session 的持久记忆。快照在 session 启动时冻结到系统提示词中（不破坏 prompt cache），同时通过 `find_relevant()` 在每条用户消息前注入相关记忆。

---

## 3. LLM 供应商抽象

### 3.1 整体架构

```
ChatLLM (chat.py)          ← 统一的 ReAct 接口
    │
    └── ChatOpenAIWithReasoning (llm.py)  ← 扩展 LangChain ChatOpenAI
            │
            ├── ProviderCapabilities (capabilities.py)  ← 每供应商的差异声明
            │
            └── 13+ 供应商：
                openai / openrouter / deepseek / gemini / groq /
                dashscope(qwen) / zhipu(glm) / moonshot(kimi) /
                minimax / mimo / z.ai / ollama / openai-codex
```

### 3.2 ProviderCapabilities：声明式能力矩阵

`agent/src/providers/capabilities.py` 定义了一个 `ProviderCapabilities` frozen dataclass，声明每个供应商+模型的特性：

```python
# agent/src/providers/capabilities.py

@dataclass(frozen=True)
class ProviderCapabilities:
    name: str                          # 规范名称
    api_key_env: Optional[str]         # API Key 环境变量
    base_url_env: str                  # Base URL 环境变量
    capture_reasoning: bool = False    # 是否保存 reasoning_content
    send_reasoning_content: bool = False # 是否在历史中回放 reasoning_content
    gemini_thought_signatures: bool = False  # Gemini thought_signature 回放
    normalize_assistant_content: bool = False # content=None → ""
    openrouter_reasoning_body: bool = False   # OpenRouter extra_body.reasoning
    default_headers: Mapping[str, str] = {}   # 默认请求头
    native_adapter_package: Optional[str] = None  # 原生适配器包名
```

每个供应商的具体配置示例：

```python
_PROVIDERS = {
    "openai": ProviderCapabilities("openai", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "openrouter": ProviderCapabilities(
        "openrouter", ..., capture_reasoning=True, openrouter_reasoning_body=True,
    ),
    "deepseek": ProviderCapabilities(
        "deepseek", ..., capture_reasoning=True,
        native_adapter_package="langchain-deepseek",
    ),
    "gemini": ProviderCapabilities(
        "gemini", ..., gemini_thought_signatures=True,
    ),
    "moonshot": ProviderCapabilities(
        "moonshot", ..., capture_reasoning=True, send_reasoning_content=True,
        normalize_assistant_content=True,
        default_headers={"User-Agent": "Vibe-Trading/0.1.10"},
    ),
    # ...
}
```

**模型名推断**：当 provider 为 `openai`（默认）时，通过 `_infer_from_model()` 从模型名推断实际供应商：
- `gemini*` → gemini
- `deepseek*` → deepseek
- `glm*` → zhipu
- 含 `kimi` 或 `moonshot` → moonshot

### 3.3 ChatOpenAIWithReasoning：reasoning 字段的统一处理

`agent/src/providers/llm.py` 扩展了 LangChain 的 `ChatOpenAI`，在三个路径上处理非标准 reasoning 字段：

1. **入站**：`_convert_dict_to_message` / `_convert_delta_to_message_chunk` → 将 `reasoning_content` / `reasoning` 归一化到 `additional_kwargs["reasoning_content"]`
2. **出站**：`_convert_message_to_dict` → 将 `reasoning_content` 回注到请求体（Kim K2.6 等严格供应商要求多轮历史中包含此字段）
3. **Gemini thought_signature**：从 tool_call 的 `extra_content.google.thought_signature` 提取，在下一轮请求中回放

### 3.4 ChatLLM：统一的流式接口

```python
# agent/src/providers/chat.py

@dataclass
class LLMResponse:
    content: Optional[str] = None
    tool_calls: List[ToolCallRequest] = field(default_factory=list)
    reasoning_content: Optional[str] = None
    finish_reason: str = "stop"
    usage_metadata: Optional[Dict[str, int]] = None
    content_filter_triggered: bool = False

class ChatLLM:
    def stream_chat(self, messages, tools=None, on_text_chunk=None,
                    on_reasoning_chunk=None, should_cancel=None) -> LLMResponse:
        # 流式调用 LLM，处理 streaming chunks → 聚合为 LLMResponse
        ...

    def chat(self, messages) -> LLMResponse:
        # 非流式调用（用于 auto_compact 的摘要请求）
        ...
```

---

## 4. 工具体系

### 4.1 BaseTool + ToolRegistry

```python
# agent/src/agent/tools.py

class BaseTool(ABC):
    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}     # JSON Schema
    repeatable: bool = False            # 是否允许重复调用
    is_readonly: bool = True            # 是否只读（决定并行/串行）

    @classmethod
    def check_available(cls) -> bool:   # 依赖检查（API key 等）
        return True

    @abstractmethod
    def execute(self, **kwargs) -> str:  # 返回 JSON string
        ...

    def to_openai_schema(self) -> Dict:  # → OpenAI function calling 格式
        ...

class ToolRegistry:
    def register(self, tool: BaseTool) -> None: ...
    def get(self, name: str) -> Optional[BaseTool]: ...
    def get_definitions(self) -> List[Dict]: ...  # 所有工具的 OpenAI schema
    def execute(self, name: str, params: Dict) -> str: ...  # 自动包装错误为 JSON
```

### 4.2 工具全景（54 个 MCP 工具）

| 类别 | 工具 | 依赖 |
|------|------|------|
| **市场数据** | `get_market_data`, `get_stock_profile`, `search_symbol` | 免费 |
| **A 股专项** | `get_fund_flow`, `get_dragon_tiger`, `get_northbound_flow`, `get_margin_trading`, `get_block_trades`, `get_shareholder_count`, `get_lockup_expiry`, `get_sector_info`, `get_research_reports`, `get_stock_news`, `screen_market` | Tushare token |
| **财务数据** | `get_financial_statements`, `get_sec_filings` | 部分免费 |
| **期权** | `get_options_chain`, `analyze_options` | 免费 |
| **宏观** | `get_macro_series` | FRED_API_KEY |
| **因子** | `factor_analysis`, `alpha_bench`, `alpha_compare` | 免费 |
| **回测** | `backtest` | 免费* |
| **文档/网络** | `read_document`, `read_url`, `web_search` | 免费 |
| **文件** | `write_file`, `read_file`, `edit_file` | 免费 |
| **技能** | `list_skills`, `load_skill`, `save_skill`, `patch_skill` | 免费 |
| **交易日志** | `analyze_trade_journal` | 免费 |
| **Shadow Account** | `extract_shadow_strategy`, `run_shadow_backtest`, `render_shadow_report`, `scan_shadow_signals` | 免费 |
| **Swarm** | `list_swarm_presets`, `run_swarm`, `get_swarm_status`, `get_run_result`, `list_runs`, `reap_stale_runs`, `retry_run` | LLM key |
| **交易接口** | `trading_connections`, `trading_select_connection`, `trading_check`, `trading_account`, `trading_positions`, `trading_orders`, `trading_quote`, `trading_history` | 券商 OAuth |
| **其他** | `compact`, `remember`, `bash`, `background_*`, `session_search` | - |

### 4.3 工具注册的依赖检查

每个工具通过 `check_available()` 声明依赖。例如 `get_macro_series` 需要 `FRED_API_KEY`，如果环境变量不存在则工具不注册。这保证了 LLM 看到的工具列表始终是实际可用的。

---

## 5. Swarm 多智能体系统

### 5.1 架构概览

```
SwarmRuntime (runtime.py)     ← DAG 拓扑调度
    │
    ├── SwarmStore (store.py)  ← 运行状态持久化（文件系统）
    ├── TaskStore (task_store.py) ← DAG 验证 + 拓扑排序
    │
    ├── Presets (presets.py)   ← YAML 预设加载
    │   └── presets/*.yaml     ← 29 个预设团队定义
    │
    └── Worker (worker.py)     ← 单 Worker 的轻量 ReAct 循环
        ├── build_worker_prompt() ← 角色 + 上游上下文 + 技能
        ├── grounding          ← 注入已验证的市场快照
        └── 输出合约检查        ← _classify_deliverable()
```

### 5.2 DAG 拓扑调度

每个 Swarm 运行是一个 DAG：Agent（节点）有依赖关系（边）。`SwarmRuntime.start_run()` 执行流程：

1. **加载预设** YAML → 构建 `SwarmRun` 和 `SwarmTask` 列表
2. **DAG 验证** → `validate_dag()` 检查循环依赖
3. **拓扑分层** → `topological_layers()` 将任务按依赖关系分层
4. **逐层执行**：
   - 层内任务并行执行（ThreadPoolExecutor，max_workers=4）
   - 层间串行等待
   - 上游任务失败时，下游任务被阻塞（`blocks_downstream`）

```python
# agent/src/swarm/runtime.py — 核心调度逻辑（简化）

def _execute_run(self, run, live_callback):
    layers = topological_layers(run.tasks)
    
    for layer in layers:
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {}
            for task in layer:
                # 收集上游摘要
                upstream_summaries = {
                    dep_key: run.get_task_summary(dep_id)
                    for dep_key, dep_id in task.input_from.items()
                }
                # 提交 Worker
                fut = pool.submit(
                    run_worker,
                    agent_spec=run.get_agent_spec(task.agent_id),
                    task=task,
                    upstream_summaries=upstream_summaries,
                    user_vars=run.user_vars,
                    run_dir=run.run_dir,
                    event_callback=live_callback,
                )
                futures[fut] = task
            
            # 收集结果，更新任务状态
            for fut in as_completed(futures):
                result = fut.result()
                task = futures[fut]
                run.update_task(task.id, result)
```

### 5.3 Worker：轻量 ReAct 循环

Worker（`agent/src/swarm/worker.py`）是 Swarm 的执行单元。与主 AgentLoop 的区别：

| 特性 | AgentLoop | Swarm Worker |
|------|-----------|-------------|
| 上下文压缩 | 5 层 | 1 层（microcompact） |
| 工具注册 | 完整 54 工具 | 按 agent_spec.tools 白名单过滤 |
| 迭代预算 | 50（默认） | 20 工具调用硬限制 |
| 提示词结构 | 通用任务路由 | 角色特有 + 上游上下文 + grounding |
| 输出合约 | 无强制要求 | 必须写 report.md + 内容完整性检查 |

Worker 的输出合约检查（`_classify_deliverable()`）覆盖 6 种失败模式：
- 空输出
- 未解析的 tool-call 标记
- 显式伪数据声明
- 原始 tool-result 信封
- 仅计划无执行
- 数据 Agent 无工具调用且无 report.md

### 5.4 预设模板系统

29 个 YAML 预设文件在 `agent/src/swarm/presets/`。典型结构：

```yaml
# 以 investment_committee.yaml 为例（简化）
name: investment_committee
title: Investment Committee
description: Bull/bear debate → risk review → PM decision

variables:
  - name: target
    description: Ticker to analyze (e.g. NVDA.US)
  - name: market
    description: Market (US, HK, ChinaA, Crypto)

agents:
  - id: bull
    role: Bull Researcher
    model_name: ${LANGCHAIN_MODEL_NAME}
    tools: [get_market_data, get_stock_news, get_financial_statements,
            load_skill, web_search, read_url, write_file, read_file]
    skills: [bull-case-analysis, growth-investing, valuation-multiples]
    system_prompt: |
      You are a Bull Researcher...
      {upstream_context}

  - id: bear
    role: Bear Researcher
    # ...

  - id: manager
    role: Research Manager
    input_from:
      bull_context: bull
      bear_context: bear
    # ...
```

`build_worker_prompt()` 将预设拼接成完整的 Worker 提示词：
```
## Role
{agent_spec.role}

{agent_spec.system_prompt}  ← 含 {upstream_context} 占位符

## Available Skills
{filtered skill descriptions}

## Ground Truth                   ← grounding 模块注入
{verified market snapshot}

## Market Data Tool Policy       ← 如果工具有 get_market_data
...

## Data Citation Discipline (HARD RULE)  ← 反幻觉规则
...

## Execution Rules               ← 3 阶段工作流
...

## Current Date & Time
...
```

### 5.5 Grounding：反幻觉机制

`agent/src/swarm/grounding.py` 在 Swarm 启动时为每个涉及的 symbol 预取 OHLCV 数据，格式化为 "Ground Truth" markdown 块注入每个 Worker 的系统提示词。这确保所有 Worker 在同一份权威价格数据基础上工作。

---

## 6. 数据架构

### 6.1 18 个数据源

```
加载器注册表 (backtest/loaders/registry.py)
    │
    ├── A 股：tushare, akshare, baostock, tencent, sina, eastmoney, mootdx
    ├── 港股：yfinance, futu
    ├── 美股：yfinance, stooq, yahoo, finnhub*, alphavantage*, tiingo*, fmp*
    ├── 期货：akshare
    ├── 加密：okx, ccxt (100+ 交易所)
    ├── 宏观：fred*
    └── 本地：local (CSV / Parquet / DuckDB)
    
    * 需要 API key
```

### 6.2 有序 Fallback 链

每个数据请求按配置的优先级链尝试多个数据源。失败分类：
- `VendorRateLimitError` → 跳过，尝试下一个
- `NoMarketDataError` → 跳过（带警告）
- 其他 `Exception` → 跳过（记录日志）

### 6.3 OHLC 完整性守卫

在加载器边界（`backtest/loaders/`）集中检查每条 K 线的完整性：
- `high < low` → 丢弃
- 非正价格 → 丢弃
- 开高低收区间越界 → 丢弃

这保证了所有下游消费者（回测引擎、因子计算、图表渲染）不会拿到脏数据。

### 6.4 本地数据缓存

`VIBE_TRADING_DATA_CACHE=1` 启用后，所有已结算的历史 K 线缓存到 `~/.vibe-trading/cache`。缓存命中时跳过网络请求。今日数据永不缓存（最后一根 K 线仍在形成）。

---

## 7. 安全设计

### 7.1 多层安全边界

```
┌─────────────────────────────────────────┐
│ 接入安全                                  │
│ • API_AUTH_KEY 认证（远程部署必需）         │
│ • CSRF 保护（同源检查 + CORS 白名单）       │
│ • 路径遍历防护（safe_path / safe_ticker）   │
│ • 上传大小限制（50MB，流式接收）             │
├─────────────────────────────────────────┤
│ 执行安全                                  │
│ • 生成代码在独立子进程中执行                 │
│ • 白名单环境变量（不含 LLM/券商/交易凭据）     │
│ • Shell 工具需显式环境变量启用              │
│ • 文件工具沙箱（隔离的读/写根目录）          │
├─────────────────────────────────────────┤
│ 交易安全                                  │
│ • 用户承诺的 mandate（标的/仓位/杠杆/日上限） │
│ • 文件系统 kill switch（即时停止所有交易）    │
│ • 预交易门（fail-closed：异常时拒绝）        │
│ • 完整审计账本（所有订单意图记录）            │
│ • mandate 自动过期                         │
│ • 无结构性 paper/live 区分的券商硬限制 paper  │
└─────────────────────────────────────────┘
```

### 7.2 交易风控：Mandate + Kill Switch

交易执行必须通过两层防护：

**Mandate**：用户在启动前手动编辑的 YAML 文件，定义：
- 允许的标的范围（symbol universe）
- 单笔最大订单金额
- 总敞口上限
- 杠杆上限
- 每日交易次数上限

**Kill Switch**：文件系统级别的即时停止开关。agent 在每次交易前检查文件是否存在，如果存在则拒绝执行并清除所有未成交订单。

### 7.3 生成代码的沙箱执行

`Runner._build_runtime_env()`（`agent/src/core/runner.py`）构建一个白名单环境变量集合，只包含：
- OS/Python 基础变量（PATH, HOME, TMPDIR, PYTHONPATH...）
- 代理/证书设置（HTTP_PROXY, SSL_CERT_FILE...）
- 只读市场数据配置（TUSHARE_TOKEN, FINNHUB_API_KEY...）

显式排除：LLM API keys、券商凭据、交易配置、顾问接口配置。

---

## 8. 关键子系统

### 8.1 Alpha Zoo（452 个因子）

```
agent/src/factors/
├── base.py          # 因子计算核心算子（rank, scale, ts_rank, ts_corr...）
├── _backend.py      # bottleneck/NumPy 加速
├── registry.py      # 因子注册表
├── bench_runner.py  # 批量基准测试
├── bench_runner_strict.py  # 严格基准（随机对照 + OOS 分割）
├── compare_runner.py # 因子对比
└── zoo/
    ├── qlib158/     # 154 个因子（Microsoft Qlib Alpha158）
    ├── alpha101/    # 101 个因子（Kakushadze 2015）
    ├── gtja191/     # 191 个因子（国泰君安 2014）
    └── academic/    # 6 个因子（Fama-French 5 + Carhart）
```

每个因子是一个独立的 `.py` 模块，遵循 `AlphaCompute` 协议：
- 输入：`dict[str, pd.DataFrame]`（panel，key 为 open/high/low/close/volume/vwap/returns 等）
- 输出：`pd.DataFrame`（index=日期，columns=股票代码，values=因子值）
- 前视偏差守卫：禁止 `Ref(df, -n)` 负偏移
- NaN 传播：不静默 `fillna(0)`

### 8.2 Shadow Account（旗舰功能）

```
交易日志 CSV
    │
    ├── (1) analyze_trade_journal
    │   └── 解析 → FIFO 配对 → 行为画像（持仓天数/胜率/处置效应/追涨/过度交易/锚定）
    │
    ├── (2) extract_shadow_strategy
    │   └── 盈利往返 → 特征工程 → KMeans 聚类(k=2-5) → 决策树(max_depth=3)
    │       → 路径提取 → ShadowRule 对象（3-5 条 if-then 规则）
    │
    ├── (3) run_shadow_backtest
    │   └── 跨市场回测（A 股/港股/美股/加密）→ δ-PnL 归因
    │
    ├── (4) render_shadow_report
    │   └── 8 节 HTML/PDF 报告 + 图表
    │
    └── (5) scan_shadow_signals
        └── 今日匹配 Shadow 入场节奏的标的（仅供研究）
```

提取算法的核心思路是：**用 KMeans 将盈利交易聚类，再用浅层决策树从每个簇中提取可解释的规则**。价格上下文特征（entry_rsi14, prior_5d_return）是可选增强，缺失时降级为 NaN。

### 8.3 技能系统（79 个技能）

`SkillsLoader`（`agent/src/agent/skills.py`）使用**渐进式披露**：
- 系统提示词只注入一行摘要（`get_descriptions()`）
- 完整文档通过 `load_skill(name)` 工具按需加载
- 每个技能是一个目录，包含 `SKILL.md`（frontmatter + body）+ 可选的支持文件

技能覆盖：技术分析（K 线、波浪、一目均衡、SMC、谐波、缠论）、量化方法（因子研究、ML 策略、配对交易）、风险管理（VaR/CVaR、压力测试、对冲）、期权（BS、Greeks、多腿策略）、加密（资金费率、清算热力图、稳定币流）、行为金融、交易日志诊断等。

### 8.4 回测系统（7 个引擎）

```
backtest/
├── runner.py         # 回测 Runner：配置验证 → 数据加载 → 引擎路由 → 结果输出
├── engines/
│   ├── china_a.py    # A 股（涨跌停、T+1、印花税、ST 过滤）
│   ├── global_equity.py  # 全球股票（多货币、分红调整）
│   ├── crypto.py     # 加密货币（24/7、资金费率）
│   ├── china_futures.py  # 中国期货（保证金、交割）
│   ├── global_futures.py # 全球期货
│   ├── forex.py      # 外汇
│   └── options.py    # 期权
├── loaders/          # 18 个数据加载器 + 注册表
├── metrics.py        # 指标计算（Sharpe, MaxDD, Win Rate...）
├── benchmark.py      # 基准对比面板
├── correlation.py    # 相关性热力图
├── validation.py     # 蒙特卡洛 + Bootstrap CI + Walk-Forward
└── run_card.py       # 运行卡片（JSON + Markdown）
```

回测引擎是**向量化的**（非事件驱动），直接接收 `SignalEngine` 产生的信号 DataFrame，计算持仓、权益曲线和指标。

### 8.5 交易接口层（10+ 券商）

```
trading_connector_tool.py  ← 统一的 MCP 工具入口
    │
    ├── IBKR (TWS/Gateway 本地 + 官方 MCP OAuth)
    ├── Robinhood (MCP OAuth, 有界自主交易)
    ├── Tiger / Longbridge / Alpaca / OKX / Binance / Futu / Dhan / Shoonya
    │
    └── 统一抽象：
        ├── connector list/use/check
        ├── account / positions / orders / quote / history（读）
        └── place_order / cancel_order（写，需 mandate + kill switch）
```

---

## 9. 设计决策评注

### 9.1 做得好的设计

**1. 5 层级联压缩** — 每个层级解决不同的问题（内存压力 vs 上下文溢出 vs 信息衰减），且触发阈值递增。L2 零 API 成本、L3 尾部预算保护、L5 迭代更新防止信息衰减，设计非常精细。

**2. ProviderCapabilities 能力矩阵** — 将 13+ 供应商的差异声明为 frozen dataclass，而非散落在 if-else 中。模型名推断让用户可以用 `openai` provider + `gemini-3-flash` model 的组合，自动路由到正确的适配逻辑。

**3. Worker 输出合约检查** — `_classify_deliverable()` 覆盖 6 种静默失败模式，包括"Agent 说它没真实数据"、"只写计划不执行"、"返回 tool-result 信封而非分析"等。这在多 Agent 系统中至关重要——上游 Worker 的静默失败会导致下游 Worker 基于空/假数据工作。

**4. 工具去重机制** — `_called_ok` 集合 + `repeatable` 标志。防止 Agent 陷入重复调用同一工具的循环，同时允许合理场景（如多次 `web_search`）。

**5. 记忆的两层设计** — WorkspaceMemory（运行时，参与压缩）vs PersistentMemory（跨 session，快照冻结在 session 启动时保持 prompt cache，同时按需注入相关记忆到用户消息）。

**6. 生成代码的沙箱** — 白名单环境变量 + 独立子进程 + 超时。不信任 LLM 生成的任何代码。

**7. 交易安全的纵深防御** — Mandate（事前承诺）+ Kill switch（事中停止）+ Audit ledger（事后审计）+ 结构性 paper/live 区分。

**8. grounding 反幻觉** — 在 Swarm 启动时预取真实价格数据，注入每个 Worker 提示词，要求所有数字必须可追溯到工具调用结果或 grounding 块。

### 9.2 值得讨论的设计选择

**1. 自研 ReAct vs LangGraph** — Vibe-Trading 选择自研，获得了对流式、心跳、进度事件的完全控制，但失去了 LangGraph 的 checkpoint 断点续跑、声明式图的可视化和调试能力。对于这个项目的需求（CLI/Web UI 实时交互）来说，自研是正确的。

**2. 无 checkpoint 续跑** — `AgentLoop.run()` 每次独立运行，不支持 LangGraph 式的中断恢复。对于长时间运行的 Swarm 任务，这由 Swarm 层面的重试（`retry_run`）和文件系统持久化弥补。

**3. Worker 使用非流式 chat() 用于工具执行** — Swarm Worker 使用 `ChatLLM.chat()`（非流式）执行工具调用，只有主循环使用 `stream_chat()`。这是合理的——Worker 工具调用只是中间步骤，不需要实时流式给用户。

**4. 提示词中的任务路由** — 系统提示词包含 ~60 行的任务路由指南（Backtest/Swarm/Analysis/Document/Trade Journal/Shadow Account），这增加了 prompt 长度但减少了无效的工具调用探索。

**5. YAML 预设 vs 代码定义** — 29 个 Swarm 预设用 YAML 而非 Python 代码定义，降低了创建新团队的难度，但 YAML 的模板变量系统（`{target}`, `{market}` 等）比较原始，不支持条件逻辑。

**6. 技能系统无版本管理** — 技能通过 `save_skill` / `patch_skill` 修改，但没有内置的版本管理或回滚机制。

### 9.3 扩展点

1. **Swarm Worker 的上下文压缩** — 当前只有 L1（microcompact），可以加入 L2（context_collapse）来支持更长时间的 Worker 运行
2. **回测引擎的事件驱动模式** — 当前所有引擎都是向量化的，未来可以添加事件驱动引擎以支持更复杂的交易逻辑（止损、移动止盈）
3. **技能版本管理** — 为 `save_skill` / `patch_skill` 添加版本历史和回滚
4. **AgentLoop checkpoint** — 在关键点（每次压缩后、工具执行后）添加可选的 checkpoint，支持中断恢复
5. **跨 session 的工作区恢复** — 当前每次 `run()` 创建新的 `run_dir`，无法在 CLI 重启后恢复之前的工作区
