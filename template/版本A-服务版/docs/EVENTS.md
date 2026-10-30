# 事件系统（EVENTS）——自动抽取与写入

> 目标：用户不需要说“记住”，正常说话就会留下该留的记忆。
> 显式“记住”仍然支持，但不再是唯一入口。

## 三层：日志 + 事件 + 语义记忆

```
日志层（turn_logs）       每回合原样追加，不过滤、不判断；不向量化
事件层（event_records）   会话关闭后按类型生成；独立检索
语义记忆（records）       事实 / 偏好 / 承诺 / 知识；长期保留
```

事件层不是把日志塞进向量库。日志只事实，事件才可召回；普通事件衰减后归档，日志仍在。

### 会话分段

```
同一个人，相邻两回合间隔 > SESSION_GAP_SEC（默认 1800 秒 = 30 分钟）
→ 上一段关闭
→ 会话内按类型分桶，每种类型生成一条 event_records
→ 命中七类：0.55–0.80
→ 没命中：plain，普通事件（默认 0.35）
```

日志行本身永不删除；普通事件只从检索层归档，不删 `turn_logs`，也不删 `event_legacy`。

### 最长会话 2 小时

`
同一段会话连续超过 SESSION_MAX_SEC（默认 7200 秒）
→ 先结算旧段，再开新段
防止群聊连续刷 8 小时只生成一条巨型事件
`

### 时间戳与结算（不靠轮询）

`
每条日志自带 ts（调用方给或写入时取）
append() 只做一次比较：ts - 上一条.ts > 1800 ？
sweep() 也只查 MAX(ts)，没有定时器、没有轮询时钟
`

### 多类型不合并（不会互相吞并）

`
同一段会话里出现 preference + plan + relationship
→ 拆成 3 条 event_records，各带各的重要度（0.7 / 0.7 / 0.8）
→ event_type 分别为 preference / plan / relationship
→ 每条独立 event_id，source_log_ids 记录来源日志
→ 不用逗号串当事件主键
`

### 权重曲线（事件是“可触发的记忆”）

```
weight = importance × 0.5^(年龄天数 / 30)
plain 且 weight < 0.08 → is_archived = 1
importance 高的非普通事件不因年龄自动归档
```

`/decay` 只更新 `event_records.weight`，只归档普通事件。  
`turn_logs` 与 `event_legacy` 永远不动，需要时可从日志重建事件。

## 一轮对话怎么处理

```
用户消息
  │
  ├─ 1. 只写 turn_logs：原文、时间、session_id
  │
  ├─ 2. 不按单轮写事件，不按单轮写向量
  │
  ├─ 3. 会话关闭（30 分钟空闲 / 2 小时上限 / sweep）
  │
  ├─ 4. 按类型分桶，生成 event_records
  │      每条带 event_id、start_ts/end_ts、source_log_ids
  │
  └─ 5. 普通事件按权重衰减，归档 event_records，日志保留
```

## 七类事件与触发

| 类型 | 触发（示例） | 重要度 | 落库标题 |
|---|---|---|---|
| `fact` | 我今年32岁 / 我在杭州上班 / 我住在… | 0.80 | 事实·… |
| `preference` | 我喜欢 / 讨厌 / 不吃 / 受不了 | 0.70 | 偏好·… |
| `plan` | 明天 / 下周 / 打算 / 要去 / 约好 | 0.70 | 计划·… |
| `promise` | 我会 / 答应 / 一定 / 保证 | 0.80 | 承诺·… |
| `relationship` | 和同事吵架 / 和他分手 / 认识… | 0.80 | 关系·… |
| `event` | 今天/昨天/刚才 + 做了某事 | 0.55 | 事件·… |
| `note` | 记住 / 记一下 / 别忘了 | 0.80 | 指定记录·… |

## 跳过规则（保守优先）

```
长度 < 4 字          → 过短
寒暄（你好/晚安/哈哈/好的/谢谢…） → 寒暄
≤12 字且以 吗/呢/？ 结尾 且无其他命中 → 纯问句
没有命中任何规则      → 无规则命中
```

## 为什么不是“全记”

```
全记 → 噪声淹没召回，重要的事反而浮不上来
只认“记住” → 不自然，用户要教 AI 做事
本方案 → 只记“关于用户的事实/偏好/计划/关系/事件”
```

## 可选：LLM 兜底

设 `MEM_EXTRACT_LLM=1` 且 `MEM_EXTRACT_URL` 指向一个 chat 接口，
规则没命中时由小模型判断是否值得记（失败自动回退规则，不阻断）。

## 调用方式

```bash
# 库
python -c "import autocapture as A; print(A.capture_turn('明天我要去考试', person='user:A'))"

# 命令行（按轮导入 JSONL）
python src/store_cli.py turns turns.jsonl

# HTTP
curl -X POST http://127.0.0.1:8095/ingest \
  -H 'Content-Type: application/json' \
  -d '{"user":"今天和同事吵架了","reply":"…","person":"user:A"}'
```

## 实测（examples/autocapture_demo.py）

```text
你好                  → 跳过：寒暄
我特别喜欢吃辣，不喜欢甜的   → preference
明天我要去医院复查        → plan
我今年32岁，在杭州上班     → fact
今天和同事吵架了         → relationship（不再重复记 event）
记住，下周三交房租        → note（不再重复记 plan）
今天天气怎么样？         → 跳过：纯问句
我特别喜欢吃辣，不喜欢甜的   → preference（合并）
```

## 可调参数

见 [PARAMETERS.md](PARAMETERS.md)：
近重阈值、跳过长度、各类型重要度、标签、LLM 开关。
