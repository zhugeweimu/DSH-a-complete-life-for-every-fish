# 快速开始（Quickstart）

## 1. 安装

```bash
cd memory-core
python -m pip install -r requirements.txt
# 可选：ONNX 重排
python -m pip install onnxruntime
```

## 2. 建库

```bash
python run.py --init-db
# 或：sqlite3 data/metadata.db < sql/schema.sql
```

## 3. 塞入演示数据并验证检索（不依赖外部服务）

```bash
python examples/seed_demo.py
# 预期输出：
#   记忆条数: 3
#   查询 药   → [('带药', ...)]
#   查询 烟花 → [('烟花', ...)]
```

## 4. 起服务

```bash
# 全部（检索 8096 / 图谱 8094 / 内核 8095）
python run.py --all

# 或只起内核
python run.py --core
```

## 5. 调用

```bash
curl http://127.0.0.1:8095/health

curl -X POST http://127.0.0.1:8095/recall \
  -H 'Content-Type: application/json' \
  -d '{"q":"带药","top":5,"rerank":"off"}'
```

## 6. 接入自己的记忆

写入走 `MEM_INBOX`（默认 `http://127.0.0.1:8001/booku-memory/api/memories`），
或直接按 `sql/schema.sql` 写 `booku_memory_records`。

**注意**：内核只读库；写入端负责去重、清洗、时间戳。
