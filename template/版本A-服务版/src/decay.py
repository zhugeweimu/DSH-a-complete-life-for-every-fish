# -*- coding: utf-8 -*-
"""event_records 权重衰减：只归档事件，不动物流日志和 event_legacy。"""
from __future__ import annotations

import os
import sqlite3
import time

HALF_LIFE_DAYS = float(os.environ.get("EVENT_HALF_LIFE_DAYS", "30"))
ARCHIVE_BELOW = float(os.environ.get("EVENT_ARCHIVE_BELOW", "0.08"))


def run(db=None, now=None):
    db = db or os.environ.get("MEM_DB")
    if not db:
        return {"ok": False, "error": "MEM_DB 未设置"}
    now = float(now or time.time())
    con = sqlite3.connect(db, timeout=30)
    rows = con.execute(
        "SELECT id, event_type, importance, start_ts, weight FROM event_records "
        "WHERE is_archived=0"
    ).fetchall()
    updated = archived = 0
    for eid, etype, imp, start_ts, old_w in rows:
        age_days = max(0.0, (now - float(start_ts or now)) / 86400.0)
        w = float(imp or 0.35) * (0.5 ** (age_days / HALF_LIFE_DAYS))
        con.execute("UPDATE event_records SET weight=? WHERE id=?", (round(w, 6), eid))
        updated += 1
        if etype == "plain" and w < ARCHIVE_BELOW:
            con.execute("UPDATE event_records SET is_archived=1 WHERE id=?", (eid,))
            archived += 1
    con.commit()
    con.close()
    return {"ok": True, "updated": updated, "archived": archived,
            "half_life_days": HALF_LIFE_DAYS, "archive_below": ARCHIVE_BELOW}
