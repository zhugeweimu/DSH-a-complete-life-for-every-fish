# -*- coding: utf-8 -*-
"""C 段迁移：turn_logs↔event_records 关联化 + 8 条畸形逗号 id 映射。

纪律：
  · 先默认 **dry-run**：只建/填影子表（event_links_shadow / event_id_map_shadow），
    对真实表零写入，输出影响行数与对比结果；
  · `--apply` 前必须对比通过（映射一一对应、无歧义）；应用在一个事务里完成，
    原始 id 写进 event_id_map 保留出处；
  · `--rollback` 依据 event_id_map 反向还原（无映射则不动）；
  · 生产库先自行备份 metadata.db（本脚本不替你做备份）。

用法：
  python src/migrate_event_links.py --dry-run
  python src/migrate_event_links.py --apply
  python src/migrate_event_links.py --rollback
  python src/migrate_event_links.py --db <path> --dry-run
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

DEFAULT_DB = os.environ.get("MEM_DB") or os.path.join(HERE, "..", "..", "mu-memory",
                                                      "data", "booku_memory", "metadata.db")

SHADOW_LINKS = "event_links_shadow"
SHADOW_MAP = "event_id_map_shadow"

DDL_SHADOW = """
CREATE TABLE IF NOT EXISTS event_links_shadow (
    log_id INTEGER NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (log_id, event_id));
CREATE TABLE IF NOT EXISTS event_id_map_shadow (
    old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    migrated_at REAL NOT NULL DEFAULT 0);
