# -*- coding: utf-8 -*-
"""迁移：给记忆库加 private 分区三列（owner / visibility / deletable）。

只做加法（ALTER TABLE ADD COLUMN，带默认值），不改任何已有数据语义：
  · 旧记录 visibility='shared'、deletable=1、owner=''
  · 迁移前先做一份 sqlite 备份，路径打印出来

用法：
  python src/migrate_visibility.py [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store as ST  # noqa: E402

DEFAULT_DB = os.environ.get("MEM_DB") or os.path.join(HERE, "..", "..", "mu-memory",
                                                      "data", "booku_memory", "metadata.db")


def backup(db_path):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = "%s.bak-private-%s" % (db_path, stamp)
    shutil.copy2(db_path, dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = os.path.abspath(a.db)
    if not os.path.exists(db):
        print("DB 不存在：%s" % db)
        return 2
    con = sqlite3.connect(db, timeout=10)
    before = con.execute("SELECT COUNT(*) FROM booku_memory_records").fetchone()[0]
    cols = {r[1] for r in con.execute("PRAGMA table_info(booku_memory_records)")}
    missing = [c for c, _ in ST.PRIVATE_COLUMNS if c not in cols]
    print("db=%s" % db)
    print("rows=%d missing_columns=%s" % (before, missing))
    if a.dry_run:
        con.close()
        print("dry-run：未改动")
        return 0
    bak = backup(db)
    print("backup=%s" % bak)
    ST._ensure_provenance(con)
    con.close()
    con = sqlite3.connect(db, timeout=10)
    cols = {r[1] for r in con.execute("PRAGMA table_info(booku_memory_records)")}
    stats = {}
    for c in ("visibility", "deletable", "owner"):
        if c in cols:
            stats[c] = dict(con.execute(
                "SELECT %s, COUNT(*) FROM booku_memory_records GROUP BY %s" % (c, c)).fetchall())
    after = con.execute("SELECT COUNT(*) FROM booku_memory_records").fetchone()[0]
    con.close()
    print("columns_now=%s" % sorted(c for c, _ in ST.PRIVATE_COLUMNS if c in cols))
    print("rows_after=%d stats=%s" % (after, stats))
    print("OK" if after == before else "MISMATCH rows %d -> %d" % (before, after))
    return 0 if after == before else 1


if __name__ == "__main__":
    sys.exit(main())
