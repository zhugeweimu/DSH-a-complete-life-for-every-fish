# 记忆本体（memory-core）

> 只包含**记忆本身**：存储、索引、检索、重排、衰减、图谱、档案。
> 不包含情绪模型、语音、角色扮演、聊天平台插件、关系矩阵等任何联动挂钩。

## 它是什么

一套可独立运行的中文长期记忆内核：

```
存储   SQLite（记录 / 标签 / 临时备忘 / 图谱三表）
索引   BM25（中文二元切分）
语义   bge 向量（可选，走外部 /v1/embeddings）
融合   RRF（字面 + 语义）
重排   BGE reranker（可选，ONNX）
衰减   时间衰减 × 重要性 × 最近激活
图谱   实体 / 关系边 / 事件
服务   HTTP（单进程，无框架）
```

## 目录

```
memory-core/
├─ README.md              本文件
├─ MODULES.md             每个模块：职责 / 输入输出 / 依赖 / 不负责什么
├─ requirements.txt
├─ LICENSE
├─ config.example.json    配置样例（环境变量同名）
├─ sql/
│  └─ schema.sql          记忆库建表语句（从线上库导出）
├─ src/
│  ├─ memory_core.py      统一服务：/health /recall /remember /ingest /relation /why /profile
│  ├─ store.py            独立写入：库调用/去重/合并
│  ├─ turnlog.py          日志层：每回合追加 + 30 分钟会话分段 + 会话事件
│  ├─ autocapture.py      自动抽取：普通对话也能落事件（fact/preference/plan/promise/relationship/event/note）
│  ├─ store_cli.py        写入命令行：add/batch/ingest/turns/list/show
│  ├─ hybrid.py           BM25 + 向量 + RRF + 语料加载
│  ├─ reranker_onnx.py    ONNX 交叉编码器重排（可选）
│  ├─ retrieval_server.py 只管检索的轻量服务
│  └─ graph_server.py     实体 / 关系 / 事件图谱
└─ tests/
   ├─ test_schema.py
   ├─ test_bm25.py
   ├─ test_graph.py
   └─ test_imports.py
```

## 快速开始

```bash
pip install -r requirements.txt

# 1) 建库
sqlite3 data/metadata.db < sql/schema.sql

# 2) 起图谱（可选）
GRAPH_DIR=./data/graph python src/graph_server.py 8094

# 3) 起记忆内核
MEM_DB=./data/metadata.db python src/memory_core.py 8095
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEM_DB` | `./data/metadata.db` | 记忆库路径 |
| `MEM_INBOX` | `http://127.0.0.1:8001/booku-memory/api/memories` | 写入端点（内核只读库，写入转发） |
| `GRAPH_DIR` | `./data/graph` | 图谱数据目录 |
| `EMBED_URL` | `http://127.0.0.1:8081/v1/embeddings` | 向量服务（可选） |
| `RERANK_MODEL` | 空 | ONNX 重排模型路径（可选） |

## API（8095）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/recall` | 召回：`{q, top, person, scope, rerank}` |
| POST | `/remember` | 写入（转发到 `MEM_INBOX`） |
| POST | `/relation` | 增加关系边 |
| POST | `/why` | 解释某条关系/事件的证据 |
| GET | `/relations?entity=` | 查关系 |
| GET | `/profile?person_id=` | 人物档案（印象/特征） |

## 自述与日记（最初版）

`
日记：<库目录>/diary/YYYY-MM-DD.md，按行追加；同时写库（type=diary）
自述：<库目录>/diary/self.md，按日期追加；同时写库（type=self）
导出：<库目录>/vault/ —— index.md + memories/日期/ + 日记/ + 自述.md
`

不做 LLM 总结、不做人格成长，只保证“过去留得住、翻得到”。

## 设计原则

1. **只读库**：内核不写记忆库，写入统一走 inbox，避免双写。
2. **可降级**：没有向量服务时退化为 BM25；没有重排模型时跳过重排。
3. **可替换**：检索、重排、图谱各自独立进程，可单独跑。
4. **纯记忆**：情绪、语气、人格、平台适配都不在本包内。

## 注入边界（重要）

**本系统不自动注入上下文。** 原因：不同宿主方式不同。

`
会话框自带注入     → 可以完全不用本接口
桥/自建平台        → 显式调 POST /context 或 context.build()
`

接口只负责“按需生成一段可注入文本”，塞不塞、塞多少由宿主决定：

`ash
curl -X POST http://127.0.0.1:8095/context \
  -H 'Content-Type: application/json' \
  -d '{"person":"user:A","q":"带药","top":5,"max_chars":1200}'
# → {"text":"[10-09 16:53] 带药\n…","items":[...]}
`

## 可调参数

去重阈值、衰减半衰期、召回权重、端口等个人化参数集中列在 [docs/PARAMETERS.md](docs/PARAMETERS.md)，改前看影响，改后跑测试。

## 一封信

根目录的 [letter.md](letter.md) 是给主人的信，随包下载，随时可看。

## 许可

Apache-2.0（仅本包代码与 schema；不含任何角色设定、对话数据或平台插件）。
