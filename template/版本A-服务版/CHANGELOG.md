# 变更记录

## 2026-11-02 · 移除 8001/Neo-MoFox 运行时依赖

### 修复

- `MEM_WRITE_MODE` 默认改为 `local`：`/remember`、`/experience` 直接写 SQLite，不再 POST 到 8001。
- `store.add()` 显式使用 `MEM_DB`，并补齐 `bucket`、`source`、`status` 等 NOT NULL 字段，避免本地写入失败。
- `/list`、`/profile` 改为本地 SQLite 查询，不再访问 `MEM_INBOX`。
- `MEM_INBOX` 默认置空，仅 `MEM_WRITE_MODE=auto` 旧版兼容时使用。
- `config.example.json` 增加 `MEM_WRITE_MODE=local`，移除默认 8001 依赖。
- 文档同步：README / API / ARCHITECTURE / PARAMETERS / quickstart 不再要求 Neo-MoFox。

### 验收

- 不启动 8001，只启动 memory-core 8096，写入成功。
- 重启后 `/recall` 能读回同一条记忆。
- `/list` 返回本地 SQLite 记录。

## 2026-10-30 · 日志 / 事件分层

### 修复

- 停止 `memory_neo` 按单轮写 `event`：普通对话只写 `turn_logs`，事件在会话关闭时统一构建。
- 事件不再写入 `booku_memory_records`：新增独立 `event_records` 表，事件和语义记忆分开。
