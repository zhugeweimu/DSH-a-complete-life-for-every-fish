# -*- coding: utf-8 -*-
"""consolidate —— 夜间巩固：只清理普通事件，重要事件不动

规则：
  · 普通事件：memory_type 为 session:plain / event / session:event
  · 条件：超过 archive_days（默认 30 天）且衰减权重 < min_weight（默认 0.35）
  · 动作：软删除（is_deleted=1），不物理删
  · 其余类型（偏好/计划/承诺/关系/事实/日记/自述/笔记）永不清理
"""
from __future__ import annotations

import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import hybrid as H  # noqa: E402

PLAIN_TYPES = ("session:plain", "event", "session:event")


def _db(db=None):
    return db or os.environ.get("MEM_DB") or os.path.join(HERE, "..", "data", "metadata.db")


def _decay(ts, last_active, importance, now):
    base = max(float(ts or 0), float(last_active or 0), 0)
    if not base:
        return 0.0
    imp = max(0.05, min(1.0, float(importance or 0.5)))
    days = max(0.0, (now - base) / 86400.0)
    half_life = 7.0 + 365.0 * (imp ** 2)
    floor = 0.3 * imp
    w = floor + (1.0 - floor) * (2.0 ** (-days / half_life))
    return max(0.0, min(1.0, w))


def run(db=None, now=None, archive_days=30, min_weight=0.35, idle_minutes=10):
    now = float(now or time.time())
    # 正在聊天就跳过，避免和写入抢锁
    try:
        con0 = sqlite3.connect(_db(db), timeout=10)
        row = con0.execute("SELECT MAX(ts) FROM turn_logs").fetchone()
        con0.close()
        if row and row[0] and now - float(row[0]) < float(idle_minutes) * 60:
            return {"skipped": "active", "scanned": 0, "archived": []}
    except Exception:
        pass
    docs = H.load_corpus(_db(db))
    archived = []
    con = sqlite3.connect(_db(db), timeout=10)
    try:
        for d in docs:
            if (d.get("type") or "") not in PLAIN_TYPES:
                continue
            age_days = (now - float(d.get("ts") or 0)) / 86400.0
            w = _decay(d.get("ts"), d.get("last_activated"), d.get("importance"), now)
            imp = max(0.05, min(1.0, float(d.get("importance") or 0.5)))
            if age_days >= archive_days and (w < min_weight or imp <= 0.4):
                con.execute("UPDATE booku_memory_records SET is_deleted=1 WHERE memory_id=?",
                            (d.get("id"),))
                archived.append({"id": d.get("id"), "title": d.get("title"),
                                 "weight": round(w, 3), "age_days": round(age_days, 1)})
        con.commit()
    finally:
        con.close()
    return {"archived": archived, "scanned": len(docs)}
