# 增量 API 同步器

本项目只使用 Python 标准库实现，包含一个本地分页 REST 服务和一个将该服务增量同步到 SQLite 的命令行程序，不访问外部网络，也不依赖第三方包。

## 目录结构

- `mock_api/`：基于 `http.server` 的本地分页服务。
- `fixtures/`：规则驱动的数据和分页配置，可重新生成。
- `syncer/`：HTTP 客户端、游标分页、水位线、SQLite 存储、checkpoint 和 CLI。
- `tests/test_syncer.py` 与根目录 `test_syncer.py`：覆盖 9 条验收口径的 `unittest`。
- `out/`：demo 运行时生成的 SQLite 和 checkpoint 文件。

## 本地服务语义

服务从 `fixtures/dataset.json` 读取分页数据，并提供：

- `GET /items?cursor=<不透明游标>&limit=<页大小>&since=<ISO-8601 水位线>`
  - 返回 `{"items": [...], "next_cursor": "..."}`。
  - 第一页不传 `cursor`；后续页面逐页传入上一页返回的 `next_cursor`。
  - 最后一页的 `next_cursor` 为空字符串 `""`。
  - `since` 只保留 `updated_at` 严格大于该水位线的记录。
  - 服务端会折叠过滤后为空的页，并继续返回后续非空页。
- `GET /meta`
  - 返回服务端当前最大 `updated_at`。
  - 同步器在开始一轮新同步前用它判断是否存在变更；没有变更时不会请求 `/items`。
  - 统计摘要中的“请求数”只统计 `/items` 数据页请求，不统计 `/meta` 探测请求。

游标是 base64 编码的 JSON，对客户端不透明。客户端不能猜测游标内容，只能原样回传。

## 同步语义

### 游标分页

`syncer.pager.CursorPager` 从空游标开始请求第一页，然后不断回传 `next_cursor`，直到收到空字符串。每处理完一页，同步器立即：

1. 提交 SQLite 事务；
2. 把下一页游标和本轮起始水位线写入 JSON checkpoint；
3. 原子替换 checkpoint 文件并 fsync。

进程在任意两页之间被杀掉后，重启会读取 checkpoint 并从未完成的下一页继续，已完成页不会再次拉取。

### 水位线

checkpoint 中保存已完成轮次看到的最大 `updated_at`。下一轮同步把该值作为 `since` 发给服务端，因此只拉取严格更新的记录。

进行中的轮次会固定使用开始时的水位线作为 `resume_watermark`。只有完整轮次成功结束后才推进正式水位线，避免中途失败造成漏同步。

### 幂等写入与冲突

SQLite 表 `items.id` 是 `PRIMARY KEY`，数据库层保证同一 id 只有一行。写入使用 `INSERT ... ON CONFLICT(id) DO UPDATE`：

- 远端记录的 `updated_at` 大于本地值：远端覆盖本地，并计一次冲突。
- 两者 `updated_at` 相同：视为重复投递，不重复写入，也不计冲突。
- 更旧的远端版本不会覆盖较新的本地版本。

### 删除传播

服务端记录的 `deleted=true` 表示墓碑。同步器收到后执行物理删除：

```sql
DELETE FROM items WHERE id = ?
```

本地不会保留墓碑标记。删除成功时删除计数加一；如果本地本来没有该行，不重复计数。

## 重新生成服务端数据

默认数据集严格按以下规则生成：共 9 个记录槽、3 页、每页 3 条；一个 id 在相邻两页重复投递；第二页有一条墓碑；共 8 个不同 id。

```bash
python -m fixtures.builder
```

该命令会重建 `fixtures/dataset.json`。同步器不依赖固定 id 数量之外的硬编码结果，测试也通过 `fixtures.builder` 按规则生成数据。

## 运行测试

```bash
python -m unittest test_syncer -v
```

根目录的 `test_syncer.py` 是测试入口垫片，实际用例位于 `tests/test_syncer.py`。

## 运行 demo

先启动本地服务：

```bash
python -m mock_api --host 127.0.0.1 --port 8080
```

在另一个终端运行同步器：

```bash
python -m syncer.runner \
  --base http://127.0.0.1:8080 \
  --db out/sync.db \
  --state out/state.json
```

成功后 stdout 会打印一行摘要，格式如下：

```text
sync summary: requests=3 inserted=7 deleted=0 conflicts=0
```

再次运行且服务端没有变化时，不会拉取数据页：

```text
sync summary: requests=0 inserted=0 deleted=0 conflicts=0
```

## Checkpoint 格式

`out/state.json` 示例：

```json
{
  "active": false,
  "completed_rounds": 1,
  "next_cursor": "",
  "resume_watermark": null,
  "watermark": "2026-01-01T00:08:00Z"
}
```

`active=true` 表示存在一个需要恢复的未完成轮次，`next_cursor` 是下一页游标。
