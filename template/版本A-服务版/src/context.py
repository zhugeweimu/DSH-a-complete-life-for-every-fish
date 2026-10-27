# -*- coding: utf-8 -*-
"""context —— 注入接口（只提供，不自动注入）

设计边界：
  · 本模块**不会**把记忆塞进任何 prompt、也不会替宿主决定上下文
  · 宿主（平台、桥、脚本）需要时显式调用 build()
  · 会话框自带上下文注入的，可以完全不用这个接口

build() 做的事：
  过滤（person）→ BM25 召回 → 时间衰减加权 → 按预算拼成一段文本
  返回 {"text": 可注入文本, "items": 明细}
"""
from __future__ import annotations

import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import hybrid as H  # noqa: E402

DEFAULT_MAX_CHARS = 1200


def _decay(ts, last_active, importance, now=None):
    now = float(now or time.time())
    base = max(float(ts or 0), float(last_active or 0), 0)
    if not base:
        return 0.0
    imp = max(0.05, min(1.0, float(importance or 0.5)))
    days = max(0.0, (now - base) / 86400.0)
    half_life = 7.0 + 365.0 * (imp ** 2)
    floor = 0.3 * imp
    w = floor + (1.0 - floor) * (2.0 ** (-days / half_life))
    return max(0.0, min(1.0, w))   # 0..1


def build(person="", q="", top=5, max_chars=DEFAULT_MAX_CHARS, db=None, now=None):
    path = db or os.environ.get("MEM_DB") or os.path.join(HERE, "..", "data", "metadata.db")
    docs = H.load_corpus(path)
    if person:
        docs = [d for d in docs if (d.get("person") or "") == person]
    if not docs:
        return {"text": "", "items": []}
    if q:
        bm = H.BM25(docs)
        ranked = [(s, i) for s, i in bm.search(q, top=max(top * 3, 10))]
    else:
        ranked = [(0.0, i) for i in range(len(docs))]
    scored = []
    for s, i in ranked:
        d = docs[i]
        w = _decay(d.get("ts"), d.get("last_activated"), d.get("importance"), now=now)
        scored.append((s * (0.4 + 0.6 * w), d, w))
    scored.sort(key=lambda x: -x[0])
    top_raw = scored[0][0] if scored else 1.0
    items, lines, used = [], [], 0
    for final, d, w in scored:
        line = "[%s] %s\n%s" % (
            time.strftime("%m-%d %H:%M", time.localtime(float(d.get("ts") or 0))),
            d.get("title") or "", (d.get("text") or "").strip().replace("\n", " ")[:160])
        if used + len(line) > max_chars:
            continue
        lines.append(line)
        used += len(line)
        items.append({"id": d.get("id"), "title": d.get("title"), "score": round(final, 4),
                      "score_norm": round(final / top_raw, 4) if top_raw else 0.0,
                      "weight": round(w, 4)})
        if len(items) >= top:
            break
    return {"text": "\n".join(lines), "items": items}
