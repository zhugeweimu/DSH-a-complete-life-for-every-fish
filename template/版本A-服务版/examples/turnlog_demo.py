# -*- coding: utf-8 -*-
"""日志层演示：每回合日志 → 30 分钟分段 → 会话事件 → 权重曲线"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import turnlog as TL  # noqa: E402
import memory_core as MC  # noqa: E402

DB = os.path.join(HERE, "turnlog_demo.db")
SCHEMA = os.path.join(ROOT, "sql", "schema.sql")

MIN = 60
TURNS = [
    (0, "我喜欢喝咖啡"),
    (5 * MIN, "明天要考试，有点紧张"),
    (36 * MIN, "今天好累"),
    (40 * MIN, "随便聊聊"),
    (41 * MIN, "哦哦"),
    (80 * MIN, "晚安"),
]


def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(open(SCHEMA, encoding="utf-8").read())
    con.commit()
    con.close()

    base = 1760000000
    print("== 逐回合追加日志 ==")
    for dt, text in TURNS:
        r = TL.append("user:A", text, ts=base + dt, db=DB)
        note = ""
        if r["closed"]:
            c = r["closed"]
            note = "→ 关闭 %d 个事件（imp=%.2f，%s）" % (len(c["event_ids"]), c["importance"],
                                                     ",".join(c["kinds"]) or "普通")
        print("  +%3d 分钟  %-18s  会话 %s  %s" % (dt // 60, text, r["session_id"], note))

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    print("\n== 日志层（原样留存）==")
    for row in con.execute("SELECT ts,user_text,session_id,closed,event_id FROM turn_logs ORDER BY ts"):
        print("  +%3d 分钟  %-18s session=%s closed=%d event=%s" % (
            (row["ts"] - base) // 60, row["user_text"], row["session_id"], row["closed"], row["event_id"]))
    print("\n== 事件层（会话关闭后生成）==")
    for row in con.execute("SELECT memory_type,title,importance FROM booku_memory_records ORDER BY created_at"):
        print("  [%s] %-28s imp=%.2f" % (row["memory_type"], row["title"], row["importance"]))
    con.close()

    print("\n== 权重曲线（imp=0.70 vs 0.35，天数 → 权重）==")
    import time as _t
    now = _t.time()
    for imp in (0.70, 0.35):
        vals = []
        for d in (0, 1, 7, 30, 90, 365):
            ts = now - d * 86400
            vals.append("%dd=%.2f" % (d, MC._decay(ts, ts, imp)))
        print("  imp=%.2f  %s" % (imp, "  ".join(vals)))


if __name__ == "__main__":
    main()
