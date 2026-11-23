# private 分区与导出备份（2026-11-03）

## 数据模型

`booku_memory_records` 新增三列（迁移脚本 `src/migrate_visibility.py`，纯 ADD COLUMN）：

| 列 | 默认 | 含义 |
|---|---|---|
| `owner` | `''` | 归属人（如 `self:self`） |
| `visibility` | `shared` | `shared` / `private` |
| `deletable` | `1` | 用户侧是否可删；private 强制为 0 |

旧数据迁移后一律 `shared / deletable=1 / owner=''`，语义不变。

## 读写规则

- `POST /remember`：`visibility=private` 必须给 `owner`，且需要请求头
  `X-MEM-Private-Token: <MEM_PRIVATE_TOKEN>`；未配置令牌时一律 403（fail-closed）。
- `POST /recall`、`POST /context`：默认过滤掉 private；只有带令牌且
  `include_private=true` 才可见，且 `owner` 必须匹配。
- `DELETE /memories/<id>`：private → 403；`deletable=0` → 403；shared 可软删。
- `PUT` / `PATCH /memories/*`：405（没有修改接口，private 用户侧不可改）。
- `/list`：不返回 private。
- 分区分隔：`memory_id` 指纹含 visibility/owner，同文不会跨区合并。
- 启动服务：把 `MEM_PRIVATE_TOKEN` 放进启动环境（**不要写进仓库**），例如
  `set MEM_PRIVATE_TOKEN=<随机长串>` 后再跑 `start-memory-core.bat`。

## 导出备份

```bat
:: 只导 shared（给人翻/查/搬）
python src/export_memories.py --out D:\备份\柜子

:: 连 private 一起导（需要令牌）
set MEM_PRIVATE_TOKEN=***
python src/export_memories.py --out D:\备份\柜子 --include-private

:: 重放（保留 memory_id 与 tags）
python src/export_memories.py --import D:\备份\柜子\memories_xxx --db 目标.db
:: 或 HTTP：POST /export {"tag":"cabinet"}（private 需令牌 + include_private）
```

目录结构：

```
memories_<tag>_<ts>/
  memories.json     # 数据：记录 + 标签；图片只留 [[media:media/xxx]] 或 ![](media/xxx) 引用
  media/            # 图片 sidecar（原文件，按内容 sha1 去重）
  MANIFEST.json     # 文件清单 + sha256 + counts + 重放命令
memories_<tag>_<ts>.zip   # 以上整包
```

规则：

- **JSON 里不放图片字节**：`data:image/...;base64,` 会被解码成 `media/<sha1>.<ext>`，
  记录正文替换为 `[[media:media/xxx]]`；本地路径图片会被拷进 `media/`，正文改写成
  `media/xxx`（markdown 里就是 `![](media/xxx)`，人可直接看）。
- 同字节图片去重成一个 sidecar；`counts.media` 是 sidecar 文件数。
- 导入时按需把 sidecar 还原成 data URI（默认），找不到文件则保留引用。
- private 记录的图片不会进 shared 备份的 `media/`。

## 迁移与备份

- 迁移前自动备份：`metadata.db.bak-private-<YYYYmmdd_HHMMSS>`（与 DB 同目录）。
- 已在线上执行：`mu-memory/data/booku_memory/metadata.db`
  → 备份 `metadata.db.bak-private-20261103_055943`，1022 行全部 `shared`。

## 启用 / 关闭（task-14）

令牌只放在用户目录，**不进仓库、不进 bat 明文**：

```
C:\Users\WEI\.dsh\secrets\mem_private_token.txt   （ACL 仅当前用户 FullControl）
```

- `D:\qqaibot\start-memory-core.bat` 用 `set /p MEM_PRIVATE_TOKEN=<...token.txt` 注入环境后再启动 8095；
- AstrBot 插件 `remember_private` 先读环境变量，读不到再读同一个文件，请求带 `X-MEM-Private-Token`；
- `GET /capabilities` 的 `private_enabled` 可直接看是否启用；
- 改令牌/开关后必须重启 8095（环境变量在进程启动时读取）。

回滚/关闭：
1. 删掉或清空 `mem_private_token.txt`；
2. 去掉 `start-memory-core.bat` 里 4 行 `MEM_PRIVATE_TOKEN` 注入（可选）；
3. 重启 8095。
遗留 private 行不会被读出来（默认过滤），放回令牌 + 重启即恢复。

## 测试

```bat
python tests/test_private.py          # 7 例：分区/令牌/删保护/导出计数
python tests/test_export_media.py     # 3 例：JSON 无 base64、sidecar、MANIFEST、重放
python tests/test_private_http.py     # 11 例：真实服务（18095）写入/隔离/删除/导出
```
