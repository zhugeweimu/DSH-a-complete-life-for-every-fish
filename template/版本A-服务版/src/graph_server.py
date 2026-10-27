# -*- coding: utf-8 -*-
"""graph_server v2 —— 实体以稳定 id 为锚，名字只是标签

实体：
  entities(id, name, aliases, features, first_seen, last_seen)
    id        user:10001（唯一锚点，永不变）
    name      当前称呼（她可以改）
    aliases   历史名字（改过的都留着）
    features  特征：昵称、头像、说话习惯、群名片…（她可以写）

关系：
  relations(subject_id, object_id, subject_name, object_name, predicate, note, valid_from, valid_to)
  —— 名字是快照，用来显示；判定和查询一律走 id

接口：
  POST /entity     登记/更新实体特征（id 为锚）
  POST /relation   写关系（subject/object 可以给 id 或名字；名字会解析成 id）
  POST /why        当前边 + 事件向量 top-k
  GET  /relations?entity=user:10001 或 ?name=某人
  GET  /entities?q=三季
  GET  /health
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_DIR = os.environ.get("GRAPH_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "graph"))
DB_PATH = os.path.join(DATA_DIR, "graph.db")
VEC_PATH = os.path.join(DATA_DIR, "graph_vec.json")
EMB_URL = os.environ.get("EMB_URL", "http://127.0.0.1:8081/v1/embeddings")
EMB_MODEL = os.environ.get("EMB_MODEL", "bge-small-zh-v1.5-f16")
_lock = threading.Lock()


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS entities(
        id TEXT PRIMARY KEY, name TEXT, aliases TEXT, features TEXT,
        first_seen REAL, last_seen REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS relations(
        id TEXT PRIMARY KEY,
        subject_id TEXT, object_id TEXT,
        subject_name TEXT, object_name TEXT,
        predicate TEXT, note TEXT,
        valid_from REAL, valid_to REAL, created_at REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS events(
        id TEXT PRIMARY KEY, relation_id TEXT, text TEXT, ts REAL, ref TEXT)""")
    try:
        con.execute("ALTER TABLE events ADD COLUMN ref TEXT")
    except Exception:
        pass
    con.commit()
    return con


def _rid(sid, pred, oid):
    return hashlib.sha1(("%s|%s|%s" % (sid, pred, oid)).encode("utf-8")).hexdigest()[:16]


def resolve(name_or_id):
    """名字或 id → (实体id, 当前显示名)。找不到返回 (None, name)。"""
    s = (name_or_id or "").strip()
    if not s:
        return None, ""
    con = _conn()
    # 先按 id 查（user:xxx、self:agent 这类带冒号的都是 id）
    if ":" in s:
        row = con.execute("SELECT id,name FROM entities WHERE id=?", (s,)).fetchone()
        con.close()
        if row:
            return row[0], row[1]
        return None, s
    con.close()
    con = _conn()
    row = con.execute("SELECT id,name FROM entities WHERE name=?", (s,)).fetchone()
    if not row:
        row = con.execute("SELECT id,name FROM entities WHERE aliases LIKE ?",
                          ("%" + s + "%",)).fetchone()
    con.close()
    return (row[0], row[1]) if row else (None, s)


def upsert_entity(uid, name="", features=None):
    if not uid:
        return None
    now = time.time()
    with _lock:
        con = _conn()
        row = con.execute("SELECT name,aliases,features FROM entities WHERE id=?", (uid,)).fetchone()
        if row:
            old_name, aliases, feats = row[0] or "", row[1] or "", row[2] or "{}"
            al = json.loads(aliases) if aliases else []
            f = json.loads(feats) if feats else {}
            if name and name != old_name:
                if old_name and old_name not in al:
                    al.append(old_name)
            else:
                name = old_name or name
            if features:
                f.update({k: v for k, v in features.items() if v})
            con.execute("UPDATE entities SET name=?,aliases=?,features=?,last_seen=? WHERE id=?",
                        (name, json.dumps(al, ensure_ascii=False), json.dumps(f, ensure_ascii=False), now, uid))
        else:
            con.execute("INSERT INTO entities(id,name,aliases,features,first_seen,last_seen)"
                        " VALUES(?,?,?,?,?,?)",
                        (uid, name or uid, "[]", json.dumps(features or {}, ensure_ascii=False), now, now))
        con.commit()
        con.close()
    return uid


