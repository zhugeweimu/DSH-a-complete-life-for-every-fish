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

import io
import os
import re
import sqlite3
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
import autocapture as AC  # noqa: E402
import identity as IDENT  # noqa: E402
import store as S  # noqa: E402


def _person(p, db=None):
    """写入前统一成 canonical person_id（qq:<uin> → user:qq:<uin>）。

    2026-11-19：turn_logs / event_records 之前直接落原始 person，
    导致同一主人在库里有两套前缀；这里收口到 identity.canonical。
    """
    try:
        return IDENT.canonical(p, _db(db)) or S.canon_person(p)
    except Exception:
        return S.canon_person(p)

GAP = float(os.environ.get("SESSION_GAP_SEC", "1800"))
# A：30 分钟硬切窗口 —— 同一 person 相邻回合 gap>=1800s 或窗口累计>=1800s 就关事件开新事件。
# 不再有 7200s 上限、不再按轮数/字数切、不做跨窗口合并。
WINDOW_SEC = float(os.environ.get("SESSION_WINDOW_SEC", "1800"))
PLAIN_IMPORTANCE = float(os.environ.get("SESSION_PLAIN_IMPORTANCE", "0.35"))

# 事件正文软截断：首尾保留 + 中间摘要 + 全文另存（禁止裸截断）
EVENT_TEXT_LIMIT = int(os.environ.get("EVENT_TEXT_LIMIT", "2000"))
EVENT_TEXT_HEAD = int(os.environ.get("EVENT_TEXT_HEAD", "800"))
EVENT_TEXT_TAIL = int(os.environ.get("EVENT_TEXT_TAIL", "400"))

DDL = """
CREATE TABLE IF NOT EXISTS turn_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person     TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    user_text  TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    closed     INTEGER NOT NULL DEFAULT 0,
    event_id   TEXT NOT NULL DEFAULT '',
    namespace  TEXT NOT NULL DEFAULT 'chat'
);
CREATE INDEX IF NOT EXISTS idx_log_person_ts ON turn_logs (person, ts);
CREATE INDEX IF NOT EXISTS idx_log_session  ON turn_logs (session_id);
CREATE TABLE IF NOT EXISTS event_records (
    id TEXT PRIMARY KEY, person TEXT, start_ts REAL, end_ts REAL,
    summary TEXT, event_type TEXT, importance REAL, weight REAL,
    source_log_ids TEXT, legacy INTEGER DEFAULT 0, is_archived INTEGER DEFAULT 0,
    full_text_path TEXT DEFAULT '', namespace TEXT NOT NULL DEFAULT 'chat'
);
-- C：turn_logs（多对多）↔ event_records 的关联表，可 JOIN
CREATE TABLE IF NOT EXISTS event_links (
    log_id   INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    kind     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (log_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_link_event ON event_links (event_id);
-- C：畸形/多值 id → 单值 id 的迁移映射（保留旧 id 出处）
CREATE TABLE IF NOT EXISTS event_id_map (
    old_id      TEXT PRIMARY KEY,
    new_id      TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    migrated_at REAL NOT NULL DEFAULT 0
);
"""


