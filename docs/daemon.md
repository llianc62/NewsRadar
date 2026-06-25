# Daemon — 后台调度

`main.py` 是本地常驻进程，使用信号驱动的异步事件模型替代轮询。

## 架构模式

```
Timer ──set()──► asyncio.Event ◄──await── Worker ──exec──► Job
```

每个任务类型（crawl、sync）有独立的 `asyncio.Event`。Timer 按可配间隔 set 事件；Worker await 事件 → 执行 → 清除 → 等待下一轮。

## 启动序列

```
1. 加载 config.yaml + 环境变量
2. 连接 PostgreSQL + init_schema（含幂等迁移）
3. 启动 FastAPI web server（uvicorn）
4. 启动后台 workers + timers
5. 手动触发首次 sync 信号
```

## 工作线程

所有阻塞 I/O（DB 操作、HTTP 请求）运行在专用 `ThreadPoolExecutor`（max 4 workers），避免阻塞 event loop。

## 可配参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `crawler.daemon_interval_minutes` | 60 | 爬取间隔 |
| `crawler.sync_interval_minutes` | 60 | 云端同步间隔 |

## 关闭

信号驱动（SIGINT/SIGTERM）：
1. 设置 `asyncio.Event` 通知所有 worker 停止
2. 取消 pending 的 asyncio Task
3. 关闭 ThreadPoolExecutor（10s 超时）
4. 关闭 PG 连接池

## 关键文件

| 文件 | 用途 |
|------|------|
| `main.py` | Daemon 入口 — 信号驱动事件循环 |
| `config/loader.py` | 配置加载 — YAML + 环境变量合并 |
| `config.yaml` | 默认配置文件 |
