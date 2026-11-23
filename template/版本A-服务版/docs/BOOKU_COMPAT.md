# 8001 booku-memory 兼容层（8095，2026-11-03）

CE 等旧客户端按 8001 的 `booku-memory` 协议发请求；8001 已停用，8095 是唯一写入口。
本兼容层让 8095 直接认这套路径，内部转本地 `remember` / `recall`，**不与 8001 双写**。

## 接口

| 方法 | 路径 | 映射 |
|---|---|---|
| POST | `/booku-memory/api/memories` | `booku_remember` → `store.add`（booku 形状：folder_id/bucket/memory_type/status/core_tags/diffusion_tags/opposing_tags） |
| GET | `/booku-memory/api/memories?limit=&offset=&person_id=&folder_id=` | 列表（默认不含 private） |
| GET/POST | `/booku-memory/api/search?q=&limit=&person_id=&namespace=` | `recall`（rerank=light，默认 namespace=all），返回 `{items:[{id,title,content,text,source,person_id,created_at,score}], total, mode, q}` |

写入返回：`{id, success, merged, mode, item:{id,title,folder_id,status}}`。

- private 记忆不会出现在 list/search（除非带令牌 + include_private 走原生 `/recall`）。
- 写入私有仍需 `X-MEM-Private-Token`（走原生 `/remember`；booku 兼容写入默认 shared）。

## CE 接线（部署侧）

`D:\幕-记忆库\ce\config.json` 与源 `agent-neurofusion\ce\config.json` 两处同步：

```json
"mem_write": "http://127.0.0.1:8095/booku-memory/api/memories",
"memory_search_url": "http://127.0.0.1:8095/booku-memory/api/search"
```

改完重启 CE（`start-ce.bat`）。CE 代码零改动：`[记:]` 仍发 booku 形状，`memory_search()` 仍按 `{items:[{text|content|title, source|id}]}` 解析。

## 验证

- 直接 POST 兼容写入 → 200 `{id, success:true}`；`GET /booku-memory/api/search?q=<唯一标记>` 命中该 id。
- CE 侧 `do_tool('查','ZXQ7391-KB')` → 命中 8095 记录（无文件命中时自动回落 memory_search）；`do_tool('记',...)` → 写库成功。
- 同一内容只写一条（去重按 memory_id 指纹）。

## 回滚

1. CE config 两处改回 `http://127.0.0.1:8001/...`（备份：`config.json.bak-8001-*` / `config.json.bak-8001`）；
2. 重启 CE；8095 的兼容路由保留也无害（纯新增）。