def _ensure_fulltext_col(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(event_records)")}
    if cols and "full_text_path" not in cols:
        con.execute("ALTER TABLE event_records ADD COLUMN full_text_path TEXT DEFAULT ''")
    if cols and "namespace" not in cols:
        con.execute("ALTER TABLE event_records ADD COLUMN namespace TEXT NOT NULL DEFAULT 'chat'")
    lcols = {r[1] for r in con.execute("PRAGMA table_info(turn_logs)")}
    if lcols and "namespace" not in lcols:
        con.execute("ALTER TABLE turn_logs ADD COLUMN namespace TEXT NOT NULL DEFAULT 'chat'")


# namespace 白名单：少了 dsh 会被静默降级成 chat，
# 写进去带错命名空间、读侧按 dsh 过滤全捞不到，而且全程不报错。
_NS_ALLOWED = ("chat", "game", "work", "dsh")


def _ns_of(session_namespace):
    ns = str(session_namespace or "chat").strip().lower()
    return ns if ns in _NS_ALLOWED else "chat"


_TOOL_MARK = re.compile(r"\[(?:桌|查|记|读|归|搜笔|读笔|日|地图|网|网链|网搜|搜网)[:：][^\]]*\]")
_SYS_LINE = re.compile(r"^\s*(?:系统|system|指令|instruction|提示词|prompt)\s*[:：]", re.I)


def _game_clean(text):
    """游戏事件只留局内事件/对话：剔除工具标记、指令与系统提示行。"""
    out = []
    for line in str(text or "").splitlines():
        if _TOOL_MARK.search(line):
            continue
        if _SYS_LINE.match(line) or line.strip().startswith(("[系统", "系统提示")):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _db(db=None):
    return db or os.environ.get("MEM_DB") or S.DEFAULT_DB


def ensure(db=None):
    con = sqlite3.connect(_db(db), timeout=10)
    con.executescript(DDL)
    _ensure_fulltext_col(con)
    con.commit()
    con.close()


def _last(con, person):
    return con.execute(
        "SELECT ts, session_id FROM turn_logs WHERE person=? ORDER BY ts DESC, id DESC LIMIT 1",
        (person,)).fetchone()


def _fulltext_dir(db=None):
    base = os.path.dirname(os.path.abspath(_db(db)))
    d = os.path.join(base, "fulltext")
    os.makedirs(d, exist_ok=True)
    return d


def soft_truncate(text, eid, db=None):
    """软截断：首尾保留 + 中间摘要 + 全文另存。返回 (入库文本, 全文相对路径)。

    不再裸 `[:2000]`：超限时正文仍是首+尾，中间只留摘要标记，全文落
    `<db目录>/fulltext/<event_id>.txt`，路径写进 event_records.full_text_path。
    """
    text = text or ""
    if len(text) <= EVENT_TEXT_LIMIT:
        return text, ""
    path = os.path.join(_fulltext_dir(db), eid + ".txt")
    try:
        if not os.path.exists(path):
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
    except OSError:
        return text[:EVENT_TEXT_LIMIT], ""
    head = text[:EVENT_TEXT_HEAD]
    tail = text[-EVENT_TEXT_TAIL:] if EVENT_TEXT_TAIL else ""
    omitted = len(text) - len(head) - len(tail)
    base = os.path.dirname(os.path.abspath(_db(db)))
    rel = os.path.relpath(path, base).replace("\\", "/")
    return ("%s\n…[中略 %d 字，全文 %s]…\n%s" % (head, omitted, rel, tail)), rel


def _session_stats(con, person, session_id):
    row = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(user_text)+LENGTH(reply_text)),0) "
        "FROM turn_logs WHERE person=? AND session_id=? AND closed=0",
        (person, session_id)).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def close_session(person, session_id, db=None):
    """把一段会话变成事件（每种类型一条）；返回单值 event_id + event_ids。

    关联落 event_links（log_id ↔ event_id），turn_logs.event_id 只存单值。
    """
    ensure(db)
    con = sqlite3.connect(_db(db), timeout=10)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, user_text, reply_text, ts, COALESCE(namespace,'chat') AS ns FROM turn_logs "
        "WHERE person=? AND session_id=? AND closed=0 ORDER BY ts, id", (person, session_id)).fetchall()
    if not rows:
        con.close()
        return None
    ns = _ns_of(rows[0]["ns"])
    pairs = [(r["id"], r["user_text"] or "", r["reply_text"] or "") for r in rows
             if (r["user_text"] or "").strip()]
    if ns == "game":
        # 游戏记忆：只记局内事件与对话，剔除指令/工具标记/系统提示行
        pairs = [(i, _game_clean(u), _game_clean(rp)) for i, u, rp in pairs
                 if _game_clean(u).strip() or _game_clean(rp).strip()]
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
        content = " / ".join(parts)                      # 全文，不裸截断
        eid = "ev-" + uuid.uuid4().hex
        summary, full_path = soft_truncate(content, eid, db=db)   # B：软截断+全文另存
        con.execute(
            "INSERT OR REPLACE INTO event_records"
            "(id,person,start_ts,end_ts,summary,event_type,importance,weight,source_log_ids,"
            " legacy,is_archived,full_text_path,namespace) "
            "VALUES(?,?,?,?,?,?,?,?,?,0,0,?,?)",
            (eid, person, rows[0]["ts"], rows[-1]["ts"], summary, k, imp, imp,
             ",".join(source_ids), full_path, ns)
        )
        for sid in source_ids:                            # C：可 JOIN 的关联表
            con.execute("INSERT OR REPLACE INTO event_links (log_id,event_id,kind) VALUES (?,?,?)",
                        (int(sid), eid, k))
        event_ids.append(eid)
        imps.append(imp)
    con.execute("UPDATE turn_logs SET closed=1, event_id=? WHERE person=? AND session_id=?",
                (event_ids[0], person, session_id))      # C：turn_logs 只存单值
    con.commit()
    con.close()
    return {"event_id": event_ids[0], "event_ids": event_ids, "importance": max(imps),
            "kinds": sorted(groups), "turns": len(rows), "merged": False}


def append(person, user_text, reply_text="", ts=None, db=None, namespace="chat", boundary=""):
    """追加一条日志并按 namespace 分策略切事件：

    · chat / game：同一 person 相邻回合 gap>=1800s 或窗口累计>=1800s → 关事件开新事件（不做合并）
    · work：一个任务=一个事件，不按时间切；只在 boundary=goal/goal_changed（新任务开始，先关旧的）
      或 completed/canceled（任务结束，写完这条立即关事件）时切边界
    """
    ensure(db)
    person = _person(person, db)
    ts = float(ts or time.time())
    ns = _ns_of(namespace)
    boundary = str(boundary or "").strip().lower()
    con = sqlite3.connect(_db(db), timeout=10)
    con.row_factory = sqlite3.Row
    last = _last(con, person)
    closed = None
    if last and boundary in ("goal", "goal_changed", "game_start"):
        closed = close_session(person, last["session_id"], db=db)
        session_id = "s%d" % int(ts)
    elif last:
        same = last["session_id"]
        row = con.execute("SELECT MIN(ts), COALESCE(MAX(namespace),'chat') FROM turn_logs "
                          "WHERE person=? AND session_id=?", (person, same)).fetchone()
        start = float((row or [0])[0] or 0) or 0.0
        cur_ns = _ns_of((row or [0, "chat"])[1] if row else ns)
        split = False
        if cur_ns != ns:
            split = True                             # 来源切换（chat/game/work 之间）必须切
        elif ns == "chat":
            # 只有聊天按 30 分钟空闲/窗口切；game 一局一事件、work 一任务一事件都不按时间切
            if ts - float(last["ts"]) >= GAP or (start and ts - start >= WINDOW_SEC):
                split = True
        if split:
            closed = close_session(person, same, db=db)
            session_id = "s%d" % int(ts)
        else:
            session_id = same
    else:
        session_id = "s%d" % int(ts)
    con.execute("INSERT INTO turn_logs (person,ts,user_text,reply_text,session_id,namespace) "
                "VALUES (?,?,?,?,?,?)",
                (person, ts, user_text or "", reply_text or "", session_id, ns))
    con.commit()
    con.close()
    if boundary in ("completed", "canceled", "game_end"):
        closed = close_session(person, session_id, db=db) or closed
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
