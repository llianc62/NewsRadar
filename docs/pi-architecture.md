# Pi Agent Harness — 技术架构文档

> 项目地址：https://github.com/earendil-works/pi
> 分析日期：2026-07-21

## 概述

Pi 是一个**自扩展编码 Agent 运行时**（self-extensible coding agent），提供从底层 LLM 统一调用、到 Agent 循环、到会话管理、到 TUI 交互的全栈能力。整个项目是 TypeScript monorepo，包含 6 个核心包和 4 个示例扩展包。

---

## 包结构

```
pi/
├── packages/
│   ├── ai/              # 统一多厂商 LLM API 层
│   ├── agent/           # Agent 运行时 + 工具调用 + 会话管理（通用，无 TUI 依赖）
│   ├── coding-agent/    # 交互式编码 Agent CLI（面向最终用户）
│   ├── orchestrator/    # 多 Agent 实例生命周期管理（Supervisor）
│   └── tui/             # 终端 UI 库（差分渲染）
├── scripts/             # 构建/发布/检查脚本
└── .pi/                 # 本地配置目录（extensions/ · skills/ · prompts/ · git/ · npm/）
```

---

## 分层架构

### 1. `@earendil-works/pi-ai` — 统一 LLM API 层

**职责：** 屏蔽多厂商 API 差异，提供统一的流式接口。

**架构模式：** 按厂商分模块，通过 `ProviderStreams` 接口统一暴露。

```
src/api/
├── openai-responses.ts          # OpenAI Responses API
├── openai-completions.ts        # OpenAI Completions API
├── openai-codex-responses.ts    # OpenAI Codex API
├── anthropic-messages.ts        # Anthropic Messages API
├── google-generative-ai.ts      # Google Gemini API
├── google-vertex.ts             # Google Vertex AI
├── bedrock-converse-stream.ts   # AWS Bedrock
├── mistral-conversations.ts     # Mistral AI
├── azure-openai-responses.ts    # Azure OpenAI
├── cloudflare.ts                # Cloudflare Workers AI
├── pi-messages.ts               # Pi 自研协议
├── openrouter-images.ts         # OpenRouter 图片生成
├── github-copilot-headers.ts    # GitHub Copilot 认证
├── lazy.ts                      # 延迟加载包装器
└── simple-options.ts            # 简化选项
```

**核心接口：**

```typescript
// 每个 API 模块导出 stream 和 streamSimple 两个函数
interface ProviderStreams {
  stream(model, context, options?): AssistantMessageEventStream;
  streamSimple(model, context, options?): AssistantMessageEventStream;
}
```

**`AssistantMessageEventStream`** 是流式事件协议，支持：
- `start` / `text_delta` / `text_end` / `toolcall_delta` / `toolcall_end` 等增量事件
- `done` / `error` 终止事件
- `partial` 字段携带当前的完整 `AssistantMessage` 快照

**模型系统** 通过 `Model<TApi>` 泛型类型关联：

```typescript
interface Model<TApi extends Api> {
  id: string;           // 模型标识
  api: TApi;            // API 类型（"openai-completions" | "anthropic-messages" | ...）
  provider: ProviderId; // 提供商（"openai" | "anthropic" | ...）
  baseUrl: string;      // API 端点
  reasoning: boolean;   // 是否支持推理
  thinkingLevelMap?: ThinkingLevelMap;  // 推理级别映射
  cost: ModelCost;      // 定价信息
  contextWindow: number; // 上下文窗口
  maxTokens: number;     // 最大输出
  compat?: OpenAICompletionsCompat | OpenAIResponsesCompat | AnthropicMessagesCompat;
}
```

**认证系统** 支持多方式：
- API Key（环境变量 + 配置文件）
- OAuth 设备流（GitHub Copilot 等）
- 凭据存储（`AuthStorage` + `FileAuthStorageBackend`）

---

### 2. `@earendil-works/pi-agent-core` — Agent 运行时

**职责：** Agent 循环、工具调用、事件系统、会话管理、上下文压缩、分支管理。

#### 核心类型

