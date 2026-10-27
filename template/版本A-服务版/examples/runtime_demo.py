# -*- coding: utf-8 -*-
"""记忆系统运行逻辑演示（只依赖 memory-core，不接任何其他系统）

流程：建库 → 写入（含去重）→ 建索引 → 召回（时间衰减）→ 图谱 → 档案 → 夜间维护
每一步打印 DB 状态，便于对照代码与文字。
"""
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import hybrid as H  # noqa: E402

DB = os.path.join(HERE, "runtime_demo.db")
NOW = 1760000000  # 固定时间，保证可复现


def init():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(open(os.path.join(ROOT, "sql", "schema.sql"), encoding="utf-8").read())
    con.commit()
    con.close()


def insert(title, content, person, tags, importance, created):
    con = sqlite3.connect(DB)
    mid = "m%d" % int(created)
    con.execute(
        "INSERT INTO booku_memory_records (memory_id,title,content,person_id,folder_id,memory_type,"
        "importance,created_at,updated_at,last_activated_at,activation_count,is_deleted)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
        (mid, title, content, person, "demo", "event", importance, created, created, created))
    for t in tags:
        con.execute("INSERT INTO booku_memory_tags (memory_id,tag_type,tag_value) VALUES (?,?,?)",
                    (mid, "core", t))
    con.commit()
    con.close()
    return mid


def jaccard(a, b):
    A, B = set(H.tokens(a)), set(H.tokens(b))
    return len(A & B) / max(1, len(A | B))


def find_duplicate(title, content):
    """近重：标题+内容 Jaccard > 0.8 视为同一条"""
    docs = H.load_corpus(DB)
    best, best_score = None, 0.0
    for d in docs:
        s = jaccard(title + content, d["title"] + d["text"])
        if s > best_score:
            best, best_score = d, s
    return best, best_score


def decay(ts, last_active, importance, now=NOW):
    base = max(float(ts or 0), float(last_active or 0), 0)
    if not base:
        return 0.0
    imp = max(0.05, min(1.0, float(importance or 0.5)))
    days = max(0.0, (now - base) / 86400.0)
    half_life = 7.0 + 365.0 * (imp ** 2)
    floor = 0.3 * imp
    return floor + (1.0 - floor) * (2.0 ** (-days / half_life))


def recall(q, top=3):
    docs = H.load_corpus(DB)
    bm = H.BM25(docs)
    out = []
    for score, i in bm.search(q, top=top * 2):
        d = docs[i]
        w = decay(d["ts"], d["last_activated"], d["importance"])
        out.append((d["title"], round(score, 3), round(w, 3), round(score * (0.4 + 0.6 * w), 3)))
        if len(out) >= top:
            break
    return out


def profile(person):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT title, importance, created_at FROM booku_memory_records "
                       "WHERE person_id=? AND is_deleted=0 ORDER BY created_at DESC", (person,)).fetchall()
    tags = [r[0] for r in con.execute(
        "SELECT t.tag_value FROM booku_memory_tags t JOIN booku_memory_records r "
        "ON t.memory_id=r.memory_id WHERE r.person_id=? GROUP BY t.tag_value ORDER BY COUNT(*) DESC",
        (person,))]
    con.close()
    return {"count": len(rows), "recent": [r["title"] for r in rows[:3]], "tags": tags[:5]}


def nightly():
    """夜间维护：低权重且过期的软删除；清理过期临时备忘"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT memory_id, created_at, last_activated_at, importance "
                       "FROM booku_memory_records WHERE is_deleted=0").fetchall()
    removed = []
    for r in rows:
        w = decay(r["created_at"], r["last_activated_at"], r["importance"])
        if w < 0.35 and r["importance"] < 0.4:
            con.execute("UPDATE booku_memory_records SET is_deleted=1 WHERE memory_id=?",
                        (r["memory_id"],))
            removed.append(r["memory_id"])
    con.execute("DELETE FROM booku_temporary_memos WHERE expires_at>0 AND expires_at < ?", (NOW,))
    con.commit()
    con.close()
    return removed


def main():
    print("== 1. 建库 ==")
    init()
    print("  表：booku_memory_records / tags / temporary_memos")

    print("\n== 2. 写入（带去重）==")
    day = 86400
    data = [
        ("带药", "他今天给我带了药，还叮嘱我按时吃", "user:A", ["关心"], 0.8, NOW - 2 * day),
        ("带药-重复", "他今天给我带了药，还叮嘱我按时吃。", "user:A", ["关心"], 0.8, NOW - day),
        ("考试", "明天要考试了，有点紧张", "user:A", ["学习"], 0.5, NOW - 3 * day),
        ("群聊刷屏", "哈哈哈哈哈哈", "user:B", ["群聊"], 0.15, NOW - 40 * day),
    ]
    for title, content, person, tags, imp, ts in data:
        dup, score = find_duplicate(title, content)
        if dup and score > 0.8:
            con = sqlite3.connect(DB)
            con.execute("UPDATE booku_memory_records SET activation_count=activation_count+1, "
                        "last_activated_at=? WHERE memory_id=?", (ts, dup["id"]))
            con.commit()
            con.close()
            print("  合并：%s → 已有 %s（Jaccard %.2f）" % (title, dup["title"], score))
        else:
            mid = insert(title, content, person, tags, imp, ts)
            print("  写入：%s（%s，重要度 %.2f）" % (title, mid, imp))

    print("\n== 3. 索引与召回（BM25 × 时间衰减）==")
    for q in ("带药", "考试", "哈哈"):
        print("  查询 %-4s → %s" % (q, recall(q, top=2)))

    print("\n== 4. 人物档案 ==")
    print("  user:A →", profile("user:A"))

    print("\n== 5. 夜间维护 ==")
    removed = nightly()
    print("  软删除（低权重+低重要度）：", removed or "无")

    print("\n== 6. 维护后再次召回 ==")
    for q in ("带药", "哈哈"):
        print("  查询 %-4s → %s" % (q, recall(q, top=2)))


if __name__ == "__main__":
    main()
