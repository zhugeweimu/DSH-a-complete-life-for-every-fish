# 可调参数（个性化，按需要改）

这些值都是**经验值/口味值**，不是硬约束。改之前先看「影响」，改之后跑 `tests/` 确认没坏。

## 写入与去重（`src/store.py`）

| 参数 | 默认 | 位置 | 影响 |
|---|---|---|---|
| 精确判重 | sha1(title\|content\|person) 前 16 位 | `fingerprint()` | 改算法会让老 id 失效 |
| 近重阈值 | **0.8**（Jaccard） | `find_duplicate()` 的 `> 0.8` | 调低 → 合并更狠；调高 → 重复更多 |
| 近重扫描范围 | 同人最近 **300** 条 | `find_duplicate()` 的 `LIMIT 300` | 调大更准、更慢 |
| 重要度启发式 | 日记/自述 0.9，群聊 0.15，其余 0.5 | `_importance()` | 影响衰减速度 |
| 标题截断 | 80 字 | `normalize_title()` | 只影响显示 |

## 衰减（`src/memory_core.py`）

| 参数 | 默认 | 位置 | 影响 |
|---|---|---|---|
| 半衰期 | `H = 7 + 365 * imp²` 天 | `_decay()` | 调大 → 忘得慢 |
| 下限 | `floor = 0.3 * imp` | `_decay()` | 调大 → 老记忆更不容易沉底 |
| 流水清理 | 群聊且 7 天未激活 → 清理 | `_cleanup_flow()` | 调大 → 清得更狠 |
| 召回过滤 | `MEM_MIN_SCORE` 默认 0.55 | 环境变量 | 调低 → 召回更多、更杂 |

## 召回排序（`src/memory_core.py` / `hybrid.py`）

| 参数 | 默认 | 位置 | 影响 |
|---|---|---|---|
| 衰减权重 | `0.4 + 0.6 * decay` | 召回打分处 | 调高 → 更偏向“新/重要” |
| RRF k | 60 | `hybrid.rrf` | 调大 → 融合更平滑 |
| 重排权重 | 0.2 | `hybrid.search` | 调高 → 更信交叉编码器 |
| 向量服务 | `http://127.0.0.1:8081/v1/embeddings` | `EMBED_URL` | 换模型要重测阈值 |
| 文本截断 | 500 字 | `hybrid.embed` / `memory_core` | 超长会被截 |

## 服务与端口（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEM_DB` | `./data/metadata.db` | 记忆库路径 |
| `MEM_INBOX` | `http://127.0.0.1:8001/booku-memory/api/memories` | 写入转发端点 |
| `MEM_WRITE_MODE` | `auto` | `local` = 直接写本地；`auto` 有 inbox 就转发 |
| `GRAPH_DIR` | `./data/graph` | 图谱数据目录 |
| `CORE_PORT` / 端口参数 | 8095 | 内核端口 |
| `PORT_GRAPH` / `PORT_RETRIEVAL` | 8094 / 8096 | 独立服务端口 |
| `MEM_MIN_SCORE` | 0.55 | 召回最低分 |

## 图谱（`src/graph_server.py`）

| 参数 | 默认 | 影响 |
|---|---|---|
| 事件时间窗扩展 | ±12 小时 | 改 `_expand` 的 `pad_hours` |
| why 返回条数 | 5 | 改 `/why` 的 k |

## 原则

```
① 个人化参数集中在这里，代码里不散落魔法数
② 改阈值不改结构；改结构先加测试
③ 换嵌入模型必须重测 近重阈值 与 MEM_MIN_SCORE
```
