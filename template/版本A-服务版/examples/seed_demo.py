# -*- coding: utf-8 -*-
"""最小可运行示例：建库 → 塞 3 条演示记忆 → BM25 召回（不依赖任何外部服务）"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import hybrid as H  # noqa: E402

DB = os.path.join(HERE, "demo.db")


def main():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(open(os.path.join(ROOT, "sql", "schema.sql"), encoding="utf-8").read())
    rows = [
        ("m1", "带药", "他今天给我带了药，还叮嘱我按时吃", "user:10001", "关心"),
        ("m2", "考试", "明天要考试了，有点紧张", "user:10001", "学习"),
        ("m3", "烟花", "晚上一起去看烟花吧", "user:10002", "约定"),
    ]
    for mid, title, content, person, tag in rows:
        con.execute(
            "INSERT INTO booku_memory_records (memory_id,title,content,person_id,folder_id,"
            "memory_type,importance,created_at,updated_at,last_activated_at,activation_count,is_deleted)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            (mid, title, content, person, "demo", "event", 0.6, 1700000000, 1700000000, 0, 0))
        con.execute("INSERT INTO booku_memory_tags (memory_id,tag_type,tag_value) VALUES (?,?,?)",
                    (mid, "core", tag))
    con.commit()
    con.close()

    docs = H.load_corpus(DB)
    bm = H.BM25(docs)
    print("记忆条数:", len(docs))
    for q in ("带药", "烟花", "紧张"):
        res = bm.search(q, top=2)
        print("查询 %-4s →" % q, [(docs[i]["title"], round(s, 2)) for s, i in res])


if __name__ == "__main__":
    main()
