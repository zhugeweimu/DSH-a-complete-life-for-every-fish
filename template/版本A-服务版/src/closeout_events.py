# -*- coding: utf-8 -*-
"""task-32 收尾：活跃 chat 事件 >30min 清零。

规则（与 MEMORY_SEGMENTATION.md 一致，不硬截断、不人工合并）：
  · 能按 turn_logs 时间切出多窗口 → 程序化切分（沿用 migrate_taskj 的窗口逻辑）
  · 日志不足/只落一个窗口 → 不切，标记 review_state='needs_review' 并 is_archived=1
  · game/work 不动（game 一局一事件、work 一任务一事件）
  · 未闭合会话先 sweep 补关（event_id='' 的行）

用法：python src/closeout_events.py --dry-run|--apply|--rollback
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import turnlog as TL  # noqa: E402
import migrate_taskj as MJ  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS review_map (
    event_id TEXT PRIMARY KEY, prev_archived INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0);
"""


def _db(db=None):
    cand = []
    if db:
        cand.append(db)
    if os.environ.get("MEM_DB"):
        cand.append(os.environ["MEM_DB"])
    cand.append(os.path.join(HERE, "..", "..", "mu-memory", "data", "booku_memory", "metadata.db"))
    for c in cand:
        p = os.path.abspath(c)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    raise SystemExit("找不到可用记忆库（请设 MEM_DB 或用 --db）")


def _ensure(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(event_records)")}
    if cols and "review_state" not in cols:
        con.execute("ALTER TABLE event_records ADD COLUMN review_state TEXT NOT NULL DEFAULT ''")
    con.executescript(DDL)


def _active_long(con):
    rows = []
    for r in con.execute(
            "SELECT id,person,start_ts,end_ts,event_type,importance,weight,source_log_ids,"
            " COALESCE(namespace,'chat') ns FROM event_records "
            "WHERE COALESCE(is_archived,0)=0 AND (end_ts-start_ts) >= ?", (MJ.WINDOW,)).fetchall():
        if r["ns"] != "chat":
            continue
        ids = [x.strip() for x in (r["source_log_ids"] or "").split(",") if x.strip().isdigit()]
        logs = []
        if ids:
            q = ("SELECT id,ts,user_text,reply_text FROM turn_logs WHERE id IN (%s)"
                 % ",".join("?" for _ in ids))
            logs = sorted([tuple(x) for x in con.execute(q, ids)], key=lambda x: float(x[1] or 0))
        wins = MJ._windows(logs) if len(logs) >= 2 else []
        rows.append({"id": r["id"], "logs": len(logs), "windows": len(wins),
                     "duration": round((r["end_ts"] or 0) - (r["start_ts"] or 0), 1)})
    return rows


def stage(db=None):
    path = _db(db)
    TL.ensure(path)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        _ensure(con)
        pending = TL.pending_sessions(db=path)
        long_rows = _active_long(con)
        split = [r for r in long_rows if r["windows"] > 1]
        stay = [r for r in long_rows if r["windows"] <= 1]
        return {"db": path,
                "unclosed_sessions": len(pending),
                "unclosed_turns": sum(p["turns"] for p in pending),
                "unclosed_detail": pending[:10],
                "active_over30m": len(long_rows),
                "splittable": len(split),
                "unsplittable": len(stay),
                "samples_split": split[:5],
                "samples_stay": stay[:8]}
    finally:
        con.close()


def apply(db=None):
    path = _db(db)
    rep = stage(path)
    now = time.time()
    # 1) 未闭合会话先补关（独立连接，避免和下面事务抢锁）
    swept = TL.sweep(now=now, db=path)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    n_split_old = n_split_new = n_review = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        _ensure(con)
        for r in _active_long(con):
            row = con.execute("SELECT * FROM event_records WHERE id=?", (r["id"],)).fetchone()
            if row is None:
                continue
            ids = [x.strip() for x in (row["source_log_ids"] or "").split(",") if x.strip().isdigit()]
            logs = []
            if ids:
                q = ("SELECT id,ts,user_text,reply_text FROM turn_logs WHERE id IN (%s)"
                     % ",".join("?" for _ in ids))
                logs = sorted([tuple(x) for x in con.execute(q, ids)], key=lambda x: float(x[1] or 0))
            wins = MJ._windows(logs) if len(logs) >= 2 else []
            if len(wins) > 1:
                for idx, wstart, wlogs in wins:
                    new_id = MJ._build_event(con, row["person"], row["event_type"] or "event",
                                             row["importance"] or 0.5, row["weight"] or 0.5, wlogs, row["id"])
                    con.execute("INSERT OR REPLACE INTO event_split_map (old_id,new_id,window_idx,"
                                "window_start,created_at) VALUES (?,?,?,?,?)",
                                (row["id"], new_id, idx, wstart, now))
                    n_split_new += 1
                con.execute("UPDATE event_records SET is_archived=1 WHERE id=?", (row["id"],))
                n_split_old += 1
            else:
                con.execute("INSERT OR REPLACE INTO review_map (event_id,prev_archived,created_at) "
                            "VALUES (?,?,?)", (row["id"], int(row["is_archived"] or 0), now))
                con.execute("UPDATE event_records SET is_archived=1, review_state='needs_review' "
                            "WHERE id=?", (row["id"],))
                n_review += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    # 终态
    con = sqlite3.connect(path, timeout=10)
    left = con.execute(
        "SELECT COUNT(*) FROM event_records WHERE COALESCE(is_archived,0)=0 "
        "AND COALESCE(namespace,'chat')='chat' AND (end_ts-start_ts)>=?", (MJ.WINDOW,)).fetchone()[0]
    con.close()
    rep.update({"applied": True, "swept_sessions": len(swept),
                "split_old": n_split_old, "split_new_windows": n_split_new,
                "archived_needs_review": n_review,
                "active_over30m_after": left})
    return rep


def rollback(db=None):
    path = _db(db)
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        n_split = 0
        for old, new in con.execute("SELECT old_id,new_id FROM event_split_map").fetchall():
            con.execute("DELETE FROM event_links WHERE event_id=?", (new,))
            con.execute("DELETE FROM event_records WHERE id=?", (new,))
            n_split += 1
        for old in {r[0] for r in con.execute("SELECT old_id FROM event_split_map")}:
            con.execute("UPDATE event_records SET is_archived=0 WHERE id=?", (old,))
        n_rev = 0
        for eid, prev in con.execute("SELECT event_id,prev_archived FROM review_map").fetchall():
            con.execute("UPDATE event_records SET is_archived=?, review_state='' WHERE id=?",
                        (int(prev), eid))
            n_rev += 1
        con.execute("DELETE FROM event_split_map")
        con.execute("DELETE FROM review_map")
        con.commit()
        return {"db": path, "restored_split_events": n_split, "restored_review_events": n_rev}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MEM_DB") or "")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", action="store_true")
    a = ap.parse_args()
    if a.rollback:
        out = rollback(a.db or None)
    elif a.apply:
        out = apply(a.db or None)
    else:
        out = stage(a.db or None)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
