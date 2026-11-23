# -*- coding: utf-8 -*-
"""实体身份层：别名/名字/旧前缀 → canonical person_id。

原则：ID 是主键，名字只是别名；查不到就返回原值并标记未确认，不猜。
"""
import json
import os
import sqlite3
import threading

_LOCK = threading.Lock()
_CACHE = {}
_LOADED = set()

# 身份表属于本地私有数据，不进仓库：默认读 data/identity.json，
# 可以用 MEM_IDENTITY_JSON 指到别处。文件不存在就当空表。
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDENTITY_JSON = os.environ.get("MEM_IDENTITY_JSON") or os.path.join(
    _HERE, "data", "identity.json")


def _load_defaults():
    try:
        with open(IDENTITY_JSON, encoding="utf-8") as fp:
            doc = json.load(fp)
    except (OSError, ValueError):
        return {}
    amap = doc.get("alias_map") if isinstance(doc, dict) else None
    if not isinstance(amap, dict):
        return {}
    return {str(k).strip(): str(v).strip()
            for k, v in amap.items() if k and v}


DEFAULTS = _load_defaults()


def _ensure(db):
    if db in _LOADED:
        return
    with _LOCK:
        if db in _LOADED:
            return
        con = sqlite3.connect(db, timeout=10)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS person_aliases("
                        "alias TEXT PRIMARY KEY, canonical TEXT NOT NULL, "
                        "kind TEXT DEFAULT 'auto', source TEXT DEFAULT '', "
                        "confidence REAL DEFAULT 0.5)")
            cols = {r[1] for r in con.execute("PRAGMA table_info(person_aliases)")}
            for col, decl in (("kind", "TEXT DEFAULT 'auto'"), ("source", "TEXT DEFAULT ''"),
                              ("confidence", "REAL DEFAULT 0.5")):
                if col not in cols:
                    con.execute("ALTER TABLE person_aliases ADD COLUMN %s %s" % (col, decl))
            for a, c in DEFAULTS.items():
                con.execute("INSERT OR IGNORE INTO person_aliases(alias,canonical,kind,source,confidence) "
                            "VALUES(?,?,?,?,?)", (a, c, "seed", "identity.py", 0.9))
            con.commit()
        finally:
            con.close()
        _LOADED.add(db)


def _table(db):
    _ensure(db)
    con = sqlite3.connect(db, timeout=10)
    try:
        rows = con.execute("SELECT alias,canonical FROM person_aliases").fetchall()
    finally:
        con.close()
    return dict(rows)


def canonical(raw, db):
    p = str(raw or "").strip()
    if not p:
        return ""
    if db and os.path.exists(db):
        try:
            tb = _table(db)
            if p in tb:
                return tb[p]
            if p.startswith("qq:"):
                rest = p[3:]
                canon = ("user:qq:" + rest) if rest.isdigit() else ("user:" + rest)
                return tb.get(canon, canon)
            if p.startswith("mumu:FriendMessage:"):
                rest = p[len("mumu:FriendMessage:"):]
                if rest.isdigit():
                    canon = "user:qq:" + rest
                    return tb.get(canon, canon)
        except Exception:
            pass
    if p.startswith("qq:"):
        rest = p[3:]
        return ("user:qq:" + rest) if rest.isdigit() else ("user:" + rest)
    if p.startswith("mumu:FriendMessage:"):
        rest = p[len("mumu:FriendMessage:"):]
        if rest.isdigit():
            return "user:qq:" + rest
    return p


def add_alias(db, alias, canonical_id, kind="manual", source="", confidence=0.8):
    _ensure(db)
    con = sqlite3.connect(db, timeout=10)
    try:
        con.execute("INSERT INTO person_aliases(alias,canonical,kind,source,confidence) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(alias) DO UPDATE SET "
                    "canonical=excluded.canonical,kind=excluded.kind,source=excluded.source,"
                    "confidence=excluded.confidence",
                    (str(alias).strip(), str(canonical_id).strip(), kind, source, float(confidence)))
        con.commit()
    finally:
        con.close()
    return True
