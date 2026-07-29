# Bug: Agent 会话跨页面恢复时消息显示异常

> **状态:** 待修复(阻塞于 agent Context 持久化改造,见下文)
> **发现日期:** 2026-07-29
> **关联:** [docs/superpowers/plans/2026-07-28-agent-session-resume.md](../superpowers/plans/2026-07-28-agent-session-resume.md)(Task 1-8 已合并,此 bug 为该 plan 上线后测试发现)

## 概述

Agent 对话页"跳转离开再跳转回来"的跨页面恢复存在两个显示 bug:

1. **进行中任务重连,用户消息丢失**(只剩 AI 流式回复)
2. **已完成任务重连,assistant 回复重复显示两条**

## 症状

### Bug 1:进行中任务重连,用户消息丢失

**复现:**

1. 新会话发送一条消息
2. AI 仍在流式回复时,跳转到其他页面(如热点新闻页)
3. 跳转回 AI 对话页

**预期:** 用户消息 + AI 回复(续推)都显示
**实际:** 只看到 AI 流式续推回复,**用户发的那条消息没显示**

### Bug 2:已完成任务重连,回复重复

**复现:**

1. 新会话发送消息,等 AI 回答完成(全部显示)
2. 跳转到其他页面
3. 跳转回 AI 对话页

**预期:** 用户消息 + 一条 AI 回复
**实际:** **两条相同的 AI 回复**(一条来自 DB 历史,一条来自 ChatTask 缓存补发)

## 根因分析

核心:**重连时 `loadMessages`(DB 历史)与 `_forward`(ChatTask 内存缓存)职责重叠且不协调。**

### 关键事实:写库时机

[agent/memory.py:81-92](../../agent/memory.py#L81) 的 `ShortTermMemory.save` 在 executor `_finalize`(任务完成)阶段才**同时**写 user + assistant:

```python
async def save(self, ctx):
    ...
    await asyncio.to_thread(self._db.save_agent_message, sid, "user", ctx.user_input)
    await asyncio.to_thread(self._db.save_agent_message, sid, "assistant", ctx.final_output)
```

即:user 消息**不是收到就写**,而是等任务完成才落库。

### Bug 1 链条(进行中重连,user 丢失)

进行中任务重连时:

- DB:user/assistant 都没写(任务没完成,`_finalize` 没跑)
- `loadMessages`(前端从 DB 加载):加载不到当前轮 user 消息
- `_forward`(后端补发 ChatTask 缓存):只补发 AI 的 `resume`(`full_reply`),**不含 user 消息**
- 结果:user 消息丢失

### Bug 2 链条(已完成重连,重复)

已完成任务重连时:

- DB:user + assistant 都已写
- `loadMessages`:加载到完整回复(显示第 1 条)
- WS endpoint 连接恢复([web/agent.py:699](../../web/agent.py#L699))对**任何** `chat_task`(含 `done=True`)都触发 `_forward`
- `_forward` 的 `done` 分支([web/agent.py:195-201](../../web/agent.py#L195))又补发 `resume` + `done`(显示第 2 条)
- 结果:两条相同回复

### 前端顺序问题(加剧)

[agent_chat.html:466](../../web/templates/pages/agent_chat.html#L466) 的 `switchSession` 中 `loadMessages(sid)` 未 `await` 就 `reconnectWS()`,两者并行。WS 的 `_forward` 事件可能在 `loadMessages` 返回前先到,导致渲染顺序错乱。

## 涉及代码

| 位置 | 问题 |
|------|------|
| [agent/memory.py:81-92](../../agent/memory.py#L81) | `ShortTermMemory.save` 在 `_finalize` 才写 user+assistant(user 写库晚) |
| [web/agent.py:699](../../web/agent.py#L699) | WS endpoint 连接恢复未过滤 `done`,已完成任务也 `_forward` |
| [web/agent.py:186-212](../../web/agent.py#L186) | `_forward` 的 `done` 分支补发 `resume`+`done`,与 `loadMessages` 重复 |
| [web/agent.py:34-47](../../web/agent.py#L34) | `ChatTask` 无 `user_message` 字段,无法补发当前轮 user |
| [agent_chat.html:466](../../web/templates/pages/agent_chat.html#L466) | `switchSession` 的 `loadMessages` 未 `await` |
| [agent_chat.html:593](../../web/templates/pages/agent_chat.html#L593) | `init` 同样 `loadMessages` 未 `await` |

## 修复方案讨论历程

### 方案 A:Web 层即时写 user(已否决)

在 `_start_chat` 收到 chat 即 `db.save_agent_message(sid, "user", message)`,`memory.save` 只写 assistant。

**否决理由:** 绕过 memory 模块多处写库,与有序对话的统一写库原则冲突;`NullMemory`(不持久化)场景下仍写 user,语义不纯。

### 方案 B:executor `_prepare` 写 user(已否决)

改 `MemoryModule` 接口,`_prepare` 写 user,`_finalize` 只写 assistant。

**否决理由:** 用户决策"对话记录统一交给 memory 维护,避免多处写库 + 控写库频率"。不提前写 user。

### 方案 C(最终待实施):不改写库时机,ChatTask 内存缓存 user_message

保持 memory 在 `_finalize` 统一写。`user_message` 放 `ChatTask` 内存(不落库),活跃时 `_forward` 补发:

1. `ChatTask` 加 `user_message: str` 字段(内存)
2. `_start_chat` 记录 `ct.user_message = message`
3. WS endpoint **已完成(`done`)不 `_forward`**,只活跃(`not done`)补发
4. `_forward` 活跃分支:先发 `user_message` -> `resume` -> 续推 token -> `done`;`done` 分支不补发(走 DB)
5. memory 不改
6. 前端 `await loadMessages` 再连 WS + `handleMessage` 加 `user_message` 分支

**职责切分:进行中任务走 `_forward` 补发(含 user_message);已完成任务走 DB 历史,不 `_forward`。**

> 注:方案 C 是 agent Context 改造前的过渡方案。Context 改造后可能有更优解(见下)。

## 阻塞依赖:agent Context 持久化改造

讨论中发现一个更深的架构 gap,影响本 bug 的最优修复路径:

**当前 `DefaultAgent` 每次 `chat`/`chat_stream` 都 `_make_ctx` 新建临时 Context([agent/agent.py:119,135](../../agent/agent.py#L119)),run 完丢弃。agent 实例不持有会话上下文/对话记录。**

这与主流做法(会话内 agent 未销毁则一直持有 context,除非主动压缩)不符。导致:

- "从活跃 agent 读对话记录"不可行(agent 无缓存)——本 bug 修复只能退而用 `ChatTask` 内存缓存当前轮
- agent 无法在会话内维持完整上下文(每轮都从 DB `memory.load` 重建历史)

**待办:先讨论并实施 agent Context 持久化改造,再回来按方案 C(或改造后的更优方案)修复本 bug。**

## 临时缓解

无。bug 在跨页面恢复时必现,但正常单页对话不受影响。