```typescript
// Agent 消息系统（扩展自 LLM 的 Message）
type AgentMessage = Message | CustomAgentMessages[keyof CustomAgentMessages];

// 消息类型
UserMessage     = { role: "user", content: TextContent[] | ImageContent[], timestamp }
AssistantMessage = { role: "assistant", content: (TextContent | ThinkingContent | ToolCall)[], usage, stopReason, ... }
ToolResultMessage = { role: "toolResult", toolCallId, toolName, content, isError, timestamp }
```

#### Agent 循环

由 `agent-loop.ts` 中的 `runAgentLoop()` 驱动，流程：

```
agentLoop(prompts, context, config, signal)
  │
  ├─ emit agent_start
  ├─ 将 prompts 加入 context.messages
  ├─ 进入循环:
  │   ├─ convertToLlm(messages) → Message[]
  │   ├─ transformContext(messages) → 可选上下文裁剪
  │   ├─ streamFn(model, context, options) → 调 LLM
  │   ├─ emit turn_start
  │   ├─ 逐 token 接收事件流:
  │   │   ├─ text_start → text_delta → text_end
  │   │   ├─ toolcall_start → toolcall_delta → toolcall_end
  │   │   └─ 每个事件 emit message_update
  │   ├─ emit message_end
  │   ├─ 执行工具调用（sequential / parallel 两种模式）
  │   │   ├─ beforeToolCall hook → 可拦截
  │   │   ├─ 执行工具
  │   │   ├─ afterToolCall hook → 可改写结果
  │   │   └─ 工具结果加入 context
  │   ├─ emit turn_end
  │   ├─ shouldStopAfterTurn? → 可提前终止
  │   ├─ getSteeringMessages? → 注入引导消息
  │   ├─ prepareNextTurn? → 可切换模型/上下文
  │   └─ 循环直到 shouldStop 或 LLM 返回文本
  │
  └─ emit agent_end(messages)
```

**AgentLoopConfig 关键配置：**

| 配置项 | 说明 |
|--------|------|
| `model` | 使用的 LLM 模型 |
| `convertToLlm` | AgentMessage → Message 转换器 |
| `transformContext` | 上下文窗口管理（裁剪/压缩） |
| `toolExecution` | 工具执行模式：`"sequential"` / `"parallel"` |
| `beforeToolCall` | 工具执行前拦截（权限/参数校验） |
| `afterToolCall` | 工具执行后改写结果 |
| `shouldStopAfterTurn` | 每轮后判断是否停止 |
| `getSteeringMessages` | 获取引导消息 |
| `getFollowUpMessages` | 获取后续消息 |
| `prepareNextTurn` | 准备下一轮（可切换模型/上下文） |

#### Agent 状态管理

`AgentState` 接口暴露全部运行时状态：

```typescript
interface AgentState {
  systemPrompt: string;
  model: Model<any>;
  thinkingLevel: ThinkingLevel;
  tools: AgentTool<any>[];
  messages: AgentMessage[];
  readonly isStreaming: boolean;
  readonly streamingMessage?: AgentMessage;
  readonly pendingToolCalls: ReadonlySet<string>;
  readonly errorMessage?: string;
}
```

#### 事件系统

Agent 发射丰富的事件供 UI 订阅：

```
AgentEvent =
  | agent_start / agent_end             // Agent 生命周期
  | turn_start / turn_end               // 每轮 LLM 调用
  | message_start / message_update / message_end  // 消息生命周期
  | tool_execution_start / _update / _end  // 工具执行生命周期
```

#### 工具系统

```typescript
interface AgentTool<TParameters, TDetails> {
  name: string;
  description: string;
  parameters: TSchema;
  label: string;          // 前端显示名
  prepareArguments?: (args) => Static<TParameters>;  // 参数兼容
  execute: (toolCallId, params, signal?, onUpdate?) => Promise<AgentToolResult<TDetails>>;
  executionMode?: "sequential" | "parallel";
}
```

工具结果支持：
- `content` — 返回给 LLM 的文本/图片内容
- `details` — 结构化数据（供 UI 渲染）
- `addedToolNames` — 动态添加工具
- `terminate` — 提示终止当前批次

