# -*- coding: utf-8 -*-
"""store —— 独立写入层（不依赖任何平台）

三种用法：
  1) 库调用：store.add(...) / store.add_batch([...])
  2) 命令行：python src/store_cli.py add --title ... --content ...
  3) HTTP：POST /remember（memory_core 在 MEM_WRITE_MODE=local 时走这里）

写入纪律：
  · 先校验（空内容拒绝）
  · 再去重（精确指纹 + 近重 Jaccard > 0.8）
  · 重复则合并：激活 +1、更新时间、标签并集、内容取较长的
  · 只做软删除标记，不物理删
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import hybrid as H  # noqa: E402  （同目录）

DEFAULT_DB = os.environ.get("MEM_DB") or os.path.join(HERE, "..", "data", "metadata.db")


def _db(db=None):
    return db or os.environ.get("MEM_DB") or DEFAULT_DB


def _now(ts=None):
    return float(ts if ts is not None else time.time())


def normalize_title(s):
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s[:80]


def fingerprint(title, content, person="", memory_type=""):
    raw = "%s|%s|%s|%s" % (normalize_title(title), (content or "").strip(), person or "", memory_type or "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def jaccard(a, b):
    A, B = set(H.tokens(a)), set(H.tokens(b))
    return len(A & B) / max(1, len(A | B))


def _importance(title, given=None):
    """重要度启发式：0.9 日记/自述、0.15 群聊、0.5 其余（可调，见 docs/PARAMETERS.md）"""
    try:
        if given is not None and float(given) > 0:
            return max(0.05, min(1.0, float(given)))
    except Exception:
        pass
    t = title or ""
    if t.startswith("日记") or t.startswith("自述"):
        return 0.9
    if t.startswith("群聊"):
        return 0.15
    return 0.5


def find_duplicate(db, title, content, person="", memory_type=""):
    """返回 (row|None, score, kind)，kind ∈ exact/near/none；不同类型不互相合并"""
    con = sqlite3.connect(_db(db))
    con.row_factory = sqlite3.Row
    fp = fingerprint(title, content, person, memory_type)
    row = con.execute(
        "SELECT memory_id,title,content,person_id,activation_count FROM booku_memory_records "
        "WHERE is_deleted=0 AND memory_id=?", (fp,)).fetchone()
    if row:
        con.close()
        return dict(row), 1.0, "exact"
    q = ("SELECT memory_id,title,content,person_id,activation_count FROM booku_memory_records "
         "WHERE is_deleted=0 AND (person_id=? OR person_id='')")
    args = [person or ""]
    if memory_type:
        q += " AND memory_type=?"
        args.append(memory_type)
    q += " ORDER BY created_at DESC LIMIT 300"
    rows = con.execute(q, args).fetchall()
    con.close()
    best, best_s = None, 0.0
    for r in rows:
        s = jaccard(title + " " + content, r["title"] + " " + r["content"])
        if s > best_s:
            best, best_s = dict(r), s
    # 个性化阈值：近重合并线 0.8，可调（见 docs/PARAMETERS.md）
    if best_s > 0.8:
        return best, best_s, "near"
    return None, best_s, "none"


def _merge(con, dup, title, content, tags, ts):
    new_content = content if len(content) > len(dup["content"] or "") else dup["content"]
    con.execute(
        "UPDATE booku_memory_records SET content=?, title=?, activation_count=activation_count+1, "
        "last_activated_at=?, updated_at=? WHERE memory_id=?",
        (new_content, normalize_title(title) or dup["title"], ts, ts, dup["memory_id"]))
    for t in tags or []:
        con.execute("INSERT INTO booku_memory_tags (memory_id,tag_type,tag_value) VALUES (?,?,?)",
                    (dup["memory_id"], "core", t))


def add(title, content, person="", tags=None, importance=None, folder="", memory_type="event",
        ts=None, db=None):
    """写入一条；返回 {"id","merged","dup_id","score"}"""
    title = normalize_title(title)
    content = (content or "").strip()
    if not content:
        raise ValueError("content 不能为空")
    if not title:
        title = content[:20]
    ts = _now(ts)
    con = sqlite3.connect(_db(db))
    try:
        dup, score, kind = find_duplicate(db, title, content, person, memory_type)
        if dup:
            _merge(con, dup, title, content, tags, ts)
            con.commit()
            return {"id": dup["memory_id"], "merged": True, "dup_id": dup["memory_id"],
                    "kind": kind, "score": round(score, 3)}
        mid = fingerprint(title, content, person, memory_type)
        imp = _importance(title, importance)
        con.execute(
            "INSERT INTO booku_memory_records (memory_id,title,content,person_id,folder_id,"
            "memory_type,importance,created_at,updated_at,last_activated_at,activation_count,is_deleted)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
            (mid, title, content, person or "", folder or "", memory_type, imp, ts, ts, ts))
        for t in tags or []:
            con.execute("INSERT INTO booku_memory_tags (memory_id,tag_type,tag_value) VALUES (?,?,?)",
                        (mid, "core", t))
        con.commit()
        return {"id": mid, "merged": False, "dup_id": None, "kind": kind, "score": round(score, 3)}
    finally:
        con.close()


def add_batch(items, db=None):
    return [add(item.get("title", ""), item.get("content", ""),
                person=item.get("person", item.get("person_id", "")),
                tags=item.get("tags") or [], importance=item.get("importance"),
                folder=item.get("folder", ""), memory_type=item.get("memory_type", "event"),
                ts=item.get("ts"), db=db) for item in items]


def ingest_jsonl(path, db=None):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(add_batch([json.loads(line)], db=db)[0])
    return out


def ingest_markdown(path, db=None):
    """按标题切分：以 # / ## / ### 开头的行为标题，其余为内容"""
    title, buf, out = "", [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if re.match(r"^#{1,3}\s+", line):
                if title or buf:
                    out.append(add(title, "\n".join(buf), db=db))
                title = re.sub(r"^#{1,3}\s+", "", line).strip()
                buf = []
            else:
                buf.append(line.rstrip())
    if title or buf:
        out.append(add(title, "\n".join(buf), db=db))
    return out
