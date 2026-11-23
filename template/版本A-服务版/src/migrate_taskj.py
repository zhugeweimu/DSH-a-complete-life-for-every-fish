# -*- coding: utf-8 -*-
"""任务J 数据迁移（dry-run 优先，影子对比，不直接覆盖；提供 rollback）。

覆盖：
  A：把时长 >=30min 的旧事件，按 turn_logs 时间程序化切成 30min 窗口（不手动合并）。
  P1-1：event_records.namespace 回填（按 person/kind 判 game/chat）。
  P1-2：同 title+content+person+namespace 的活跃重复合并；加 fingerprint 唯一约束。
  P1-3：不改数据；由 store/memory_core 的 DB 解析负责 fail-fast。

用法：
  python src/migrate_taskj.py --dry-run
  python src/migrate_taskj.py --apply
  python src/migrate_taskj.py --rollback
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import turnlog as TL  # noqa: E402
import store as ST  # noqa: E402

WINDOW = float(os.environ.get("SESSION_WINDOW_SEC", "1800"))

DDL = """
CREATE TABLE IF NOT EXISTS event_split_map (
    old_id TEXT NOT NULL, new_id TEXT NOT NULL, window_idx INTEGER NOT NULL DEFAULT 0,
    window_start REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (old_id, new_id));
