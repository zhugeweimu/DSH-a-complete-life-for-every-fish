# 变更记录

## 2026-10-30 · 日志 / 事件分层

### 修复

- 停止 `memory_neo` 按单轮写 `event`：普通对话只写 `turn_logs`，事件在会话关闭时统一构建。
- 事件不再写入 `booku_memory_records`：新增独立 `event_records` 表，事件和语义记忆分开。
- 修复 `event_id` 逗号串当事件主键：`turn_logs.event_id` 保留为日志字段，`event_records` 每条事件独立 id。
- 修复长会话内容截断：事件内容上限从 800 字提高到 2000 字。

### 优化

- `close_session` 按类型分桶，每种类型生成独立 `event_records`，并记录 `source_log_ids`、`start_ts/end_ts`。
- `hybrid.load_corpus` 同时读取 `event_records` 和语义记忆，事件级检索不再拼碎事件。
- 新增 `/decay`：`weight = importance × 0.5^(年龄/30天)`，`plain` 且 `weight < 0.08` 归档。
- 旧数据迁移：857 条旧 `event/session` 记录先归档到 `event_legacy`，再标记原记录 `is_deleted=1`；原始日志保留。

### 边界

- 仍然按 30 分钟空闲 / 2 小时上限切会话，不做语义合并。
- 跨会话同一话题后续只做关联提案，不合并时间线。
- `turn_logs` 不向量化，不能主动检索；需要检索时从 `event_records` 查。

## 测试

```text
python -m unittest discover -s tests -v
Ran 40 tests
OK
```
