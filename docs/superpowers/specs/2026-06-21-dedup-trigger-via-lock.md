# 使用 asyncio.Lock 防止重复触发

## 问题

用户在前端多次点击"抓取"/"同步"按钮，每次点击都往 `asyncio.Queue`
塞一个 callback，Worker 会排队依次执行——短时间内跑多次相同 job。

```
点击1 → queue.put(cb1)
点击2 → queue.put(cb2)   ← 重复
点击3 → queue.put(cb3)   ← 重复
         ↓
Worker: get(cb1) → 执行 job → get(cb2) → 又执行 job → ...
```

Timer 也存在同样问题：job 执行中 queue 为空，Timer 又可以 put，造成积压。

## 方案

用 **`asyncio.Lock`** 作为运行互斥锁。Worker 执行期间持有锁，
锁住时 Timer 跳过、API trigger 返回 409。

### 核心原则

- 锁本身即是运行状态，不需要额外的 running 标记
- `lock.locked()` 天然可查询
- `async with lock` 保证异常退出也释放锁

## 改动清单

### 1. `main.py`

#### 1.1 `__init__` — 加两个 Lock

```python
self._crawl_lock = asyncio.Lock()
self._sync_lock = asyncio.Lock()
```

#### 1.2 `queues` dict — 把 lock 传给 web 层

```python
queues = {
    "crawl": self._crawl_queue,
    "sync": self._sync_queue,
    "crawl_lock": self._crawl_lock,   # web 层查询 locked() 状态
    "sync_lock": self._sync_lock,
}
```

asyncio.Lock 是引用类型，两边共享同一个对象。

#### 1.3 `_worker` — 签名加 lock 参数，执行 job 时持有锁

```python
async def _worker(self, name: str, queue: asyncio.Queue, job, lock: asyncio.Lock) -> None:
    ...
    callback = await self._wait_queue(queue)
    if callback is None and self._shutdown_event.is_set():
        break
    async with lock:
        await self._try_run_job(name, job, callback)
```

**关键**：`get()` 放在锁外面，`_try_run_job` 在锁里面。这样 get 取出 callback
后槽位释放，但锁还锁着——Timer 和 API 都进不来。

#### 1.4 `_timer` — 签名加 lock 参数，锁住时跳过

```python
async def _timer(self, queue: asyncio.Queue, interval_min: int,
                 name: str, lock: asyncio.Lock) -> None:
    ...
    if not lock.locked():
        await queue.put(None)
```

#### 1.5 创建 Worker/Timer 时传入 lock

```python
# Workers
self._worker("Crawl", self._crawl_queue, self._crawl_job, self._crawl_lock),
self._worker("Sync", self._sync_queue, self._sync_job, self._sync_lock),

# Timers
self._timer(self._crawl_queue, crawl_interval, "Crawl", self._crawl_lock),
self._timer(self._sync_queue, sync_interval, "Sync", self._sync_lock),
```

### 2. `web/app.py`

#### 2.1 trigger_crawl — 加锁检查

```python
@app.post("/api/trigger/crawl")
async def trigger_crawl():
    queue = app.state.queues.get("crawl")
    lock = app.state.queues.get("crawl_lock")
    if queue is None:
        return JSONResponse({"ok": False, "error": "not available"}, 404)
    if lock and lock.locked():
        return JSONResponse({"ok": False, "error": "已有抓取任务正在执行"}, 409)

    notif = _add_notification(0, "新闻抓取", "running", category="crawl")
    ...
```

#### 2.2 trigger_sync — 同理

```python
if lock and lock.locked():
    return JSONResponse({"ok": False, "error": "已有同步任务正在执行"}, 409)

notif = _add_notification(0, "云端同步", "running", category="sync")
```

#### 2.3 通知初始状态

从 `"pending"` 改为 `"running"`，因为此时 queue.put 会立即被 Worker
取出（Worker 在 wait），没有"排队中"的空窗期。

### 3. Queue maxsize

不改。`asyncio.Lock` 已经阻止了重复触发，Queue 保持无界也无害。
Timer 跳过 + API 409 保证同一时刻最多一个 callback 在 queue 里。

## 行为变化

| 场景 | 改前 | 改后 |
|------|------|------|
| 点1次"抓取" | queue 排队 → 执行 | **直接执行**（Worker 空闲） |
| 执行中点第2次 | queue 再排一个 → 执行完接着跑 | **409 "已有任务正在执行"** |
| Timer 触发时 job 在跑 | queue 积压 | **跳过，等下一轮** |
| 通知初始状态 | `"pending"` | `"running"` |

## 不改的部分

- `_wait_queue` — 不变
- `_try_run_job` — 不变
- Queue 类型 — 保持 `asyncio.Queue()`
- refetch/fetch_url 的 `_refetch_tasks` 去重 — 已有 status 检查，不冲突

## 风险评估

- **死锁**：不会。`asyncio.Lock` 在同一协程内不可重入，但 `_worker` 只 acquire 一次。
- **异常丢锁**：不会。`async with` 保证 finally 释放。
- **Timer 永久跳过**：不会。job 执行完（或异常退出）锁释放，下一轮 Timer 正常 put。
- **web 层读 lock 线程安全**：`asyncio.Lock.locked()` 不是线程安全的，
  但 trigger 端点在 asyncio 事件循环中运行，不会并发冲突。
