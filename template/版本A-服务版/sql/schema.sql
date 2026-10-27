-- 记忆本体 schema（最小可用集）
-- 只保留内核实际用到的字段；平台相关的扩展字段（地点/资产/流程等）已剔除。

CREATE TABLE IF NOT EXISTS booku_memory_records (
    memory_id          TEXT PRIMARY KEY,
    title              TEXT NOT NULL DEFAULT '',
    content            TEXT NOT NULL,
    person_id          TEXT NOT NULL DEFAULT '',
    folder_id          TEXT NOT NULL DEFAULT '',
    memory_type        TEXT NOT NULL DEFAULT 'event',
    importance         REAL NOT NULL DEFAULT 0.5,
    created_at         REAL NOT NULL DEFAULT 0,
    updated_at         REAL NOT NULL DEFAULT 0,
    last_activated_at  REAL NOT NULL DEFAULT 0,
    activation_count   INTEGER NOT NULL DEFAULT 0,
    is_deleted         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mem_person     ON booku_memory_records (person_id);
CREATE INDEX IF NOT EXISTS idx_mem_folder     ON booku_memory_records (folder_id);
CREATE INDEX IF NOT EXISTS idx_mem_type       ON booku_memory_records (memory_type);
CREATE INDEX IF NOT EXISTS idx_mem_created    ON booku_memory_records (created_at);
CREATE INDEX IF NOT EXISTS idx_mem_activated  ON booku_memory_records (last_activated_at);

CREATE TABLE IF NOT EXISTS booku_memory_tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id  TEXT NOT NULL,
    tag_type   TEXT NOT NULL DEFAULT 'core',
    tag_value  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tag_memory ON booku_memory_tags (memory_id);
CREATE INDEX IF NOT EXISTS idx_tag_value  ON booku_memory_tags (tag_value);

CREATE TABLE IF NOT EXISTS booku_temporary_memos (
    memo_id     TEXT PRIMARY KEY,
    stream_id   TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0,
    expires_at  REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memo_stream  ON booku_temporary_memos (stream_id);
CREATE INDEX IF NOT EXISTS idx_memo_expires ON booku_temporary_memos (expires_at);


-- 日志层：每回合原样追加（事件由会话关闭时生成）
CREATE TABLE IF NOT EXISTS turn_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person     TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    user_text  TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    closed     INTEGER NOT NULL DEFAULT 0,
    event_id   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_log_person_ts ON turn_logs (person, ts);
CREATE INDEX IF NOT EXISTS idx_log_session  ON turn_logs (session_id);