---

### 3. 会话管理（`@earendil-works/pi-agent-core`）

**职责：** 持久化 Agent 对话历史，支持分支、回退、压缩。

#### 会话数据模型

每条会话存储为 `SessionTreeEntry` 树：

```typescript
type SessionTreeEntry =
  | MessageEntry           // 用户/助手/工具消息
  | ThinkingLevelChangeEntry  // 推理级别变更
  | ModelChangeEntry       // 模型切换
  | ActiveToolsChangeEntry // 工具切换
  | CompactionEntry        // 上下文压缩
  | BranchSummaryEntry     // 分支摘要
  | CustomEntry            // 自定义条目
  | CustomMessageEntry     // 自定义消息
  | LabelEntry             // 标签
  | SessionInfoEntry       // 会话元信息
  | LeafEntry              // 分支叶子指针
```

#### 存储后端

两种内置实现：

| 后端 | 存储方式 | 适合场景 |
|------|---------|---------|
| `JsonlSessionStorage` | JSONL 文件（每行一个 JSON 对象） | 本地持久化 |
| `MemorySessionStorage` | 内存 Map | 测试 |

`JsonlSessionStorage` 文件格式：

```
{"type":"session","version":3,"id":"...","timestamp":"...","cwd":"..."}
{"type":"message","id":"...","parentId":null,"timestamp":"...","message":{...}}
{"type":"message","id":"...","parentId":"...","timestamp":"...","message":{...}}
{"type":"compaction","id":"...","parentId":"...","timestamp":"...","summary":"...","tokensBefore":1000}
```

#### 上下文构建

`Session.buildContext()` 将树形条目解析为线性 `AgentMessage[]`：

1. `defaultContextEntryTransform` — 找到最近的 `CompactionEntry`，用它代替被压缩的历史
2. `sessionEntryToContextMessages` — 将每条 `SessionTreeEntry` 转为 `AgentMessage`
3. 支持自定义的 `entryTransforms` 和 `entryProjectors` 扩展

#### 上下文压缩

`compact()` 函数实现智能上下文窗口管理：

- `shouldCompact()` — 基于 token 估计判断是否需要压缩
- `prepareCompaction()` — 准备压缩素材（要压缩的消息、文件操作、保留边界）
- `compact()` — 用 LLM 生成摘要，替换旧消息
- `generateSummary()` — 调用 LLM 生成压缩摘要
- 支持 `CompactionDetails` 记录文件操作状态

#### 分支管理

基于 `SessionTreeEntry` 的 `parentId` 形成树形结构：

- `Session.moveTo(entryId)` — 切换到某个条目（创建分支）
- `SessionRepo.fork()` — 从某个条目分叉出新会话
- `buildBranchSummary()` — 生成分支摘要
- 每个会话维护 `leafId` 指针指向当前叶子

---

### 4. `@earendil-works/pi-coding-agent` — 编码 Agent CLI

**职责：** 面向最终用户的交互式编码助手，提供 CLI、TUI、RPC 三种运行模式。

#### 运行模式

| 模式 | 入口 | 说明 |
|------|------|------|
| 交互式 TUI | `pi` 无参数 | 全屏终端 UI，差分渲染 |
| 文本模式 | `pi --mode text` | 一次性问答，输出 markdown |
| JSON 模式 | `pi --mode json` | 事件流输出 JSON |
| RPC 模式 | `pi --mode rpc` | JSON-RPC over stdio，供 orchestrator 管理 |

#### 核心架构

`AgentSession` 类封装整个 Agent 生命周期：

```
AgentSession
├── Agent 实例（@earendil-works/pi-agent-core）
├── 会话持久化（JSONL 文件）
├── 扩展系统（ExtensionRunner）
├── Bash 执行器（bash-executor）
├── 模型管理（模型切换、推理级别）
├── 压缩管理（自动/手动压缩）
├── 会话切换（切换/分支/克隆）
└── 事件总线（EventEmitter → 各模式 I/O 层）
```

---

## ⭐ 扩展系统深度解析（核心亮点）

