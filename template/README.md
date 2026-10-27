# 记忆本体（memory-core）

**给 AI 的长期记忆：日志 + 事件 + 自述/日记，不靠上下文硬撑。**

上下文是工作台，不是档案柜。这个项目把“过去”放到窗口外面：
每回合留痕，会话结束成事件，需要时按线索翻出来。

## 解决的问题

| 问题 | 做法 |
|---|---|
| 拿上下文当长期记忆：窗口一满/一压缩，前面的事就没了 | 记忆独立存 SQLite，窗口只放“这次要用的” |
| 用户得主动喊“记住”“去看看日记”，否则 AI 不会记 | 每回合自动写日志；跨 30 分钟自动结算成会话事件 |
| 换窗口、重开会话，身份就断了 | 自述 + 日记 + 事件都在库里，新会话读得回来 |
| 不敢压缩上下文，怕丢东西 | 记忆可随时召回，压缩不再等于遗忘 |

**平台接 QQ 时效果更明显；在 DSH 窗口里养鱼，切换窗口也不影响连续性。**

## 它怎么“回忆”

```
第一层：日志        每回合原样追加（时间戳），不判断、不删除
第二层：事件        同一段会话（间隔 >30 分钟）关闭时生成
                    命中事实/偏好/计划/承诺/关系/事件/笔记 → 按类型各成一条事件，带重要度
第三层：召回        线索（BM25/可选向量）× 时间衰减 × 重要度
                    只取 top-k，不把整个库塞回上下文
第四层：注入        宿主显式调用 /context 或工具；本系统不自动注入
```

**像人回忆，而不是像读档**：先有线索，才翻到相关的那几段；时间越久、越不重要，越难被想起；常用的会被重新激活。

## 两个版本

```
版本A-服务版/        独立进程，HTTP :8095，任何宿主都能接
版本B-DSH插件版/     DSH 原生插件，只注册工具，调同一个 8095
                     → 共用一份库，不复制数据
```

**可以只装 A；装 B 必须先有 A。**

## 包含什么

| 能力 | 说明 |
|---|---|
| 记忆存储 | 记录/标签/临时备忘 |
| 日志层 | 每回合追加，带时间戳 |
| 会话事件 | 30 分钟分段，按类型拆，带重要度 |
| 自述 | `self.md` 追加式身份记录，同时进库 |
| 日记 | 一天一个文件，同时进库 |
| 检索 | BM25 + 可选向量 + 可选重排 |
| 遗忘曲线 | 半衰期 + 下限，权重恒在 0–1 |
| 自动抽取 | 事实/偏好/计划/承诺/关系/事件/笔记 |
| 回忆接口 | `/recall` `/context`（显式，不自动注入） |
| 可看的文件夹 | `<记忆库目录>/vault/`：索引 + 每日记忆 + 日记 + 自述 |
| DSH 工具 | 8 个：recall/remember/context/log/diary/self/export/sweep |

## 目录

```
开源版/
├─ README.md
├─ 版本A-服务版/
│   ├─ src/            memory_core / store / autocapture / turnlog / diary / vault / hybrid
│   ├─ sql/schema.sql
│   ├─ tests/          30 项 Python 测试
│   └─ examples/
└─ 版本B-DSH插件版/
    ├─ lib/index.js    8 个工具
    ├─ adapters/       onebot-qq 子插件 / aurambot 模块（可选）
    └─ test/           13 项 Node 测试
```

## 一封信

包内根目录有 [letter.md](letter.md)：给所有大肥鱼和主人的一封信。下载后就在本地，随时可看。

## 快速开始

```bash
# 版本A
cd 版本A-服务版
python -m pip install -r requirements.txt
python run.py --init-db
python examples/seed_demo.py
python -m unittest discover tests

# 写一条日记 / 自述
python src/store_cli.py diary --text "今天把记忆系统跑通了"
python src/store_cli.py self  --text "我是一个会记住事情的 agent"

# 导出可看的文件夹（默认 <库目录>/vault/）
python src/store_cli.py export

# 版本B
cd ../版本B-DSH插件版
node --test
```

## 可看的文件夹（vault）

写入后自动导出到记忆库旁边的 `vault/`：

```
vault/
├─ index.md                 全部记忆索引
├─ memories/YYYY-MM-DD/     每条记忆一个 .md（frontmatter 带 id/时间/类型）
├─ 日记/YYYY-MM-DD.md
└─ 自述.md
```

没有 Web UI，但用户可以直接用文件管理器/编辑器翻。

## 边界

```
单机 SQLite，无多租户
不自动注入上下文：塞不塞由宿主决定
不做人格成长/情绪/关系（只做记忆）
vault 是导出视图，事实源仍是 SQLite
```

## 来源与原创性

- 参考过：Neo-MoFox 的 booku_memory（存储接口形态）、dsh-world（日记/自述/回顾思路）。
- 自写部分：日志/事件两层、30 分钟会话分段、按类型拆事件、7 类自动抽取、遗忘曲线、召回打分、显式注入边界、vault 导出、DSH 适配。
- 未复制上游源码；表名沿用 ooku_memory_* 仅为兼容旧数据。
- 详见 [NOTICE.md](NOTICE.md)。若后续引入上游代码，必须按 AGPL-3.0 处理。

## 许可

Apache-2.0。
