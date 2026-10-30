# -*- coding: utf-8 -*-
"""把旧版 booku_memory_records 的 event/session 记录迁移到 event_records。

用法：
  python tools/migrate_event_layer.py --db path/to/metadata.db --dry-run
  python tools/migrate_event_layer.py --db path/to/metadata.db --apply

原则：
  - turn_logs 原样保留，不向量化
  - 旧记录先复制到 event_legacy，再标记 is_deleted=1
  - 能按 event_id 合并的，生成一条 event_records
  - 不能合并的，转 legacy 事件，降权保留
"""
from __future__ import annotations

import argparse
import json
import sqlite3

LEGACY_TYPES = ("event", "session:plain", "session:event", "session:note",
                "session:promise", "session:plan")
EVENT_DDL = """
CREATE TABLE IF NOT EXISTS event_records (
    id TEXT PRIMARY KEY, person TEXT, start_ts REAL, end_ts REAL,
    summary TEXT, event_type TEXT, importance REAL, weight REAL,
    source_log_ids TEXT, legacy INTEGER DEFAULT 0, is_archived INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_legacy (
    id TEXT PRIMARY KEY, person TEXT, ts REAL, title TEXT, content TEXT,
    old_type TEXT, original_json TEXT
);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    marks = ",".join("?" * len(LEGACY_TYPES))
    old = cur.execute(
        "select count(*) from booku_memory_records where is_deleted=0 and memory_type in (%s)" % marks,
        LEGACY_TYPES).fetchone()[0]
    groups = cur.execute(
        "select event_id,person,min(ts) a,max(ts) b from turn_logs "
        "where event_id is not null and event_id!='' group by event_id,person").fetchall()
    print("旧 event/session 记录:", old)
    print("可重建事件组:", len(groups))
    if not args.apply:
        print("dry-run：未改库")
        return
    cur.executescript(EVENT_DDL)
    rows = cur.execute(
        "select * from booku_memory_records where is_deleted=0 and memory_type in (%s)" % marks,
        LEGACY_TYPES).fetchall()
    for r in rows:
        d = dict(r)
        cur.execute(
            "insert or replace into event_legacy(id,person,ts,title,content,old_type,original_json) "
            "values(?,?,?,?,?,?,?)",
            (d["memory_id"], d.get("person_id") or "", d.get("created_at") or 0,
             d.get("title") or "", d.get("content") or "", d.get("memory_type") or "",
             json.dumps(d, ensure_ascii=False)))
    for g in groups:
        logs = cur.execute(
            "select id,user_text,reply_text from turn_logs where event_id=? order by ts,id",
            (g["event_id"],)).fetchall()
        summary = "\n".join("主人：%s；幕：%s" % ((x["user_text"] or "").strip(),
                                                  (x["reply_text"] or "").strip())
                            for x in logs)
        cur.execute(
            "insert or replace into event_records"
            "(id,person,start_ts,end_ts,summary,event_type,importance,weight,source_log_ids,legacy,is_archived) "
            "values(?,?,?,?,?,?,?,?,?,0,0)",
            ("ev-" + g["event_id"], g["person"] or "qq:unknown", g["a"], g["b"], summary,
             "event", 0.7, 0.8, ",".join(str(x["id"]) for x in logs)))
    for r in rows:
        d = dict(r)
        cur.execute(
            "insert or replace into event_records"
            "(id,person,start_ts,end_ts,summary,event_type,importance,weight,source_log_ids,legacy,is_archived) "
            "values(?,?,?,?,?,?,?,?,?,1,0)",
            ("legacy-" + d["memory_id"], d.get("person_id") or "qq:3665613142",
             d.get("created_at") or 0, d.get("created_at") or 0,
             (d.get("title") or "") + "\n" + (d.get("content") or ""),
             d.get("memory_type") or "event", float(d.get("importance") or 0.3), 0.35, ""))
    cur.execute("update booku_memory_records set is_deleted=1 where memory_type in (%s)" % marks,
                LEGACY_TYPES)
    cur.execute(
        "update event_records set is_archived=1 where legacy=1 and id in "
        "(select 'legacy-'||event_id from turn_logs "
        " where event_id is not null and event_id!='')")
    con.commit()
    print("event_records:", cur.execute("select count(*) from event_records").fetchone()[0])
    print("event_legacy:", cur.execute("select count(*) from event_legacy").fetchone()[0])
    con.close()


if __name__ == "__main__":
    main()