Pi 的扩展系统是其"自扩展"能力的关键。这是一个**事件驱动 + 插件注册**的架构，允许 TypeScript 模块以声明式方式接入 Agent 的所有生命周期。

### 5.1 架构总览

```
                  ┌─────────────────────────────────────────┐
                  │           ExtensionLoader               │
                  │  (jiti 动态加载 .ts / .js 模块)          │
                  └──────────┬──────────────────────────────┘
                             │ 加载
                             ▼
┌──────────────────────────────────────────────────────────────┐
│                      ExtensionRunner                          │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │  Extension 1  │  │  Extension 2  │  │  Extension N       │  │
│  │  handlers:[ ] │  │  handlers:[ ] │  │  handlers:[ ]      │  │
│  │  tools:[ ]    │  │  tools:[ ]    │  │  tools:[ ]         │  │
│  │  commands:[ ] │  │  commands:[ ] │  │  commands:[ ]      │  │
│  │  shortcuts:[ ]│  │  shortcuts:[ ]│  │  shortcuts:[ ]     │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬──────────────┘  │
│         │                 │                  │                 │
│         └────────┬────────┴─────────┬────────┘                 │
│                  ▼                  ▼                          │
│         ┌─────────────────────────────────────┐                │
│         │        ExtensionRuntime              │                │
│         │  (共享运行时: flagValues, 动作代理)    │                │
│         └─────────────────────────────────────┘                │
└──────────────────────────────────────────────────────────────┘
                             │
                             ▼
              ┌─────────────────────────────┐
              │     AgentSession / Agent    │
              │  (事件循环中调用 runner)      │
              └─────────────────────────────┘
```

### 5.2 加载流程

Pi 在三个位置发现扩展，按优先级合并去重：

```
1. 项目本地:  cwd/.pi/extensions/*.ts
2. 全局:      ~/.pi/agent/extensions/*.ts
3. 显式配置:  命令行 --extensions 或配置文件中指定路径
```

**发现规则（`discoverExtensionsInDir`）：**

```
extensions/
├── hello.ts              → 直接加载（单文件）
├── subagent/             → 子目录，检测 index.ts 或 package.json
│   ├── index.ts
│   └── agents/
└── with-deps/            → 有 package.json 的子目录
    ├── package.json      → 读取 "pi.extensions" 字段
    ├── node_modules/
    └── index.ts
```

**加载器使用 `jiti` 实时编译 TypeScript**（不需要预编译），支持：
- 直接加载 `.ts` 文件
- 子目录检测 `index.ts` / `index.js`
- `package.json` 的 `pi.extensions` 字段声明
- 缓存：同一 CWD 下的扩展使用 `jiti` 模块缓存

### 5.3 ExtensionAPI — 扩展的编程接口

每个扩展导出一个 `(api: ExtensionAPI) => void` 工厂函数。`ExtensionAPI` 是扩展的全部能力入口：

