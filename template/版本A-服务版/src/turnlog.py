# -*- coding: utf-8 -*-
"""turnlog —— 日志层 + 会话分段 + 会话事件

两层：
  日志（turn_logs）：每回合原样追加，不过滤、不判断——事实层
  事件（booku_memory_records）：会话结束后生成——记忆层

会话分段：
  同一个人，相邻两回合间隔 > SESSION_GAP_SEC（默认 1800 秒 = 30 分钟）
  → 上一段关闭，生成一条“会话事件”
  → 会话里命中七类 → 重要度取最高类；没命中 → 普通事件（0.35）

事件是“可触发的记忆”：召回时按 相关度 × 时间衰减权重 排序。
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
import autocapture as AC  # noqa: E402
import store as S  # noqa: E402

GAP = float(os.environ.get("SESSION_GAP_SEC", "1800"))
MAX_SESSION_SEC = float(os.environ.get("SESSION_MAX_SEC", "7200"))
PLAIN_IMPORTANCE = float(os.environ.get("SESSION_PLAIN_IMPORTANCE", "0.35"))

DDL = """
CREATE TABLE IF NOT EXISTS turn_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person     TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    user_text  TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    closed     INTEGER NOT NULL DEFAULT 0,
    event_id   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_log_person_ts ON turn_logs (person, ts);
CREATE INDEX IF NOT EXISTS idx_log_session  ON turn_logs (session_id);
CREATE TABLE IF NOT EXISTS event_records (
    id TEXT PRIMARY KEY, person TEXT, start_ts REAL, end_ts REAL,
    summary TEXT, event_type TEXT, importance REAL, weight REAL,
    source_log_ids TEXT, legacy INTEGER DEFAULT 0, is_archived INTEGER DEFAULT 0
);
"""


def _db(db=None):
    return db or os.environ.get("MEM_DB") or S.DEFAULT_DB


def ensure(db=None):
    con = sqlite3.connect(_db(db), timeout=10)
    con.executescript(DDL)
    con.commit()
    con.close()


def _last(con, person):
    return con.execute(
        "SELECT ts, session_id FROM turn_logs WHERE person=? ORDER BY ts DESC, id DESC LIMIT 1",
        (person,)).fetchone()


def close_session(person, session_id, db=None):
    """把一段会话变成一条事件；返回事件 id 或 None"""
    con = sqlite3.connect(_db(db), timeout=10)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, user_text, reply_text, ts FROM turn_logs WHERE person=? AND session_id=? AND closed=0 "
        "ORDER BY ts, id", (person, session_id)).fetchall()
    if not rows:
        con.close()
        return None
    pairs = [(r["id"], r["user_text"] or "", r["reply_text"] or "") for r in rows
             if (r["user_text"] or "").strip()]
    # 同一段里不同类型各自成事件，避免高权重事件互相吞并
    groups = {}
    for log_id, user_text, reply_text in pairs:
        ks = AC.detect(user_text) or ["plain"]
        if "note" in ks:
            ks = ["note"]
        elif "relationship" in ks and "event" in ks:
            ks = [k for k in ks if k != "event"] or ["event"]
        for k in ks:
            groups.setdefault(k, []).append((log_id, user_text, reply_text))
    if not groups:
        groups = {"plain": [(0, "（空会话）", "")]}
    event_ids, imps = [], []
    for k in sorted(groups):
        imp = AC.TYPE_IMPORTANCE.get(k, PLAIN_IMPORTANCE)
        parts = []
        source_ids = []
        for log_id, user_text, reply_text in groups[k]:
            if log_id:
                source_ids.append(str(log_id))
            if reply_text.strip():
                parts.append("主人说：%s\n我说：%s" % (user_text, reply_text[:1000]))
            else:
                parts.append("主人说：%s" % user_text)
        content = " / ".join(parts)[:2000]
        eid = "ev-" + uuid.uuid4().hex
        con.execute(
            "INSERT OR REPLACE INTO event_records"
            "(id,person,start_ts,end_ts,summary,event_type,importance,weight,source_log_ids,legacy,is_archived) "
            "VALUES(?,?,?,?,?,?,?,?,?,0,0)",
            (eid, person, rows[0]["ts"], rows[-1]["ts"], content, k, imp, imp,
             ",".join(source_ids))
        )
        event_ids.append(eid)
        imps.append(imp)
    con.execute("UPDATE turn_logs SET closed=1, event_id=? WHERE person=? AND session_id=?",
                (",".join(event_ids), person, session_id))
    con.commit()
    con.close()
    return {"event_id": event_ids[0], "event_ids": event_ids, "importance": max(imps),
            "kinds": sorted(groups), "turns": len(rows), "merged": False}


def append(person, user_text, reply_text="", ts=None, db=None):
    """追加一条日志；跨过 30 分钟自动关闭上一段会话并生成事件"""
    ensure(db)
    ts = float(ts or time.time())
    con = sqlite3.connect(_db(db), timeout=10)
    con.row_factory = sqlite3.Row
    last = _last(con, person)
    closed = None
    if last and ts - float(last["ts"]) > GAP:
        closed = close_session(person, last["session_id"], db=db)
        session_id = "s%d" % int(ts)
    elif last:
        same = last["session_id"]
        try:
            start = float(str(same)[1:])
        except Exception:
            start = 0.0
        if start and ts - start > MAX_SESSION_SEC:
            closed = close_session(person, same, db=db)
            session_id = "s%d" % int(ts)
        else:
            session_id = same
    else:
        session_id = "s%d" % int(ts)
    con.execute("INSERT INTO turn_logs (person,ts,user_text,reply_text,session_id) VALUES (?,?,?,?,?)",
                (person, ts, user_text or "", reply_text or "", session_id))
    con.commit()
    con.close()
    return {"session_id": session_id, "closed": closed}


def pending_sessions(db=None):
    con = sqlite3.connect(_db(db), timeout=10)
    rows = con.execute("SELECT session_id, person, MIN(ts), COUNT(*) FROM turn_logs "
                       "WHERE closed=0 GROUP BY session_id, person").fetchall()
    con.close()
    return [{"session_id": r[0], "person": r[1], "start": r[2], "turns": r[3]} for r in rows]


def sweep(now=None, db=None):
    """关闭所有空闲超过 GAP 的会话；返回生成的会话事件列表"""
    now = float(now or time.time())
    out = []
    for s in pending_sessions(db=db):
        con = sqlite3.connect(_db(db), timeout=10)
        row = con.execute("SELECT MAX(ts) FROM turn_logs WHERE session_id=? AND person=?",
                          (s["session_id"], s["person"])).fetchone()
        con.close()
        last = float((row or [0])[0] or 0)
        if now - last > GAP:
            r = close_session(s["person"], s["session_id"], db=db)
            if r:
                out.append({"session_id": s["session_id"], **r})
    return out


def prune_logs(older_days=180, db=None, closed_only=True):
    """清理旧日志行（只清已结算的）；事件不受影响"""
    import time as _t
    cutoff = _t.time() - float(older_days) * 86400
    con = sqlite3.connect(_db(db), timeout=10)
    try:
        q = "DELETE FROM turn_logs WHERE ts < ?" + (" AND closed=1" if closed_only else "")
        cur = con.execute(q, (cutoff,))
        con.commit()
        return {"deleted": cur.rowcount}
    finally:
        con.close()


def replay(turns, db=None):
    """按时间顺序灌入一组对话，返回每步结果"""
    out = []
    for t in turns:
        out.append(append(t.get("person", ""), t.get("user", ""), t.get("reply", ""),
                          t.get("ts"), db=db))
    return out
