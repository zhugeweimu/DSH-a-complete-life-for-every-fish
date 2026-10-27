# 架构（ARCHITECTURE）

## 数据流

```
写入方（聊天插件 / 脚本 / 导入器）
        │  POST /remember（或直接写库）
        ▼
   MEM_INBOX（写入口，避免双写）
        │
        ▼
   SQLite（booku_memory_records / tags / temporary_memos）
        │
        ├── BM25（hybrid.tokens / BM25）
        ├── 向量（可选 EMBED_URL → /v1/embeddings）
        └── 重排（可选 ONNX reranker）
        │
        ▼
   /recall（8095）
        ├── 图谱扩展（entities / relations / events，±时间窗）
        └── 人物档案（profile：印象 / 特征）
```

## 设计约束

1. **只读库**：内核不写记忆库；写入统一走 `MEM_INBOX`。
2. **可降级**：没有向量服务 → 只走 BM25；没有重排模型 → 跳过重排。
3. **独立进程**：检索、图谱、内核可分开部署，互不阻塞。
4. **无框架**：只用标准库 `http.server` + SQLite，依赖面尽量小。
5. **纯记忆**：不包含情绪、人格、平台适配。

## 数据模型

| 表 | 关键字段 | 说明 |
|---|---|---|
| `booku_memory_records` | memory_id, title, content, person_id, folder_id, memory_type, importance, created_at, last_activated_at, is_deleted | 主表 |
| `booku_memory_tags` | memory_id, tag_type, tag_value | 标签 |
| `booku_temporary_memos` | memo_id, stream_id, content, expires_at | 临时备忘 |
| `entities` | id, name, aliases, features | 图谱实体（graph 自建） |
| `relations` | id, subject, predicate, object, note, evidence, ts | 关系边 |
| `events` | id, subject, kind, text, ts, vec, ref | 事件（带向量） |

## 检索公式

```
BM25 分数：标准 k1=1.2, b=0.75，中文按二元切分
向量分数：余弦相似度（512 维 bge）
融合：RRF( k=60 ) 对两路排名求和
重排：交叉编码器输出 [0,1] 分数，与原分加权
最终：score = 0.5*rrf_norm + 0.3*vec_norm + 0.2*rerank（可调）
```

## 扩展点

| 想改什么 | 改哪里 |
|---|---|
| 换分词 | `hybrid.tokens` |
| 换向量模型 | `hybrid.embed`（EMBED_URL） |
| 换重排 | `reranker_onnx.py`（RERANK_MODEL） |
| 加检索字段 | `hybrid.load_corpus` + `BM25` |
| 加图谱类型 | `graph_server.add_relation` 的 predicate |
| 换存储 | `hybrid.load_corpus` 与 `memory_core` 的 SQL |