```typescript
interface ExtensionAPI {
  // ===== 事件订阅（22+ 事件类型） =====
  on("project_trust", handler)
  on("resources_discover", handler)     // 提供额外资源路径
  on("session_start", handler)          // 会话加载/创建
  on("session_shutdown", handler)       // 会话关闭
  on("session_before_compact", handler) // 压缩前（可取消/自定义）
  on("session_compact", handler)        // 压缩后
  on("session_before_tree", handler)    // 分支导航前
  on("session_tree", handler)           // 分支导航后
  on("session_info_changed", handler)   // 元信息变更
  on("session_before_switch", handler)  // 切换会话前
  on("session_before_fork", handler)    // 分叉前

  on("context", handler)                // 修改发送给 LLM 的消息
  on("before_provider_request", handler) // 修改 LLM 请求载荷
  on("before_provider_headers", handler) // 注入请求头
  on("after_provider_response", handler) // 响应后
  on("before_agent_start", handler)     // 用户输入后、Agent 启动前
  on("agent_start", handler)            // Agent 开始
  on("agent_end", handler)              // Agent 结束
  on("agent_settled", handler)          // Agent 完全稳定（含重试/压缩）
  on("turn_start", handler)             // 每轮开始
  on("turn_end", handler)               // 每轮结束
  on("message_start", handler)          // 消息开始
  on("message_update", handler)         // 消息流式更新
  on("message_end", handler)            // 消息结束（可改写消息）
  on("tool_execution_start", handler)   // 工具执行开始
  on("tool_execution_update", handler)  // 工具执行进度
  on("tool_execution_end", handler)     // 工具执行结束
  on("tool_call", handler)              // 工具调用前（可阻止）
  on("tool_result", handler)            // 工具结果后（可修改）
  on("user_bash", handler)              // 用户 ! 命令执行
  on("input", handler)                  // 用户输入处理（可转换/拦截）
  on("model_select", handler)           // 模型切换
  on("thinking_level_select", handler)  // 推理级别切换

  // ===== 工具注册 =====
  registerTool(tool: ToolDefinition)    // 注册 LLM 可调用的工具

  // ===== 命令/快捷键/CLI 标志 =====
  registerCommand(name, options)        // 注册 CLI 命令（如 /tools）
  registerShortcut(key, options)        // 注册键盘快捷键
  registerFlag(name, options)           // 注册 CLI 标志

  // ===== UI 渲染 =====
  registerMessageRenderer(type, fn)     // 自定义消息渲染
  registerEntryRenderer(type, fn)       // 自定义条目渲染

  // ===== 操作 =====
  sendMessage(message, options?)        // 发送自定义消息
  sendUserMessage(content, options?)    // 发送用户消息
  appendEntry(type, data?)             // 持久化自定义数据
  setSessionName(name)                  // 设置会话名
  setLabel(entryId, label)             // 标记/标签

  // ===== Provider =====
  registerProvider(name, config)        // 注册自定义 LLM 提供商
  unregisterProvider(name)             // 注销提供商

  // ===== 模型/工具 =====
  setModel(model)                       // 切换模型
  getThinkingLevel() / setThinkingLevel() // 推理级别
  getActiveTools() / setActiveTools()   // 启用/禁用工具
  getAllTools() / getCommands()         // 获取信息

  // ===== 执行 =====
  exec(command, args, options?)         // 执行 shell 命令
  events: EventBus                      // 扩展间通信总线
}
```

### 5.4 事件类型详解

**可修改上下文的事件**（返回结果会影响 Agent 行为）：

| 事件 | 返回类型 | 影响 |
|------|---------|------|
| `context` | `ContextEventResult` | 改写发送给 LLM 的消息列表 |
| `before_provider_request` | `unknown` | 替换 LLM 请求载荷 |
| `before_agent_start` | `BeforeAgentStartEventResult` | 注入自定义消息 / 替换 system prompt |
| `message_end` | `MessageEndEventResult` | 改写最终消息内容 |
| `tool_call` | `ToolCallEventResult` | 阻止工具调用（`block: true`） |
| `tool_result` | `ToolResultEventResult` | 修改工具执行结果 |
| `input` | `InputEventResult` | 转换/拦截用户输入 |
| `session_before_compact` | `SessionBeforeCompactResult` | 取消或自定义压缩 |
| `session_before_tree` | `SessionBeforeTreeResult` | 取消或自定义摘要 |
| `session_before_switch` | `SessionBeforeSwitchResult` | 取消会话切换 |
| `session_before_fork` | `SessionBeforeForkResult` | 取消分叉 |

**只读通知事件**（仅通知，不返回结果）：

| 事件 | 用途 |
|------|------|
| `session_start` | 会话加载后初始化状态 |
| `session_compact` | 压缩后更新 UI/状态 |
| `session_tree` | 分支导航后恢复状态 |
| `agent_start` / `agent_end` | 追踪 Agent 运行状态 |
| `turn_start` / `turn_end` | 追踪每轮对话 |
| `tool_execution_start/_update/_end` | 工具执行进度 |
| `model_select` / `thinking_level_select` | 追踪配置变更 |

### 5.5 ToolDefinition — 扩展工具接口

扩展注册的工具与内置工具平权：

