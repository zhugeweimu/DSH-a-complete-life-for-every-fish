# -*- coding: utf-8 -*-
"""autocapture —— 自动事件/记忆抽取（不要求用户说“记住”）

输入一轮对话（用户话 + 可选回复），输出该落库的记忆条目：
  fact         事实：我叫/我今年/我在…工作/我住…
  preference   偏好：我喜欢/讨厌/不吃/受不了…
  plan         计划：明天/下周/打算/要去/约好…
  promise      承诺：我会/答应/一定/保证…
  relationship 关系：我和他/吵架/和好/分手/认识…
  event        事件：今天/昨天/刚才 + 动词（了/过）
  note         显式要求：记住/记一下/别忘（保留，但不再是唯一入口）

设计原则：
  · 默认保守：寒暄、纯问句、过短不记
  · 规则优先（零成本、可解释）；可选 LLM 兜底（MEM_EXTRACT_LLM=1）
  · 去重交给 store（精确 + 近重）
  · 只记用户的事实与计划，不替用户编造
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
import store as S  # noqa: E402

RULES = [
    ("fact", r"我(今年|现在)?\s*\d+\s*岁|我(叫|名字是)|我(在|于).{1,12}(工作|上班|上学|读书)|我住在?.{1,12}"),
    ("preference", r"我(很|最|特别|超)?(喜欢|爱|讨厌|不喜欢|受不了|爱吃|不吃|过敏)"),
    ("plan", r"(明天|后天|下周|下个月|下周末|以后|回头|打算|准备|计划|约好|要去|得去|想去)"),
    ("promise", r"(我会|我答应|一定|保证|说好了|绝不)"),
    ("relationship", r"(我和|跟他|跟她|和朋友|和同事|和家人).{0,20}(吵架|和好|分手|认识|关系|见面)"),
    ("event", r"(今天|昨天|刚才|刚刚|早上|中午|下午|晚上|周末|前几天).{0,30}(了|过|去|见|吃|买|聊|玩|睡)"),
    ("note", r"(记住|记一下|记着|别忘了|别忘记|帮我记)"),
]

SKIP_EXACT = {"你好", "在吗", "在么", "晚安", "早安", "哈哈", "嗯", "哦", "好的", "谢谢", "拜拜",
              "没事", "算了", "好的呢", "行", "可以", "收到"}
TYPE_IMPORTANCE = {"fact": 0.8, "preference": 0.7, "plan": 0.7, "promise": 0.8,
                   "relationship": 0.8, "event": 0.55, "note": 0.8}
TYPE_TAG = {"fact": "事实", "preference": "偏好", "plan": "计划", "promise": "承诺",
            "relationship": "关系", "event": "事件", "note": "指定记录"}


def detect(text):
    """返回命中的类型列表（按 RULES 顺序，去重）"""
    hits = []
    for kind, pat in RULES:
        if re.search(pat, text or ""):
            hits.append(kind)
    return hits


def skip_reason(text):
    t = (text or "").strip()
    if t in SKIP_EXACT:
        return "寒暄"
    if len(t) < 4:
        return "过短"
    if len(t) <= 12 and (t.endswith("吗") or t.endswith("呢") or t.endswith("?") or t.endswith("？")):
        return "纯问句"
    return ""


def _clean(text, limit=300):
    return re.sub(r"\s+", " ", (text or "").strip())[:limit]


def _time_words(text):
    return [w for w in ("今天", "昨天", "明天", "刚才", "早上", "中午", "下午", "晚上", "周末", "下周")
            if w in (text or "")][:3]


def extract_turn(user_text, reply_text="", person="", ts=None):
    """只做抽取，不写库。返回 items 列表或跳过原因。"""
    t = _clean(user_text)
    why = skip_reason(t)
    if why:
        return {"items": [], "skipped": why}
    kinds = detect(t)
    # 同一句只保留最具体的一类，避免重复占位
    if "note" in kinds:
        kinds = ["note"]
    elif "relationship" in kinds and "event" in kinds:
        kinds = [k for k in kinds if k != "event"] or ["event"]
    if not kinds:
        return {"items": [], "skipped": "无规则命中"}
    ts = float(ts or time.time())
    items = []
    for k in kinds:
        title = "%s·%s" % (TYPE_TAG[k], t[:18])
        items.append({
            "title": title,
            "content": t,
            "type": k,
            "tags": [TYPE_TAG[k]] + _time_words(t),
            "importance": TYPE_IMPORTANCE.get(k, 0.5),
            "ts": ts,
        })
    return {"items": items, "skipped": ""}


def capture_turn(user_text, reply_text="", person="", ts=None, db=None, include_reply=False):
    """抽取 + 写入；返回 {"stored":[...],"skipped":...}"""
    ex = extract_turn(user_text, reply_text, person, ts)
    stored = []
    for it in ex["items"]:
        r = S.add(it["title"], it["content"], person=person, tags=it["tags"],
                  importance=it["importance"], memory_type=it["type"], ts=it["ts"], db=db)
        stored.append({"type": it["type"], "id": r["id"], "merged": r["merged"]})
    if include_reply and reply_text:
        ex2 = extract_turn(reply_text, "", person, ts)
        for it in ex2["items"]:
            r = S.add(it["title"], it["content"], person=person, tags=it["tags"],
                      importance=it["importance"], memory_type=it["type"], ts=it["ts"], db=db)
            stored.append({"type": it["type"], "id": r["id"], "merged": r["merged"], "from": "reply"})
    return {"stored": stored, "skipped": ex["skipped"]}


def _llm_extract(text, ts):
    """可选：本地/远端小模型抽取（MEM_EXTRACT_LLM=1 且 MEM_EXTRACT_URL 可用）"""
    url = os.environ.get("MEM_EXTRACT_URL", "")
    if not url:
        return []
    prompt = ("从这句话里抽取值得长期记住的事实/偏好/计划/承诺/关系/事件。只输出 JSON 数组，"
              "每项 {\"type\":\"fact|preference|plan|promise|relationship|event\",\"title\":\"短标题\"}。"
              "没有就输出 []。\n句子：" + text)
    body = json.dumps({"model": "x", "temperature": 0, "max_tokens": 200,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = json.loads(r.read().decode())["choices"][0]["message"]["content"]
        arr = json.loads(raw[raw.find("["):raw.rfind("]") + 1])
        return [{"type": a.get("type", "event"), "title": a.get("title", text[:18]),
                 "content": text, "tags": ["自动"], "importance": 0.6, "ts": ts} for a in arr]
    except Exception:
        return []
