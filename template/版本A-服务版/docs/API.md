# API 速查

默认端口：内核 8095、检索 8096、图谱 8094。

## 健康

```bash
curl http://127.0.0.1:8095/health
# {"ok": true, "docs": 379, "entities": 6, "relations": 2, "events": 4, ...}
```

## 召回

```bash
curl -X POST http://127.0.0.1:8095/recall \
  -H 'Content-Type: application/json' \
  -d '{"q":"带药","top":5,"rerank":"off"}'
```

返回：按分数排序的记忆（id / title / content 摘要 / score）。

参数：

| 字段 | 含义 |
|---|---|
| `q` | 查询串（必填） |
| `top` | 返回条数，默认 6 |
| `person` | 限定人物 id |
| `scope` | `all` / `person` / `folder` |
| `rerank` | `bge` / `off` |
| `since` / `until` | 时间过滤（unix 秒） |

## 写入

```bash
curl -X POST http://127.0.0.1:8095/remember \
  -H 'Content-Type: application/json' \
  -d '{"title":"带药","content":"他今天给我带了药","person_id":"user:10001","tags":["关心"]}'
```

写入会转发到 `MEM_INBOX`；内核自身不写库。

## 上下文（显式，不自动注入）

`ash
curl -X POST http://127.0.0.1:8095/context \
  -H 'Content-Type: application/json' \
  -d '{"person":"user:A","q":"带药","top":5,"max_chars":1200}'
# → {"text":"…","items":[{"id":"…","title":"…","score":…,"weight":…}]}
`

只生成文本，不注入；宿主自己决定是否放进 prompt。

## 关闭空闲会话

`ash
curl -X POST http://127.0.0.1:8095/sweep
# → {"closed":[{"session_id":"…","event_id":"…","importance":0.7,...}]}
`

## 日志（每回合）

`ash
curl -X POST http://127.0.0.1:8095/log \
  -H 'Content-Type: application/json' \
  -d '{"user":"今天好累","person":"user:A"}'
# → {"session_id":"s...","closed":null}
# 间隔超过 30 分钟后，closed 返回生成的会话事件
`

## 自动写入（一轮对话）

`ash
curl -X POST http://127.0.0.1:8095/ingest \
  -H 'Content-Type: application/json' \
  -d '{"user":"今天和同事吵架了","reply":"…","person":"user:A"}'
# → {"stored":[{"type":"relationship","id":"…","merged":false}],"skipped":""}
`

不需要用户说“记住”；规则只记事实/偏好/计划/承诺/关系/事件。

## 关系

```bash
curl -X POST http://127.0.0.1:8095/relation \
  -H 'Content-Type: application/json' \
  -d '{"subject":"user:10001","predicate":"照顾","object":"user:10002","note":"带药"}'

curl 'http://127.0.0.1:8095/relations?entity=user:10001'
```

## 解释（为什么）

```bash
curl -X POST http://127.0.0.1:8095/why \
  -H 'Content-Type: application/json' \
  -d '{"subject":"user:10001","q":"药"}'
```

返回：相关事件 + 关系边 + 证据。

## 人物档案

```bash
curl 'http://127.0.0.1:8095/profile?person_id=user:10001'
```

## 检索服务（8096）

```bash
curl 'http://127.0.0.1:8096/search?q=带药&top=5&mode=hybrid&rerank=off'
```

## 图谱服务（8094）

```bash
curl 'http://127.0.0.1:8094/relations?entity=user:10001'
```