```typescript
interface ToolDefinition<TParams, TDetails, TState> {
  name: string;
  label: string;
  description: string;
  promptSnippet?: string;           // 在 system prompt 中的简短描述
  promptGuidelines?: string[];      // 附加到 system prompt 的指南
  parameters: TSchema;              // 使用 TypeBox 定义参数 schema
  renderShell?: "default" | "self"; // 控制 UI 渲染

  prepareArguments?: (args) => Static<TParams>;  // 参数兼容层
  executionMode?: "sequential" | "parallel";

  execute(toolCallId, params, signal, onUpdate, ctx):
    Promise<AgentToolResult<TDetails>>;

  // 自定义 UI 渲染
  renderCall?(args, theme, context): Component;
  renderResult?(result, options, theme, context): Component;
}
```

### 5.6 ExtensionContext — 运行时上下文

事件处理器的 `ctx` 参数提供运行时能力：

```typescript
interface ExtensionContext {
  ui: ExtensionUIContext;        // UI 交互（select/confirm/input/notify/...）
  mode: ExtensionMode;           // "tui" | "rpc" | "json" | "print"
  hasUI: boolean;
  cwd: string;
  sessionManager: ReadonlySessionManager;  // 会话只读访问
  modelRegistry: ModelRegistry;
  model: Model<any> | undefined;
  signal: AbortSignal | undefined;        // 当前运行取消信号
  isIdle(): boolean;
  isProjectTrusted(): boolean;
  abort(): void;
  shutdown(): void;
  getContextUsage(): ContextUsage | undefined;
  compact(options?): void;
  getSystemPrompt(): string;
}
```

**ExtensionUIContext** 提供丰富的 UI 原语：

```typescript
interface ExtensionUIContext {
  select(title, options, opts?): Promise<string | undefined>;  // 选择器
  confirm(title, message, opts?): Promise<boolean>;            // 确认框
  input(title, placeholder?, opts?): Promise<string | undefined>; // 输入框
  notify(message, type?): void;                                // 通知
  onTerminalInput(handler): () => void;                        // 终端输入监听
  setStatus(key, text): void;                                  // 状态栏
  setWorkingMessage(message?): void;                           // 工作状态
  setWidget(key, content, options?): void;                     // 小部件
  setFooter(factory): void;                                    // 自定义页脚
  setHeader(factory): void;                                    // 自定义页眉
  setTitle(title): void;                                       // 窗口标题
  custom(factory, options?): Promise<T>;                       // 自定义组件
  editor(title, prefill?): Promise<string | undefined>;        // 编辑器
  pasteToEditor(text): void;                                   // 粘贴
  setEditorText(text): void;                                   // 设置编辑器文本
  addAutocompleteProvider(factory): void;                      // 自动补全
  setEditorComponent(factory): void;                           // 自定义编辑器
  setTheme(theme): void;                                       // 主题
  // ... 更多方法
}
```

### 5.7 扩展加载与运行时的生命周期

```
启动时:
  discoverAndLoadExtensions(paths)
    ├─ 发现扩展文件（本地/全局/配置）
    ├─ 去重（set）
    ├─ for each: jiti.import(path) → factory
    ├─      createExtensionAPI(ext, runtime) → api
    ├─      factory(api)  → 注册事件/工具/命令
    └─ 返回 { extensions, errors, runtime }

运行时绑定:
  runner.bindCore(actions, contextActions, providerActions)
    ├─ 复制动作实现到 runtime（sendMessage 等）
    ├─ 复制上下文回调到 runner（getModel, isIdle 等）
    └─ flush 等待中的 provider 注册

事件循环:
  AgentSession 在关键点调用 runner.emit*(event)
    ├─ emitToolCall(event)  → 每个扩展依次调用 handler
    ├─ emitContext(messages) → 每个扩展可改写消息列表
    ├─ emitToolResult(event) → 每个扩展可修改结果
    └─ emit(event)           → 通用事件分发

终止:
  各扩展 handler 通过 ctx 中的 assertActive() 检测过期
  会话切换/重载时：
    runner.invalidate("stale") → 所有 ctx 返回 this.staleMessage
```