def add_relation(subject, predicate, obj, note="", evidence=None, ts=None):
    ts = float(ts or time.time())
    sid, sname = resolve(subject)
    oid, oname = resolve(obj)
    if not sid:
        return {"ok": False, "error": "subject 不认识：%s（先登记它的实体 id）" % subject}
    if not oid:
        oid, oname = ("text:" + obj.strip()[:40]), obj
    rid = _rid(sid, predicate, oid)
    if isinstance(evidence, str):
        _parts = re.split(r"[|；\n]+", evidence)
    else:
        _parts = list(evidence or [])
    evs = []
    for e in _parts:
        if isinstance(e, dict):
            txt = (e.get("text") or "").strip()
            if txt:
                evs.append((txt, (e.get("ref") or "").strip()))
        elif e and str(e).strip():
            raw = str(e).strip()
            ref = ""
            if "|ref=" in raw:
                raw, ref = raw.split("|ref=", 1)
                raw, ref = raw.strip(), ref.strip()
            evs.append((raw, ref))
    with _lock:
        con = _conn()
        con.execute("UPDATE relations SET valid_to=? WHERE id=? AND valid_to IS NULL", (ts, rid))
        con.execute("INSERT OR REPLACE INTO relations(id,subject_id,object_id,subject_name,object_name,"
                    "predicate,note,valid_from,valid_to,created_at) VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                    (rid, sid, oid, sname, oname, predicate, note, ts, ts))
        ev_ids = []
        for txt, ref in evs:
            eid = hashlib.sha1((rid + "|" + txt).encode("utf-8")).hexdigest()[:16]
            con.execute("INSERT OR REPLACE INTO events(id,relation_id,text,ts,ref) VALUES(?,?,?,?,?)",
                        (eid, rid, txt, ts, ref))
            ev_ids.append(eid)
        con.commit()
        con.close()
        if evs:
            vec = _load_vec()
            for eid, v in zip(ev_ids, embed(evs)):
                vec[eid] = v
            _save_vec(vec)
    return {"ok": True, "relation_id": rid, "subject_id": sid, "object_id": oid,
            "events": len(ev_ids)}


def current_relations(entity=None):
    con = _conn()
    q = "SELECT * FROM relations WHERE valid_to IS NULL"
    args = ()
    if entity:
        eid, _n = resolve(entity)
        if eid:
            q += " AND (subject_id=? OR object_id=?)"
            args = (eid, eid)
        else:
            q += " AND (subject_name=? OR object_name=?)"
            args = (entity, entity)
    rows = con.execute(q + " ORDER BY valid_from DESC", args).fetchall()
    cols = [c[0] for c in con.execute("SELECT * FROM relations LIMIT 0").description]
    out = [dict(zip(cols, r)) for r in rows]
    # 显示名兜底：实体登记过就用当前名字，别把内部 id 直接给人看
    for e in out:
        for side, idk, namek in (("subject", "subject_id", "subject_name"),
                                 ("object", "object_id", "object_name")):
            if e.get(namek) in (None, "", e.get(idk)):
                row = con.execute("SELECT name FROM entities WHERE id=?", (e.get(idk),)).fetchone()
                if row and row[0]:
                    e[namek] = row[0]
    con.close()
    return out


def why(subject=None, q=None, k=5):
    edges = current_relations(subject)
    if not edges:
        return {"edges": [], "evidence": [], "note": "没有这条关系"}
    con = _conn()
    evs = []
    for e in edges:
        for r in con.execute("SELECT id,text,ts,ref FROM events WHERE relation_id=?", (e["id"],)):
            evs.append({"id": r[0], "text": r[1], "ts": r[2], "ref": r[3] or ""})
    con.close()
    if not evs:
        return {"edges": edges, "evidence": [], "note": "有关系但没记事件"}
    vec = _load_vec()
    e0 = edges[0]
    query = q or ("%s %s %s %s" % (e0["subject_name"], e0["predicate"], e0["object_name"], e0.get("note") or ""))
    qv = embed([query])[0]
    scored = sorted(((_cos(qv, vec.get(ev["id"]) or []), ev) for ev in evs), key=lambda x: -x[0])
    return {"edges": edges, "query": query,
            "evidence": [{"score": round(s, 4), "text": ev["text"], "ts": ev["ts"],
                          "id": ev["id"], "ref": ev.get("ref") or ""} for s, ev in scored[:k]],
            "events_total": len(evs)}


def embed(texts):
    out = []
    for t in texts:
        body = json.dumps({"model": EMB_MODEL, "input": t}, ensure_ascii=False).encode()
        req = urllib.request.Request(EMB_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        out.append((d.get("data") or [{}])[0].get("embedding") or [])
    return out


def _load_vec():
    try:
        with io.open(VEC_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_vec(v):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = VEC_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(v, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, VEC_PATH)


def _cos(a, b):
    if not a or not b:
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        from urllib.parse import unquote
        try:
            if self.path.startswith("/health"):
                con = _conn()
                n = lambda t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                out = {"ok": True, "entities": n("entities"), "relations": n("relations"),
                       "events": n("events"), "db": DB_PATH}
                con.close()
                return self._send(200, out)
            m = re.match(r"/relations\?(?:entity|name)=([^&]+)", self.path)
            if m:
                return self._send(200, {"edges": current_relations(unquote(m.group(1)))})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if self.path.startswith("/entity"):
                uid = str(d.get("id") or d.get("uid") or "").strip()
                # 纯数字视为平台 id；self:agent 这类自带前缀的原样保留
                if uid.isdigit():
                    uid = "user:" + uid
                if not uid:
                    return self._send(400, {"error": "id 必填"})
                upsert_entity(uid, d.get("name") or "", d.get("features") or {})
                return self._send(200, {"ok": True, "id": uid})
            if self.path.startswith("/relation"):
                r = add_relation(d.get("subject") or "", d.get("predicate") or "",
                                 d.get("object") or "", d.get("note") or "",
                                 d.get("evidence") or [], d.get("ts"))
                return self._send(200 if r.get("ok") else 400, r)
            if self.path.startswith("/why"):
                return self._send(200, why(d.get("subject"), d.get("q"), int(d.get("k") or 5)))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def log_message(self, *a):
        pass


def main():
    port = int(os.environ.get("GRAPH_PORT") or 8094)
    _conn().close()
    print("图谱服务 v2 http://127.0.0.1:%d  db=%s" % (port, DB_PATH))
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()
