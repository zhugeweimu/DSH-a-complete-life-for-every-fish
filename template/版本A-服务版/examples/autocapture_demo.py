# -*- coding: utf-8 -*-
"""自动抽取演示：不给“记住”指令，普通对话也会落成记忆"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import autocapture as AC  # noqa: E402

DB = os.path.join(HERE, "autocapture_demo.db")
SCHEMA = os.path.join(ROOT, "sql", "schema.sql")

TURNS = [
    ("你好", "寒暄"),
    ("我特别喜欢吃辣，不喜欢甜的", "偏好"),
    ("明天我要去医院复查", "计划"),
    ("我今年32岁，在杭州上班", "事实"),
    ("今天和同事吵架了", "事件+关系"),
    ("记住，下周三交房租", "显式+计划"),
    ("今天天气怎么样？", "问句"),
    ("我特别喜欢吃辣，不喜欢甜的", "重复（应合并）"),
]


def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(open(SCHEMA, encoding="utf-8").read())
    con.commit()
    con.close()

    ts = 1760000000
    for i, (text, expect) in enumerate(TURNS):
        r = AC.capture_turn(text, person="user:A", ts=ts + i * 60, db=DB)
        stored = [s["type"] + ("(合并)" if s["merged"] else "") for s in r["stored"]]
        print("  %-22s → %-28s 期望：%s" % (text, stored or ("跳过：" + r["skipped"]), expect))

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    print("\n  最终落库：")
    for row in con.execute("SELECT memory_type,title,importance FROM booku_memory_records ORDER BY created_at"):
        print("   [%s] %s (imp=%.2f)" % (row["memory_type"], row["title"], row["importance"]))
    con.close()


if __name__ == "__main__":
    main()
