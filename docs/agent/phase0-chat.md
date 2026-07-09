# Phase 0：LLM 接入 + 聊天界面

> **父文档**: [index.md](index.md)  
> **产出**: 聊天窗口 + 默认 agent  
> **可验证**: 浏览器 `/agent` 能对话

**说明**：本系统为 personal 分析系统，无用户概念。Session 由服务端自动管理（cookie 写入 session_id），用户无感知。

---

## 1. 类型定义

**文件**: `agent/types.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class LlmConfig:
    protocol: str           # "anthropic" | "openai"
    model: str
    api_key: str
    base_url: str = ""
    temperature: float = 0.7


@dataclass
class Turn:
    user: str
    assistant: str
```

---

## 2. LLM 工厂

**文件**: `agent/llm.py`

核心函数 `build_llm(cfg: LlmConfig)` 根据 `protocol` 字段加载对应 SDK：

```python
from langchain_core.language_models import BaseChatModel


def build_llm(cfg: LlmConfig) -> BaseChatModel:
    common = {
        "model": cfg.model,
        "api_key": cfg.api_key,
        "temperature": cfg.temperature,
    }
    if cfg.base_url:
        common["base_url"] = cfg.base_url

    if cfg.protocol == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(**common)
    elif cfg.protocol == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(**common)
    else:
        raise ValueError(f"Unsupported LLM protocol: {cfg.protocol!r}")
```

**设计决策**：
- SDK 懒导入（协议分支内 import），避免未使用的协议包成为硬依赖
- 未知 protocol 立即抛 ValueError，fail-fast
- 不引 `langchain` 主包，仅 `langchain-anthropic` / `langchain-openai` 两个薄包装

---

## 3. Agent 核心类（Phase 0 版）

**文件**: `agent/agent.py`

```python
from collections.abc import AsyncIterator


class Agent:
    """Phase 0：默认（无角色）agent，直调 LLM，支持流式。"""

    def __init__(self, llm_cfg: LlmConfig):
        self.llm = build_llm(llm_cfg)

    async def chat_stream(self, message: str) -> AsyncIterator[str]:
        """流式调 LLM，逐 token yield。"""
        # 使用 llm.stream() 而非 llm.invoke()
        async for chunk in self.llm.astream(message):
            content = chunk.content
            if content:
                yield content

    async def chat(self, message: str) -> str:
        """非流式版本，兼容简单场景。"""
        return await self.llm.ainvoke(message).content
```

---

## 4. 聊天界面

### 4.1 页面位置

现有侧边栏有 7 个导航项（首页 / 热点新闻 / 持仓分析 / 个股分析 / 行业报告 / 交易决策 / 系统配置），在其下方加分隔线和 AI 助手区域：

```html
<nav class="sidebar-nav">
  <!-- 现有 7 个导航项 -->
  <a href="/"           class="nav-item">首页</a>
  <a href="/hot-news"   class="nav-item">热点新闻</a>
  <a href="/positions"  class="nav-item">持仓分析</a>
  <a href="/stocks"     class="nav-item">个股分析</a>
  <a href="/reports"    class="nav-item">行业报告</a>
  <a href="/trading"    class="nav-item">交易决策</a>
  <a href="/settings"   class="nav-item">系统配置</a>
</nav>

<!-- 新增：分隔线 + AI 助手 -->
<div class="sidebar-divider"></div>

<div class="sidebar-section-label">AI 助手</div>
<div class="chat-sessions" id="chat-sessions">
  <button class="new-chat-btn" onclick="createNewChat()">+ 新建会话</button>
  <div class="session-list" id="session-list">
    <!-- JS 从 API /api/agent/sessions 获取渲染 -->
  </div>
</div>
```

### 4.2 聊天页面布局

```
┌──────────────────────────────────────────────────┐
│  ┌─────── 侧边栏 ───────┐ ┌─── 主区域 ───────────│
│  │  首页                 │ │                      │
│  │  热点新闻             │ │   消息列表            │
│  │  持仓分析             │ │   (滚动到底部)        │
│  │  个股分析             │ │                      │
│  │  行业报告             │ │                      │
│  │  交易决策             │ │                      │
│  │  系统配置             │ │                      │
│  ├───────────────────────┤ │                      │
│  │  AI 助手 (高亮)       │ │                      │
│  │  ───会话───           │ │                      │
│  │  + 新建会话           │ │   输入框 [发送]      │
│  │  昨天 15:30           │ │                      │
│  │  2026-07-05           │ │                      │
│  │  2026-07-04           │ │                      │
│  └───────────────────────┘ └──────────────────────│
└──────────────────────────────────────────────────┘
```

### 4.3 会话管理

- 每次聊天对应一个会话（session），由服务端自动管理
- 会话数据存服务端 PostgreSQL，**不存浏览器 localStorage**
- Session ID 通过 cookie 自动关联——首次访问聊天页时服务端创建 session，写入 cookie
- 重新打开页面时 cookie 带回，恢复历史对话
- 点击"新建会话"时后端创建新 session，更新 cookie
- 两张表：`agent_sessions`（会话元信息）+ `agent_messages`（消息内容）
- 会话标题默认取用户首条消息的前 30 字（不调 LLM 生成，简单可靠）

