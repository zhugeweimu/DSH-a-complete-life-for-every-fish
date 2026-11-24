# -*- coding: utf-8 -*-
"""记忆目录（索引层）：把 1589 个事件、44 个块压成一张「有哪些事」的目录。

三层：目录（这里）→ 骨架（块的 title/summary）→ 分片（事件里逐回合的
turn_logs / booku_memory_records）。目录只给标题和一句话，点开 /catalog/<id>
才展开。

全部只读：两个库都用 mode=ro 打开，这个模块里没有任何 execute 写语句。
"""

import os
import re
import sqlite3
import math
import time

import blocks_direct as BD

_LEAD = re.compile(r"^(?:[^\s：:，。]{1,10}说)[：:]\s*")
_SENT = re.compile(r"[。！？!?]")


def _ro(path):
    con = sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"),
                          uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _flat(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _first(text, limit=60):
    """首句：先取第一行（事件的 summary 是「用户说：…\\n助手说：…」的拼接），
    去掉「用户说：」这类前缀，截到第一个句号/问号/叹号，再按字数封顶。"""
    s = str(text or "").split("\n", 1)[0]
    s = _LEAD.sub("", _flat(s))
    m = _SENT.search(s)
    if m and m.start() > 0:
        s = s[:m.start()]
    s = s.strip(" 　…，,、；;。")
    return s[:limit]


def _count_sentences(text):
    segs = [p for p in re.split(r"(?<=[。！？!?])\s*|\n+", str(text or "")) if p.strip()]
    return max(1, len(segs))


def _persons(person):
    """事件库里的 person 是 user:qq:...；调用方多半传 qq:...，两种都认。"""
    p = str(person or "").strip()
    if not p:
        return []
    out = [p]
    if p.startswith("user:"):
        out.append(p[5:])
    else:
        out.append("user:" + p)
    return out


def _grams(q):
    """把一句话切成 2 字组：「这两天为什么反复改注入」不该整串去 LIKE，
    切出来的「注入」「反复」才是目录里真能命中的东西。"""
    s = re.sub(r"\s+", "", str(q or "")).lower()
    if not s:
        return []
    if re.fullmatch(r"[a-z0-9_-]+", s):
        return [s] if len(s) > 1 else []
    out = []
    for i in range(len(s) - 1):
        g = s[i:i + 2]
        if re.match(r"[\u4e00-\u9fff]", g[0]):
            out.append(g)
    return list(dict.fromkeys(out))[:10]


def _event_rows(con, person, limit, grams=None):
    sql = ("SELECT e.id, e.person, e.start_ts, e.end_ts, e.summary, "
           "COUNT(l.log_id) AS n_items "
           "FROM event_records e LEFT JOIN event_links l ON l.event_id = e.id "
           "WHERE COALESCE(e.is_archived,0)=0")
    args = []
    ps = _persons(person)
    if ps:
        sql += " AND e.person IN (%s)" % ",".join("?" for _ in ps)
        args.extend(ps)
    if grams:
        sql += " AND (%s)" % " OR ".join("e.summary LIKE ?" for _ in grams)
        args.extend(["%" + g + "%" for g in grams])
    sql += " GROUP BY e.id ORDER BY e.start_ts DESC LIMIT ?"
    args.append(int(limit))
    return con.execute(sql, args).fetchall()


def _first_logs(con, ids):
    """每个事件最早的那条 turn_logs —— 目录标题就取它对应的 record。"""
    if not ids:
        return {}
    rows = con.execute(
        "SELECT l.event_id, t.id AS log_id, t.ts, t.user_text "
        "FROM event_links l JOIN turn_logs t ON t.id = l.log_id "
        "WHERE l.event_id IN (%s) ORDER BY t.ts ASC" % ",".join("?" for _ in ids),
        list(ids)).fetchall()
    first = {}
    for r in rows:
        first.setdefault(r["event_id"], r)
    return first


def _record_title(con, person, ts, fallback):
    """首条 record 的 title；对不上就退回该回合的 user_text（两者本来就同源）。"""
    try:
        r = con.execute(
            "SELECT title FROM booku_memory_records "
            "WHERE person_id=? AND memory_type='event' AND COALESCE(is_deleted,0)=0 "
            "AND abs(created_at-?) < 3 ORDER BY abs(created_at-?) LIMIT 1",
            (person, ts, ts)).fetchone()
    except sqlite3.Error:
        r = None
    title = _first(r["title"], 60) if r else ""
    return title or _first(fallback, 60)


def _title_of(title, summary, fallback_id):
    """游戏回合那类注入文本进来时，标题会变成 "{'text': '【游戏回合】…"，
    目录里不该出现这种。跳过去，用摘要首句顶上。"""
    t = _flat(title or "")
    if t.startswith("{") or t.startswith("["):
        t = ""
    return t or _first(summary, 40) or fallback_id


def _weights(grams, blocks):
    """2 字组里「什么」「这两天」这类到处都有的词得压权重，否则目录永远先
    给出「坏心眼是什么样」。用块库自身算 df，词越常见权重越低。"""
    if not grams or not blocks:
        return {g: 1.0 for g in grams}
    corpus = [(_flat(b.get("title")) + " " + (b.get("summary") or "")).lower()
              for b in blocks]
    out = {}
    for g in grams:
        df = sum(1 for c in corpus if g in c)
        out[g] = 1.0 + math.log(max(1, len(blocks)) / (1.0 + df))
    return out


def list_items(db, person="", limit=20, kind="", q=""):
    """目录列表：事件 + 块。

    混排时给块留名额（limit//3，至少 2 条）：今天的聊天事件永远比块新，
    纯按时间截前 N 条的话块永远挤不进来，而块恰恰是「骨架」那一层。
    有关键词 q 时块命中优先——块标题里带词就是一条结实的线索。
    """
    limit = max(1, min(200, int(limit or 20)))
    kind = kind if kind in ("event", "block") else ""
    items = []
    grams = _grams(q)
    allb = BD.load(limit=10000)
    wt = _weights(grams, allb)
    con = _ro(db)
    try:
        if kind in ("", "event"):
            rows = _event_rows(con, person, limit * 6 if grams else limit, grams)
            firsts = _first_logs(con, [r["id"] for r in rows])
            scored = []
            for r in rows:
                sc = (sum(wt[g] for g in grams if g in (r["summary"] or "").lower())
                      if grams else 0)
                if grams and sc <= 0:
                    continue
                f = firsts.get(r["id"])
                title = _record_title(con, r["person"], f["ts"], f["user_text"]) if f else ""
                scored.append((sc, {
                    "id": r["id"], "kind": "event",
                    "title": _title_of(title, r["summary"], r["id"][:12]),
                    "one_line": _first(r["summary"], 60),
                    "start_ts": r["start_ts"] or 0.0,
                    "end_ts": r["end_ts"] or r["start_ts"] or 0.0,
                    "n_items": int(r["n_items"] or 0),
                }))
            if grams:
                scored.sort(key=lambda t: (-t[0], -(t[1]["start_ts"] or 0)))
            items = [x for _, x in scored]
    finally:
        con.close()
    blocks = []
    if kind in ("", "block"):
        scored_b = []
        for b in allb:
            title = _flat(b.get("title")) or b.get("id")
            summary = b.get("summary") or ""
            # 标题命中的分量比正文重：块标题是人工总结的那一句。
            sc = (sum(wt[g] for g in grams if g in title.lower()) * 3
                  + sum(wt[g] for g in grams if g in summary.lower())) if grams else 0
            if grams and sc <= 0:
                continue
            scored_b.append((sc, {
                "id": b["id"], "kind": "block",
                "title": title[:60],
                "one_line": _first(summary, 60),
                "start_ts": b.get("start_ts") or 0.0,
                "end_ts": b.get("end_ts") or 0.0,
                "n_items": _count_sentences(summary),
            }))
        scored_b.sort(key=lambda t: (-t[0], -(t[1]["start_ts"] or 0)))
        blocks = [x for _, x in scored_b]
    if kind == "block":
        return blocks[:limit]
    if kind == "event":
        return items[:limit]
    if grams:
        # 搜索时块是「骨架」，先出；事件按命中词数排后面。
        quota = min(limit, max(2, limit // 2))
        return (blocks[:quota] + items[:max(0, limit - len(blocks[:quota]))])
    quota = min(limit, max(2, limit // 3))
    take_b = blocks[:quota]
    take_e = items[:max(0, limit - len(take_b))]
    merged = take_b + take_e
    merged.sort(key=lambda x: -(x["start_ts"] or 0))
    return merged


def _event_detail(con, eid, full=False):
    row = con.execute(
        "SELECT id,person,start_ts,end_ts,summary FROM event_records WHERE id=?",
        (eid,)).fetchone()
    if not row:
        return None
    logs = con.execute(
        "SELECT l.log_id, t.id AS tid, t.ts, t.user_text, t.reply_text "
        "FROM event_links l "
        "JOIN turn_logs t ON t.id = l.log_id WHERE l.event_id=? ORDER BY t.ts ASC LIMIT 60",
        (eid,)).fetchall()
    recs = []
    if logs:
        lo, hi = float(logs[0]["ts"]) - 3, float(logs[-1]["ts"]) + 3
        try:
            recs = con.execute(
                "SELECT memory_id,title,content,created_at FROM booku_memory_records "
                "WHERE person_id=? AND memory_type='event' AND COALESCE(is_deleted,0)=0 "
                "AND created_at BETWEEN ? AND ? ORDER BY created_at",
                (row["person"], lo, hi)).fetchall()
        except sqlite3.Error:
            recs = []
    used = set()
    items = []
    for lg in logs:
        best, best_d = None, 3.0
        for rec in recs:
            if rec["memory_id"] in used:
                continue
            d = abs(float(rec["created_at"] or 0) - float(lg["ts"] or 0))
            if d < best_d:
                best, best_d = rec, d
        body = ""
        if best is not None:
            used.add(best["memory_id"])
            rid = best["memory_id"]
            line = _first(best["content"], 60) or _first(lg["user_text"], 60)
            if full:
                body = (best["content"] or "").strip()
        else:
            rid = "log:%s" % lg["log_id"]
            line = _first(lg["user_text"], 60)
        if full and not body:
            body = "\n".join(x for x in [(lg["user_text"] or "").strip(),
                                          (lg["reply_text"] or "").strip()] if x)
        item = {"record_id": rid, "one_line": line, "ts": lg["ts"] or 0.0}
        if full:
            item["text"] = body
        items.append(item)
    firsts = _first_logs(con, [eid])
    f = firsts.get(eid)
    title = _record_title(con, row["person"], f["ts"], f["user_text"]) if f else ""
    return {"id": eid, "kind": "event",
            "title": _title_of(title, row["summary"], eid[:12]),
            "start_ts": row["start_ts"] or 0.0, "end_ts": row["end_ts"] or 0.0,
            "items": items}


def _block_detail(bid, full=False):
    blk = None
    for b in BD.load(limit=10000):
        if b.get("id") == bid:
            blk = b
            break
    if not blk:
        return None
    summary = blk.get("summary") or ""
    segs = [p.strip() for p in re.split(r"(?<=[。！？!?])\s*|\n+", summary) if p.strip()]
    ts = blk.get("start_ts") or blk.get("end_ts") or 0.0
    items = []
    for i, seg in enumerate(segs[:60]):
        item = {"record_id": "%s#%d" % (bid, i + 1), "one_line": _first(seg, 60), "ts": ts}
        if full:
            item["text"] = seg
        items.append(item)
    return {"id": bid, "kind": "block",
            "title": _flat(blk.get("title"))[:60],
            "start_ts": blk.get("start_ts") or 0.0,
            "end_ts": blk.get("end_ts") or 0.0,
            "state": blk.get("state") or "",
            "items": items}


def detail(db, cid, full=False):
    """点开一条：事件走 event_links→records；块直接读 blocks.db。找不到返回 None。"""
    cid = str(cid or "").strip()
    if not cid:
        return None
    con = _ro(db)
    try:
        out = _event_detail(con, cid, full)
    finally:
        con.close()
    return out if out else _block_detail(cid, full)


def counts(db):
    con = _ro(db)
    try:
        ev = con.execute("SELECT COUNT(*) FROM event_records "
                         "WHERE COALESCE(is_archived,0)=0").fetchone()[0]
    except sqlite3.Error:
        ev = -1
    finally:
        con.close()
    return {"events": int(ev), "blocks": len(BD.load(limit=10000))}


def brief(items, n=3, max_chars=400):
    """拼成注入用的目录段。块先出（骨架），事件补位（分片）；超预算就砍条数。"""
    head = "[目录]"
    lines, used = [], len(head)
    ordered = ([x for x in items if x["kind"] == "block"]
               + [x for x in items if x["kind"] != "block"])
    for it in ordered:
        if len(lines) >= max(1, int(n)):
            break
        day = time.strftime("%m-%d", time.localtime(it["start_ts"] or 0))
        title = it["title"]
        # 块标题常自带「11-22 后半：…」，别再补一遍日期。
        line = ("· %s" % title) if re.match(r"^\d{2}-\d{2}", title) else ("· %s %s" % (day, title))
        one = it.get("one_line") or ""
        # 事件的标题就是首条 user_text，一句话往往只是它自己的开头，别重复贴。
        if one and one[:12] not in it["title"]:
            line += " — " + one
        if used + len(line) + 1 > max_chars:
            if lines:
                break
            line = line[:max_chars - used - 1]
        lines.append(line)
        used += len(line) + 1
    return "\n".join([head] + lines) if lines else ""