### 5.8 扩展系统关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 加载方式 | jiti 实时编译 TS | 无需预编译，热加载 |
| 事件模型 | 同步链式调用（非 pub/sub 广播） | 每个 handler 可修改事件，链式传递 |
| 错误处理 | 捕获异常 → 记录到 ExtensionError | 单个扩展崩溃不阻塞其他扩展 |
| 扩展间通信 | EventBus（Node EventEmitter） | 保持简单，不依赖消息队列 |
| 状态持久化 | `appendEntry()` 写入会话树 | 天然支持分支回退 |
| 性能 | 扩展按路径加载，缓存 | 避免重复加载 |

### 5.9 示例扩展赏析

**hello.ts（最小工具）：**
```typescript
import { Type } from "@earendil-works/pi-ai";
import { defineTool } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.registerTool(defineTool({
    name: "hello",
    label: "Hello",
    description: "A simple greeting tool",
    parameters: Type.Object({
      name: Type.String({ description: "Name to greet" }),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
      return {
        content: [{ type: "text", text: `Hello, ${params.name}!` }],
        details: { greeted: params.name },
      };
    },
  }));
}
```

**tools.ts（工具管理命令）：**
```typescript
export default function (pi: ExtensionAPI) {
  let enabledTools = new Set<string>();

  pi.registerCommand("tools", {
    description: "Enable/disable tools",
    handler: async (_args, ctx) => {
      // 使用 ctx.ui.custom() 显示自定义 UI 组件
      // 使用 pi.setActiveTools() 启用/禁用工具
      // 使用 pi.appendEntry() 持久化状态到会话树
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    // 从会话树恢复状态
    restoreFromBranch(ctx);
  });
}
```

**subagent（多 Agent 编排）：**
```typescript
export default function (pi: ExtensionAPI) {
  pi.registerCommand("plan", {
    description: "Plan and implement with sub-agents",
    handler: async (args, ctx) => {
      // 使用 pi.exec() 启动子 Agent 进程
      // 子 Agent 在 RPC 模式下运行
      // 收集结果并合并
    },
  });
}
```

---

## 6. Pi 的设计亮点总结

### 6.1 消息类型扩展

通过 TypeScript 声明合并（declaration merging）实现可扩展的消息系统：

```typescript
declare module "@mariozechner/agent" {
  interface CustomAgentMessages {
    artifact: ArtifactMessage;       // 自定义 artifact 消息
    notification: NotificationMessage; // 自定义通知消息
  }
}
```

### 6.2 会话即树

每条消息、每次模型切换、每次压缩都作为树节点存储，天然支持：
- **分支：** 从任意历史节点创建新分支
- **回退：** 切换到任意历史节点
- **压缩非破坏：** 压缩生成摘要节点，原始数据在 JSONL 中保留

### 6.3 工具结果动态注入

工具执行结果可以动态添加工具名（`addedToolNames`），支持"工具发现工具"的模式。

### 6.4 认证解耦

认证系统独立于 Provider 实现，通过 `AuthStorage` 接口支持：
- 文件存储
- 内存存储
- OAuth 设备流

### 6.5 上下文感知的压缩

压缩时追踪文件操作（读/写/编辑），在压缩摘要中保留文件变更信息，避免压缩后丢失上下文。

### 6.6 事件驱动扩展

22+ 事件类型覆盖 Agent 全生命周期，每个事件处理器可修改对应数据。事件链式调用，单个扩展失败不阻塞其他扩展。

---

## 7. 对 NewsRadar Agent 子系统的借鉴意义

### 7.1 当前 NewsRadar 的扩展能力

NewsRadar 的 Agent 子系统当前的基础架构：

| 能力 | 实现 |
|------|------|
| LLM 多厂商 | ModelHub 抽象（OpenAI/Anthropic） |
| 工具系统 | Registry + @tool 装饰器，支持 MCP 工具 |
| 记忆系统 | ShortTermMemory / LongTermMemory + PG |
| 知识库 | pgvector 语义检索 |
| 角色系统 | PersonaAgent + 10 个角色 + Orchestrator |
| 审批通道 | WebSocket tool_approval_request |
| 执行器 | DirectExecutor / ReActExecutor |