### 4.4 API 与 WebSocket 协议

**REST API（请求-响应）：**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/agent` | 聊天页面 |
| GET | `/api/agent/sessions` | 获取当前会话列表（分页，倒序） |
| POST | `/api/agent/sessions` | 新建会话 |
| DELETE | `/api/agent/sessions/{id}` | 删除会话及其消息 |
| GET | `/api/agent/sessions/{id}/messages` | 获取某会话的消息列表 |

**GET /api/agent/sessions 响应：**
```json
{
  "sessions": [
    {"id": 1, "title": "今天有什么新闻？", "message_count": 12, "created_at": "2026-07-07T10:30:00+08:00"},
    {"id": 2, "title": "巴菲特投资哲学",   "message_count": 5,  "created_at": "2026-07-06T15:20:00+08:00"}
  ]
}
```

**POST /api/agent/sessions 请求/响应：**
```json
// 请求：空 body，服务端自动创建
// 响应：
{"id": 3, "title": "新会话", "created_at": "2026-07-07T11:00:00+08:00"}
```

**WebSocket 端点：** `ws://<host>:<port>/api/ws`

统一 WebSocket 通道，承载所有实时通信。所有消息使用 JSON 帧，通过 `type` 字段区分消息类型。

**客户端 → 服务端消息：**
```json
{
  "type": "chat",
  "session_id": 1,
  "message": "今天有什么新闻？",
  "model": "deep"        // "deep" | "quick" — 可选，不传则沿用上次，首次默认 quick
}

{
  "type": "stop"          // 停止当前生成
}
```

**服务端 → 客户端消息：**
```json
{
  "type": "token",
  "content": "今天"
}

{
  "type": "done",
  "session_id": 1,
  "full_reply": "今天的新闻有..."
}

{
  "type": "error",
  "message": "模型调用失败"
}

{
  "type": "tool_call",          // Phase 4 起
  "name": "search_news",
  "args": {"query": "A股"}
}

{
  "type": "notification",       // 通知系统迁移至此
  "notification": {
    "id": 42,
    "category": "crawl",
    "title": "新闻抓取",
    "status": "completed",
    "summary": "抓取 15 条"
  }
}
```

**后端处理流程（Phase 0）：**
```
客户端连接 WS
  │
  ▼
接收 {"type": "chat", "session_id": 1, "message": "..."}
  → 根据 session_id 加载历史消息
  → Agent.chat_stream(message)
    → llm.astream(message) 逐 token yield
    → WS 逐帧推送 {"type": "token", "content": "..."}
  → 保存用户消息 + AI 回复到 agent_messages
  → WS 推送 {"type": "done", ...}
  │
  ▼
接收 {"type": "stop"}
  → 取消当前 Agent 生成任务
  → WS 推送 {"type": "done", "stopped": true}
```

### 4.5 模型切换

- 用户可在对话框输入区附近随时切换 `deep` / `quick` 模型
- 每次 `chat` 消息通过 `model` 字段指定本次使用的模型
- 不传则沿用上次选择，首次默认 `quick`
- 切换即时生效，不持久化到服务端

### 4.6 样式要点

- 聊天消息气泡：用户右对齐（蓝色/品牌色），AI 左对齐（灰色）
- Markdown 渲染（代码块、表格、列表）
- 流式打字效果
- 输入框支持 Enter 发送，Shift+Enter 换行
- 加载中显示"思考中..."指示器

---

## 实现检查清单

- [ ] `config.yaml` → `llm:` + `agent:` 段
- [ ] `config/loader.py` → `_load_llm_instance()` + `_load_agent_config()`
- [ ] `pyproject.toml` → 新增 `langchain-anthropic`, `langchain-openai`
- [ ] `storage/postgres.py` → `_init_agent_schema()` 会话+消息表
- [ ] `agent/__init__.py`
- [ ] `agent/types.py` → `LlmConfig`, `Turn` dataclass
- [ ] `agent/llm.py` → `build_llm()` 工厂
- [ ] `agent/agent.py` → `Agent.__init__()` + `chat_stream()`（用 `astream`）
- [ ] `web/app.py` → `register_agent_routes()` + WebSocket `/api/ws` 端点
- [ ] `web/app.py` → 通知系统从 SSE 迁移到 WS（`type: "notification"`）
- [ ] `web/templates/pages/agent_chat.html` → 聊天界面（WebSocket 客户端）
- [ ] `web/templates/components/sidebar.html` → 侧边栏 AI 助手区域
- [ ] `web/templates/base.html` → 移除 SSE 相关代码，改用统一 WS
- [ ] `main.py` → 注册 agent 路由（条件判断 `config.get("llm")`）
- [ ] 验证：浏览器 `/agent`，输入消息，看到逐 token 流式回复
- [ ] 验证：通知（抓取/同步任务）通过 WS 推送
- [ ] 验证：关闭页面重开，历史会话恢复