"""


def _db(db=None):
    """解析可用库：显式 --db / MEM_DB / mu-memory 有效库；默认 0 字节库已停用。"""
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


def stage(db=None):
    """把可推导的关联/映射写进影子表，并返回对比报告（不写真实表）。"""
    path = _db(db)
    TL.ensure(path)
    con = sqlite3.connect(path, timeout=10)
    con.executescript(DDL_SHADOW)
    con.execute("DELETE FROM " + SHADOW_LINKS)
    con.execute("DELETE FROM " + SHADOW_MAP)
    links = 0
    missing = []
    multi_rows = 0
    closed_rows = 0
    for log_id, event_id in con.execute(
            "SELECT id, event_id FROM turn_logs WHERE closed=1 AND event_id!=''"):
        closed_rows += 1
        eids = [x.strip() for x in (event_id or "").split(",") if x.strip()]
        if len(eids) > 1:
            multi_rows += 1
        for eid in eids:
            row = con.execute("SELECT event_type FROM event_records WHERE id=?", (eid,)).fetchone()
            if row is None:
                missing.append({"log_id": log_id, "event_id": eid})
                continue
            con.execute("INSERT OR IGNORE INTO " + SHADOW_LINKS + " (log_id,event_id,kind) VALUES (?,?,?)",
                        (log_id, eid, row[0] or ""))
            links += 1
    bad_ids = [r[0] for r in con.execute("SELECT id FROM event_records WHERE id LIKE '%,%'")]
    now = time.time()
    for old in bad_ids:
        new = "ev-" + uuid.uuid4().hex
        con.execute("INSERT OR REPLACE INTO " + SHADOW_MAP +
                    " (old_id,new_id,reason,migrated_at) VALUES (?,?,?,?)",
                    (old, new, "malformed-comma-id", now))
    con.commit()
    n_links = con.execute("SELECT COUNT(*) FROM " + SHADOW_LINKS).fetchone()[0]
    n_map = con.execute("SELECT COUNT(*) FROM " + SHADOW_MAP).fetchone()[0]
    new_ids = [r[0] for r in con.execute("SELECT new_id FROM " + SHADOW_MAP)]
    uniq_ok = len(set(new_ids)) == len(new_ids) == n_map
    con.commit()
    # 对比：真实表现状
    real_links = 0
    try:
        real_links = con.execute("SELECT COUNT(*) FROM event_links").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    real_map = 0
    try:
        real_map = con.execute("SELECT COUNT(*) FROM event_id_map").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    con.close()
    return {
        "db": path,
        "closed_rows": closed_rows,
        "multi_value_rows": multi_rows,
        "shadow_links": n_links,
        "shadow_map": n_map,
        "malformed_ids": len(bad_ids),
        "missing_targets": missing[:20],
        "missing_target_count": len(missing),
        "real_links_now": real_links,
        "real_map_now": real_map,
        "ok_to_apply": (n_map == len(bad_ids) and len(set(bad_ids)) == len(bad_ids) and uniq_ok),
    }


def apply(db=None):
    """把影子表应用到真实表（事务内）；原始 id 进 event_id_map 以便回滚。"""
    path = _db(db)
    rep = stage(path)
    if not rep["ok_to_apply"]:
        rep["applied"] = False
        rep["error"] = "影子对比未通过，拒绝 apply"
        return rep
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.executescript(
            "CREATE TABLE IF NOT EXISTS event_links (log_id INTEGER NOT NULL, event_id TEXT NOT NULL,"
            " kind TEXT NOT NULL DEFAULT '', PRIMARY KEY (log_id,event_id));"
            "CREATE TABLE IF NOT EXISTS event_id_map (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL,"
            " reason TEXT NOT NULL DEFAULT '', migrated_at REAL NOT NULL DEFAULT 0);")
        con.execute("DELETE FROM event_links")
        con.execute("INSERT INTO event_links (log_id,event_id,kind) "
                    "SELECT log_id,event_id,kind FROM " + SHADOW_LINKS)
        n_map = 0
        for old, new, reason, at in con.execute(
                "SELECT old_id,new_id,reason,migrated_at FROM " + SHADOW_MAP):
            con.execute("UPDATE event_records SET id=? WHERE id=?", (new, old))
            con.execute("INSERT OR REPLACE INTO event_id_map (old_id,new_id,reason,migrated_at) "
                        "VALUES (?,?,?,?)", (old, new, reason, at))
            # turn_logs：单值直接换；多值列表里逐项换
            for (log_id, eid) in con.execute(
                    "SELECT id,event_id FROM turn_logs WHERE event_id LIKE ?", ("%" + old + "%",)).fetchall():
                parts = [x.strip() for x in (eid or "").split(",") if x.strip()]
                newv = ",".join(new if p == old else p for p in parts)
                con.execute("UPDATE turn_logs SET event_id=? WHERE id=?", (newv, log_id))
            con.execute("UPDATE event_links SET event_id=? WHERE event_id=?", (new, old))
            n_map += 1
        # C 收口：turn_logs.event_id 只保留单值（取列表里第一个仍存在的 event），多值信息已进 event_links
        n_norm = 0
        for log_id, eid in con.execute(
                "SELECT id,event_id FROM turn_logs WHERE event_id LIKE '%,%'").fetchall():
            parts = [x.strip() for x in (eid or "").split(",") if x.strip()]
            pick = ""
            for x in parts:
                if con.execute("SELECT 1 FROM event_records WHERE id=?", (x,)).fetchone():
                    pick = x
                    break
            con.execute("UPDATE turn_logs SET event_id=? WHERE id=?", (pick or (parts[0] if parts else ""), log_id))
            n_norm += 1
        rep["normalized_multi_value"] = n_norm
        con.commit()
        rep["applied"] = True
        rep["mapped_ids"] = n_map
        rep["links_written"] = con.execute("SELECT COUNT(*) FROM event_links").fetchone()[0]
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return rep


def rollback(db=None):
    """按 event_id_map 反向还原（只还原仍处于 new 状态的）。"""
    path = _db(db)
    con = sqlite3.connect(path, timeout=30)
    n = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute("SELECT old_id,new_id FROM event_id_map").fetchall()
        for old, new in rows:
            con.execute("UPDATE event_records SET id=? WHERE id=?", (old, new))
            for (log_id, eid) in con.execute(
                    "SELECT id,event_id FROM turn_logs WHERE event_id LIKE ?", ("%" + new + "%",)).fetchall():
                parts = [x.strip() for x in (eid or "").split(",") if x.strip()]
                newv = ",".join(old if p == new else p for p in parts)
                con.execute("UPDATE turn_logs SET event_id=? WHERE id=?", (newv, log_id))
            con.execute("UPDATE event_links SET event_id=? WHERE event_id=?", (old, new))
            con.execute("DELETE FROM event_id_map WHERE old_id=?", (old,))
            n += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {"db": path, "rolled_back": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", action="store_true")
    a = ap.parse_args()
    if a.rollback:
        print(json.dumps(rollback(a.db), ensure_ascii=False, indent=2))
        return 0
    if a.apply:
        print(json.dumps(apply(a.db), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(stage(a.db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
