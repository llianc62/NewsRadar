# Daemon — 后台调度

`main.py` 是本地常驻进程，使用 **asyncio.Queue Channel 模式**（Go-style signal + data carrier）替代轮询。

## 架构模式

```
Timer ──put(None)──▶ asyncio.Queue ◀──get── Worker ──job──▶
Manual trigger ──put(callback)──▶  (callback 用于 SSE 通知)
```

- **Timer** 每 N 分钟向 queue 放入 `None`（定时触发，跳过通知）
- **Manual trigger**（Web API）向 queue 放入 callback closure（触发时通过 SSE 推送完成通知）
- **Worker** 从 queue 取任务执行，`asyncio.Lock` 防止重复触发
- Blocking I/O 在专用 `ThreadPoolExecutor(max_workers=4)` 中执行

每个任务类型（crawl、sync）拥有独立的 `asyncio.Queue` 和 `asyncio.Lock`。

## 启动序列

```
1. 加载 config.yaml + 环境变量
2. 连接 PostgreSQL + init_schema（含幂等迁移）
3. 注册信号处理器（SIGINT/SIGTERM → set shutdown event）
4. 启动 FastAPI web server（uvicorn，非阻塞）
5. 构建 Agent（agent.models 必填，缺失时 load_config 报错）
6. 启动后台 Workers + Timers
7. await shutdown（等待 web 故障或关闭信号）
```

## 工作线程

所有阻塞 I/O（DB 操作、HTTP 请求）运行在专用 `ThreadPoolExecutor`（max 4 workers），通过 `loop.run_in_executor()` 桥接到 async，避免阻塞 event loop。

## Agent 集成

Daemon 启动时启动 MCP Server（SSE 模式，供聊天室 agent 连接）并构建 `AgentFactory` + `tool_registry` + `base_prompt`，通过 `create_app()` 注入 Web 应用：

```python
await self._start_mcp_server(mcp_cfg)          # 聊天室 agent 依赖 MCP
tool_registry = setup_builtin_tools()
agent_factory = AgentFactory(self.config["agent"]["models"], self.db,
                             tool_registry, base_prompt=base_prompt)
app = create_app(self.db, s3_config, queues=queues, crawler=crawler,
                 agent_config=self.config, tool_registry=tool_registry,
                 agent_factory=agent_factory, base_prompt=base_prompt)
```

聊天室 agent 由 `web/agent.py:_build_chat_agent` 在首个请求时 per-session 惰性构建（`create_agent(register_mcp=True)`），不持有全局单例。Agent 路由始终注册。

## 可配参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `crawler.crawl_circle` | 60 | 抓取间隔（分钟） |
| `crawler.sync_circle` | 60 | 云端同步间隔（分钟） |
| `agent.models` | — | Agent 模型配置（可选，存在时启用 Agent） |

## 关闭

信号驱动（SIGINT/SIGTERM）：
1. 设置 `asyncio.Event` 通知所有 worker 停止
2. 取消 pending 的 asyncio Task（workers + timers + web）
3. 关闭 ThreadPoolExecutor（10s 超时，cancel_futures=True）
4. 关闭 PG 连接池

## 关键文件

| 文件 | 用途 |
|------|------|
| `main.py` | Daemon 入口 — Queue Channel 事件循环 + Agent 构建 |
| `agent/factory.py` | Agent 工厂函数 |
| `config.py` | 配置加载 — YAML + 环境变量合并 |
| `config.yaml` | 默认配置文件 |