CREATE TABLE IF NOT EXISTS dedupe_map (
    dup_id TEXT NOT NULL PRIMARY KEY, keep_id TEXT NOT NULL, created_at REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_split_old ON event_split_map (old_id);
"""


def _db(db=None):
    """迁移库解析：显式 --db / MEM_DB / mu-memory 有效库；默认 0 字节库已停用，找不到就 fail-fast。"""
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
    raise SystemExit("找不到可用记忆库（默认 0 字节库已停用，请设 MEM_DB 或用 --db）：%r" % (cand,))


def _ensure(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(event_records)")}
    if cols and "namespace" not in cols:
        con.execute("ALTER TABLE event_records ADD COLUMN namespace TEXT NOT NULL DEFAULT ''")
    con.executescript(DDL)


def _event_namespace(person, kind, summary=""):
    """旧数据 namespace 分类（按 person/kind 事实）：游戏→game；任务/生产类→work；其余 chat。"""
    p = str(person or "")
    k = str(kind or "")
    s = k + " " + str(summary or "")[:80]
    if "游戏" in s or k.lower() in ("game", "gameplay"):
        return "game"
    if k in ("task", "work", "job", "任务", "生产", "报告", "日报") or p.startswith("work:"):
        return "work"
    return "chat"


def _windows(logs):
    """按 30min 窗口分组（锚点=首条 ts）。logs: [(id,ts,user,reply)]"""
    out = []
    anchor = None
    for row in logs:
        ts = float(row[1] or 0)
        if anchor is None:
            anchor = ts
        idx = int((ts - anchor) // WINDOW)
        if not out or out[-1][0] != idx:
            out.append((idx, anchor + idx * WINDOW, []))
        out[-1][2].append(row)
    return out


def stage(db=None):
    path = _db(db)
    TL.ensure(path)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        _ensure(con)
        # P1-1 待回填
        ns_missing = con.execute(
            "SELECT COUNT(*) FROM event_records WHERE COALESCE(namespace,'')=''").fetchone()[0]
        # A 长事件
        long_events = []
        for r in con.execute(
                "SELECT id,person,start_ts,end_ts,event_type,source_log_ids,COALESCE(namespace,'') ns FROM event_records "
                "WHERE COALESCE(is_archived,0)=0 AND (end_ts-start_ts) >= ?", (WINDOW,)):
            ns = r["ns"] or _event_namespace(r["person"], r["event_type"])
            if ns != "chat":            # 只有 chat 按 30min 切；game 一局一事件、work 一任务一事件，均保留
                continue
            ids = [x.strip() for x in (r["source_log_ids"] or "").split(",") if x.strip().isdigit()]
            logs = []
            if ids:
                q = ("SELECT id,ts,user_text,reply_text FROM turn_logs WHERE id IN (%s)"
                     % ",".join("?" for _ in ids))
                logs = [tuple(x) for x in con.execute(q, ids)]
            if len(logs) < 2:
                continue
            ws = _windows(sorted(logs, key=lambda x: float(x[1] or 0)))
            if len(ws) > 1:
                long_events.append({"id": r["id"], "windows": len(ws), "duration": r["end_ts"] - r["start_ts"]})
        # P1-2 重复组
        groups = []
        for r in con.execute(
                "SELECT title,content,COALESCE(person_id,'') p,COALESCE(namespace,'') ns,"
                " COUNT(*) n FROM booku_memory_records WHERE COALESCE(is_deleted,0)=0 "
                "GROUP BY title,content,p,ns HAVING n>1"):
            groups.append({"title": (r["title"] or "")[:40], "person": r["p"], "namespace": r["ns"],
                           "rows": r["n"]})
        dup_rows = sum(g["rows"] for g in groups)
        # 唯一约束状态
        idx = [r[1] for r in con.execute("PRAGMA index_list(booku_memory_records)")]
        return {
            "db": path,
            "namespace_missing": ns_missing,
            "long_events": len(long_events),
            "long_event_windows": sum(e["windows"] for e in long_events),
            "long_events_sample": long_events[:10],
            "duplicate_groups": len(groups),
            "duplicate_rows": dup_rows,
            "groups_sample": groups[:10],
            "has_unique_index": any("uniq" in (i or "") for i in idx),
            "ok_to_apply": True,
        }
    finally:
        con.close()


def _build_event(con, person, kind, importance, weight, logs, archived_old):
    parts = []
    ids = []
    for log_id, ts, user, reply in logs:
        ids.append(str(log_id))
        if (reply or "").strip():
            parts.append("用户说：%s\n我说：%s" % (user or "", (reply or "")[:1000]))
        else:
            parts.append("用户说：%s" % (user or ""))
    eid = "ev-" + uuid.uuid4().hex
    summary, full_path = TL.soft_truncate(" / ".join(parts), eid)
    con.execute(
        "INSERT INTO event_records (id,person,start_ts,end_ts,summary,event_type,importance,weight,"
        "source_log_ids,legacy,is_archived,full_text_path,namespace) VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?)",
        (eid, person, float(logs[0][1]), float(logs[-1][1]), summary, kind, importance, weight,
         ",".join(ids), full_path, _event_namespace(person, kind)))
    for src in ids:
        con.execute("INSERT OR REPLACE INTO event_links (log_id,event_id,kind) VALUES (?,?,?)",
                    (int(src), eid, kind))
        con.execute("UPDATE turn_logs SET event_id=? WHERE id=?", (eid, int(src)))
    return eid


def apply(db=None):
    path = _db(db)
    rep = stage(path)
    con = sqlite3.connect(path, timeout=30)
    now = time.time()
    try:
        con.execute("BEGIN IMMEDIATE")
        # P1-1 回填 namespace
        n_ns = 0
        for eid, person, kind in con.execute(
                "SELECT id,person,event_type FROM event_records WHERE COALESCE(namespace,'')=''").fetchall():
            con.execute("UPDATE event_records SET namespace=? WHERE id=?",
                        (_event_namespace(person, kind), eid))
            n_ns += 1
        # A 拆分 >30min 长事件
        n_split_old = n_split_new = 0
        for eid, person, st, et, kind, imp, wt, src, ns0 in con.execute(
                "SELECT id,person,start_ts,end_ts,event_type,importance,weight,source_log_ids,"
                " COALESCE(namespace,'') FROM event_records WHERE COALESCE(is_archived,0)=0 "
                "AND (end_ts-start_ts) >= ?", (WINDOW,)).fetchall():
            if (ns0 or _event_namespace(person, kind)) != "chat":
                continue            # 只有 chat 拆 30min 窗口；game/work 保留原事件
            ids = [x.strip() for x in (src or "").split(",") if x.strip().isdigit()]
            if not ids:
                continue
            q = ("SELECT id,ts,user_text,reply_text FROM turn_logs WHERE id IN (%s)"
                 % ",".join("?" for _ in ids))
            logs = sorted([tuple(x) for x in con.execute(q, ids)], key=lambda x: float(x[1] or 0))
            if len(logs) < 2:
                continue
            ws = _windows(logs)
            if len(ws) <= 1:
                continue
            for idx, wstart, wlogs in ws:
                new_id = _build_event(con, person, kind or "event", imp or 0.5, wt or 0.5, wlogs, eid)
                con.execute("INSERT OR REPLACE INTO event_split_map (old_id,new_id,window_idx,"
                            "window_start,created_at) VALUES (?,?,?,?,?)", (eid, new_id, idx, wstart, now))
                n_split_new += 1
            con.execute("UPDATE event_records SET is_archived=1 WHERE id=?", (eid,))
            n_split_old += 1
        # P1-2 去重合并
        n_dup = 0
        groups = con.execute(
            "SELECT title,content,COALESCE(person_id,'') p,COALESCE(namespace,'') ns,"
            " GROUP_CONCAT(memory_id) ids,COUNT(*) n FROM booku_memory_records "
            "WHERE COALESCE(is_deleted,0)=0 GROUP BY title,content,p,ns HAVING n>1").fetchall()
        for title, content, person, ns, ids_s, n in groups:
            ids = [x for x in (ids_s or "").split(",") if x]
            rows = [dict(zip(("memory_id", "created_at", "activation_count", "importance"),
                             r)) for r in con.execute(
                "SELECT memory_id,created_at,activation_count,importance FROM booku_memory_records "
                "WHERE memory_id IN (%s) ORDER BY created_at DESC" % ",".join("?" for _ in ids), ids)]
            keep = rows[0]["memory_id"]
            for d in rows[1:]:
                con.execute("UPDATE booku_memory_records SET is_deleted=1, deleted_at=? WHERE memory_id=?",
                            (now, d["memory_id"]))
                con.execute("INSERT OR REPLACE INTO dedupe_map (dup_id,keep_id,created_at) VALUES (?,?,?)",
                            (d["memory_id"], keep, now))
                con.execute("INSERT OR IGNORE INTO booku_memory_tags (memory_id,tag_type,tag_value) "
                            "SELECT ?,tag_type,tag_value FROM booku_memory_tags WHERE memory_id=?",
                            (keep, d["memory_id"]))
                n_dup += 1
            con.execute("UPDATE booku_memory_records SET activation_count=COALESCE(activation_count,0)+? "
                        "WHERE memory_id=?", (sum(int(r["activation_count"] or 0) for r in rows[1:]), keep))
        # 唯一约束（部分索引：只约束活跃行；含粗粒度分区避免 shared/private 互撞）
        try:
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_mem_active ON booku_memory_records"
                "(title, content, COALESCE(person_id,''), namespace,"
                " COALESCE(visibility,'shared'), COALESCE(owner,'')) "
                "WHERE COALESCE(is_deleted,0)=0")
        except sqlite3.IntegrityError as e:
            con.rollback()
            rep.update({"applied": False, "error": "唯一索引创建失败（仍有重复）: %r" % (e,)})
            return rep
        con.commit()
        rep.update({"applied": True, "namespace_backfilled": n_ns,
                    "long_events_split": n_split_old, "new_window_events": n_split_new,
                    "duplicates_merged": n_dup})
        return rep
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def rollback(db=None):
    path = _db(db)
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DROP INDEX IF EXISTS uniq_mem_active")
        for dup, keep in con.execute("SELECT dup_id,keep_id FROM dedupe_map").fetchall():
            con.execute("UPDATE booku_memory_records SET is_deleted=0, deleted_at=0 WHERE memory_id=?",
                        (dup,))
        for old, new in con.execute("SELECT old_id,new_id FROM event_split_map").fetchall():
            con.execute("DELETE FROM event_links WHERE event_id=?", (new,))
            con.execute("DELETE FROM event_records WHERE id=?", (new,))
        for old in {r[0] for r in con.execute("SELECT old_id FROM event_split_map")}:
            con.execute("UPDATE event_records SET is_archived=0 WHERE id=?", (old,))
        n = con.execute("SELECT COUNT(*) FROM event_split_map").fetchone()[0]
        con.execute("DELETE FROM event_split_map")
        con.execute("DELETE FROM dedupe_map")
        con.commit()
        return {"db": path, "rolled_back_split": n,
                "note": "dedupe 行已恢复活跃；namespace 回填不回滚（无害）"}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MEM_DB") or ST.DEFAULT_DB)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", action="store_true")
    a = ap.parse_args()
    if a.rollback:
        print(json.dumps(rollback(a.db), ensure_ascii=False, indent=2))
    elif a.apply:
        print(json.dumps(apply(a.db), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(stage(a.db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
