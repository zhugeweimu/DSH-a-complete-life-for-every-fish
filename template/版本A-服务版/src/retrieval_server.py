# -*- coding: utf-8 -*-
"""retrieval/server.py —— 三件套检索服务（给插件/CE 调）

GET/POST /search
  {"q": "查询", "top": 5, "mode": "hybrid|lex|dense", "rerank": "off|light|llm"}
→ {"q":..., "results":[{id,title,text,score,why}], "stages":{lex:n,dense:n,rerank:"llm"}}

为什么单独起服务而不是塞进插件：
  · 向量缓存、BM25 索引只该建一次，不该每个插件各建一份
  · 插件只依赖一个 URL（和 CE 的插件契约一致）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hybrid as H
import reranker_onnx as R

DEFAULT_DB = os.environ.get("MEM_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "metadata.db")
_lock = threading.Lock()
_rcache = {}
_RCACHE_MAX = 200
_cache = {}


def get_index(db):
    with _lock:
        ent = _cache.get(db)
        if ent:
            return ent
        docs = H.load_corpus(db)
        bm = H.BM25(docs)
        ent = {"docs": docs, "bm": bm, "ts": 0.0}
        _cache[db] = ent
        return ent


def do_search(q, top=5, mode="hybrid", rerank="off", db=None, person=None, scope="all"):
    db = db or DEFAULT_DB
    ck = (db, q, top, mode, rerank, person, scope)
    if ck in _rcache:
        return _rcache[ck]
    ent = get_index(db)
    docs, bm = ent["docs"], ent["bm"]
    if person and scope == "person":
        # 严格隔离模式（默认不用）：只检索这个人自己的 + 公共记忆
        docs = [d for d in docs if (d.get("person") or "") in ("", person)]
        bm = H.BM25(docs)
    if not docs:
        return {"error": "库里没有记录", "q": q}
    lex = bm.search(q, top=50) if mode in ("hybrid", "lex") else []
    dense = H.dense_search(docs, q, top=50, cache_path=H.CACHE) if mode in ("hybrid", "dense") else []
    fused = H.rrf([x for x in (lex, dense) if x])
    if rerank == "off":
        picked = [(b, i, {}) for i, b in fused[:top]]
        mode_name = "rrf-only"
    elif rerank == "bge":
        try:
            picked = R.rerank(q, docs, fused, top)
            mode_name = "bge-rerank"
        except Exception as e:
            picked = H.light_rerank(q, docs, fused, top)
            mode_name = "light-rerank（bge 失败：%r）" % (e,)
    elif rerank == "llm":
        try:
            picked = H.llm_rerank(q, docs, fused, top)
            mode_name = "llm-rerank"
        except Exception as e:
            picked = H.light_rerank(q, docs, fused, top)
            mode_name = "light-rerank（llm 失败：%r）" % (e,)
    else:
        picked = H.light_rerank(q, docs, fused, top)
        mode_name = "light-rerank"
    res = []
    for sc, idx, parts in picked:
        d = docs[idx]
        res.append({"id": d["id"], "person": d.get("person") or "", "ts": d.get("ts") or 0,
                    "title": (d["title"] or "")[:60],
                    "text": (d["text"] or "")[:160], "score": round(float(sc), 4),
                    "why": parts})
    out = {"q": q, "mode": mode_name, "corpus": len(docs),
            "stages": {"lex": len(lex), "dense": len(dense)}, "results": res}
    if len(_rcache) > _RCACHE_MAX:
        _rcache.clear()
    _rcache[ck] = out
    return out


class H2(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"ok": True, "db": DEFAULT_DB,
                                    "docs": len(get_index(DEFAULT_DB)["docs"]),
                                    "cache": H.CACHE})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            q = (d.get("q") or d.get("query") or "").strip()
            if not q:
                return self._send(400, {"error": "q required"})
            r = do_search(q, int(d.get("top") or 5), d.get("mode") or "hybrid",
                          d.get("rerank") or "off", d.get("db"), d.get("person"),
                          d.get("scope") or "all")
            return self._send(200, r)
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def log_message(self, *a):
        pass


def main():
    global DEFAULT_DB
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("RETR_PORT") or 8093))
    ap.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args()
    DEFAULT_DB = a.db
    print("检索服务 http://127.0.0.1:%d  db=%s  docs=%d"
          % (a.port, DEFAULT_DB, len(get_index(DEFAULT_DB)["docs"])))
    ThreadingHTTPServer(("127.0.0.1", a.port), H2).serve_forever()


if __name__ == "__main__":
    main()
