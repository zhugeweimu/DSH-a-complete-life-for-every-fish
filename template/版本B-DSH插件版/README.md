# dsh-memory-core（DSH 原生插件版）

把本地 **memory-core** 接进 DSH 的插件。它只做一件事：**注册记忆工具**，让 DSH 里的会话（包括接进 QQ 的会话）能主动翻记忆、写记忆、生成上下文。

**不自动注入上下文**——会话框、QQ 插件、桥要不要用，由它们自己调。

## 两种版本的关系

```
memory-core（服务版，HTTP :8095）
   ├─ AstrBot / 其他宿主插件 → HTTP 调 /recall /remember /log /context
   └─ dsh-memory-core（本插件，DSH 进程内）
          └─ 也是 HTTP 调同一个 8095，共享同一份 SQLite
```

- **服务版**：独立进程，任何语言、任何宿主都能接（AstrBot、桥、脚本）。
- **插件版**：DSH 原生（Cordis），注册工具给 DSH 会话用；不复制数据、不分叉逻辑。

## 安装

先确保 memory-core 在跑：

```bash
cd memory-core
python run.py --core        # 默认 :8095
```

再装插件：

```bash
dsh plugin --profile web add link:<本目录绝对路径>
```

重启 DSH，在会话里就能看到 `memory_recall` / `memory_remember` / `memory_context` / `memory_log` / `memory_sweep` 五个工具。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `coreUrl` | `http://127.0.0.1:8095` | memory-core 地址 |
| `personId` | 空 | 默认人物 id（可被工具参数覆盖） |

也认环境变量：`MEMORY_CORE_URL`、`MEMORY_PERSON`。

## 工具

| 工具 | 作用 | 对应接口 |
|---|---|---|
| `memory_recall` | 翻相关记忆 | `POST /recall` |
| `memory_remember` | 写一条记忆 | `POST /remember` |
| `memory_context` | 生成可注入文本（不注入） | `POST /context` |
| `memory_log` | 记这一轮日志（跨 30 分钟结算事件） | `POST /log` |
| `memory_sweep` | 结算空闲会话 | `POST /sweep` |

## 与 DSH QQ 插件的关系

- **dsh-onebot-qq**：自带“简易群记忆”，但只是群内短期记录；要长期记忆就装本插件，让会话调 `memory_recall` / `memory_remember`。
- **dsh-qq-bridge**：它把 QQ 消息注入固定 DSH 会话，会话里装本插件即可；每个 QQ 号可作为 `person` 隔离。
- **dsh-fish**：它有自己的事件流人格记忆（`~/.dsh-fish`），和本插件不冲突：人格归它，长期事实记忆归本插件。

## 测试

```bash
node --test                       # 4 项：声明/注册/失败可读/schema
node examples/integration.mjs     # 对运行中的 8095 做真实 recall + context
```

## 边界（写清楚，避免误用）

```
不自动注入：只注册工具，不碰 system prompt / 对话历史
不复制数据：所有数据在 memory-core 的 SQLite
不替宿主决定：QQ 桥/会话框自己决定何时调
写入模式：memory-core 侧 MEM_WRITE_MODE=auto 时转发 8001；独立部署设 local
```