### 7.2 可直接借鉴的设计

#### 7.2.1 事件驱动的扩展架构

**Pi 的做法：** 22+ 事件类型覆盖 Agent 全生命周期，扩展通过 `on(event, handler)` 订阅。

**NewsRadar 可以借鉴：**
- 在 Executor 循环中增加 `before_llm_call`、`after_llm_call`、`before_tool_exec`、`after_tool_exec` 等事件钩子
- 所有钩子通过统一的 `EventBus` 分发
- 插件可以订阅事件并修改行为，而不需要修改核心代码

#### 7.2.2 会话即树（Session-as-Tree）

**Pi 的做法：** 每条消息、每次模型切换、每次压缩都是树节点，天然支持分支回退。

**NewsRadar 可以借鉴：**
- 当前 `agent_messages` 表是线性结构，可以改为树形结构（增加 `parent_id` 列）
- 支持用户从历史消息分叉新对话
- 上下文压缩时生成摘要节点，原始数据保留

#### 7.2.3 技能系统（Skills）

**Pi 的做法：** `SKILL.md` 文件 + YAML frontmatter → 自动注入 system prompt。

**NewsRadar 可以借鉴：**
- 当前 NewsRadar 的 PersonaAgent 使用硬编码系统提示词
- 可以改为从 `.md` 文件加载，支持热更新
- 技能可以动态启用/禁用

#### 7.2.4 灵活的工具定义

**Pi 的做法：** `ToolDefinition` 接口 + `defineTool()` 辅助函数 + TypeBox schema。

**NewsRadar 可以借鉴：**
- 当前 `@tool` 装饰器已经很好用，可以增加 `renderCall`/`renderResult` 等 UI 渲染钩子
- 增加 `executionMode` 支持（sequential/parallel 混排）
- 增加 `prepareArguments` 参数兼容层

#### 7.2.5 上下文压缩

**Pi 的做法：** 用 LLM 生成摘要，替换旧消息，保留文件操作状态。

**NewsRadar 可以借鉴：**
- 当前 LongTermMemory 已经支持语义检索，但缺少上下文窗口管理
- 可以增加自动压缩机制：当 token 估计超过阈值时，用 LLM 压缩历史
- 压缩时保留关键信息（工具调用结果、关键决策）

#### 7.2.6 热加载扩展

**Pi 的做法：** jiti 实时编译 TS，无需重启即可加载扩展。

**NewsRadar 可以借鉴：**
- 当前所有工具和角色在启动时注册
- 可以增加动态注册机制，运行时加载新工具/角色
- 使用 Python 的 `importlib` 实现动态加载

### 7.3 不必借鉴的部分

| Pi 的设计 | 不借鉴的理由 |
|-----------|-------------|
| JSONL 文件存储 | NewsRadar 已有 PostgreSQL，更成熟 |
| TypeScript 泛型 + TypeBox | Python 无需 TypeScript 类型系统 |
| TUI 差分渲染 | NewsRadar 是 Web 前端，不涉及终端 UI |
| Orchestrator 多进程 | NewsRadar 当前单进程够用 |
| OAuth 设备流 | 当前用 API Key 即可 |

---

## 8. 安全模型

Pi 没有内置权限系统，进程以启动用户权限运行。隔离方案：

| 方案 | 说明 |
|------|------|
| Gondolin 扩展 | 沙箱隔离 |
| Plain Docker | Docker 容器隔离 |
| OpenShell | 受限 Shell 环境 |

---

## 9. 构建与开发

```bash
npm install --ignore-scripts   # 安装依赖
npm run build                   # 构建所有包
npm run check                   # 全面检查（lint + 类型 + 依赖）
./test.sh                       # 运行测试
```

供应链安全：
- 直接依赖锁定到精确版本
- `.npmrc` 设置 `save-exact=true` 和 `min-release-age=2`
- `package-lock.json` 是依赖权威来源
- pre-commit 阻止 lockfile 变动（除非 `PI_ALLOW_LOCKFILE_CHANGE=1`）