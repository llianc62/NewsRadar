# Notifier 增量通知设计

## 问题

`notifier` 每次运行都发送当天全部新闻，每天 4 次通知中有大量重复内容。需要改为增量：每次只发送上次运行之后新抓取的新闻。

## 方案概述

不引入额外状态存储。CI 环境通过 GitHub API 获取上次 notifier 成功运行时间；本地环境通过 `--start-time` / `--end-time` 参数手动传入。SQLite 查询层扩展 `get_all()` 以支持 `created_at` 时间范围过滤。

```
CI: gh run list → --start-time <上次运行时间>
本地: --start-time / --end-time 手动传入（可选）
         │
         ▼
run_notifier(start_time=..., end_time=...)
         │
         ▼ ISO 8601 → YYYY-MM-DD HH:MM:SS
         │
         ▼
db.get_all(date, start_time=..., end_time=...)
         │
         ▼ SELECT * FROM news_items WHERE created_at > ? AND created_at < ?
         │
         ▼ 生成报告 → 发送邮件
```

## 改动文件

| 文件 | 改动 |
|------|------|
| `storage/sqlite.py` | `get_all()` 扩展 `start_time`/`end_time` 参数 |
| `news/notifier.py` | `run_notifier()` 新增 `start_time`/`end_time` 参数，做时间格式转换 |
| `cli/notify.py` | 新增 `--start-time` / `--end-time` CLI 参数，透传给 `run_notifier` |
| `.github/workflows/notifier.yml` | 新增 `gh run list` 步骤，获取上次成功运行时间并传入 `--start-time` |

## 详细设计

### 1. `storage/sqlite.py` — `get_all()` 扩展

```python
def get_all(
    self,
    date: str,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
) -> List[sqlite3.Row]:
    """Return rows for *date*, optionally filtered by created_at range.

    Args:
        date: Date string (YYYY-MM-DD), used to locate the DB file.
        start_time: If set, only rows with created_at > this
            (format: YYYY-MM-DD HH:MM:SS).
        end_time: If set, only rows with created_at < this
            (format: YYYY-MM-DD HH:MM:SS).

    Returns:
        Matching rows ordered by tier ASC, priority DESC.
    """
```

- `date` 只定位 `{date}.db` 文件，语义不变
- `start_time`/`end_time` 过滤 `created_at`，精度到秒，均为 keyword-only 参数
- 不传过滤参数 = 全量，完全向后兼容
- WHERE 子句动态拼接，SQL 注入防护通过参数化查询

### 2. `news/notifier.py` — `run_notifier()` 扩展

```python
def run_notifier(
    config: dict,
    dry_run: bool = False,
    start_time: str | None = None,
    end_time: str | None = None,
) -> None:
```

- `start_time` / `end_time` 为 ISO 8601 格式字符串（如 `2026-06-27T08:00:00Z`）
- 内部转换为 `YYYY-MM-DD HH:MM:SS` 后传给 `db.get_all(date, start_time=..., end_time=...)`
- 任一时间转换失败 → 日志警告，对应参数置为 `None`，不丢新闻
- 均不传时走 `get_all(date)` 全量，向后兼容

### 3. `cli/notify.py` — `--start-time` / `--end-time` 参数

```python
@app.command()
def notify(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Render and save the HTML report but do not send email",
    ),
    start_time: str | None = typer.Option(
        None, "--start-time",
        help="仅通知此时间（ISO 8601）之后创建的新闻",
    ),
    end_time: str | None = typer.Option(
        None, "--end-time",
        help="仅通知此时间（ISO 8601）之前创建的新闻",
    ),
):
    config = load_config("config.yaml")
    run_notifier(config, dry_run=dry_run, start_time=start_time, end_time=end_time)
```

### 4. `.github/workflows/notifier.yml` — 获取上次执行时间

在 "Run notifier" 步骤之前插入：

```yaml
- name: Get last notify time
  id: last_run
  env:
    GH_TOKEN: ${{ github.token }}
  run: |
    SINCE=$(gh run list \
      --workflow notifier.yml \
      --branch master \
      --status success \
      --limit 1 \
      --json createdAt \
      --jq '.[0].createdAt // ""')
    echo "since=$SINCE" >> $GITHUB_OUTPUT

- name: Run notifier
  env:
    ...
  run: python -m cli notify --start-time "${{ steps.last_run.outputs.since }}"
```

- `gh run list --status success --limit 1` 查询当前 workflow 上一次成功运行
- 首次运行（无历史）时 `since` 输出为空字符串，notifier 走全量
- `GITHUB_TOKEN` 自带读取 workflow run 权限，无需额外配置
- CI 场景只传 `--start-time`，不传 `--end-time`

## 边界情况

| 场景 | 行为 |
|------|------|
| 首次运行 / 无历史记录 | `--start-time` 为空 → 全量发送 |
| `workflow_dispatch` 手动触发 | 有历史则增量，无历史则全量 |
| ISO 8601 时间格式转换失败 | 日志警告 + 对应参数置 None，不丢新闻 |
| crawl 和 notify 并发运行 | notifier 下载的 DB 是上一轮 crawl 上传的稳定快照 |
| 本地运行不传过滤参数 | 全量发送，保持现有行为 |
| 同一 URL 多次抓取 | SQLite `(source_id, url)` 唯一索引保证同一天内不重复 |

## 测试要点

- `get_all(date, start_time=...)` 过滤正确性
- `get_all(date, end_time=...)` 过滤正确性
- `get_all(date, start_time=..., end_time=...)` 双边界过滤正确性
- `get_all(date)` 不传参数向后兼容
- `run_notifier(start_time=ISO_8601)` 格式转换正确
- `run_notifier()` 不传时间参数全量行为
- `run_notifier(start_time="invalid")` 回退不崩溃
