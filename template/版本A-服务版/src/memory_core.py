# -*- coding: utf-8 -*-
"""memory_core —— 记忆内核（单进程，:8095）

把原来分散的三件事收进一个服务，共用一份代码和一处配置：
  · 检索（BM25 + 向量 + RRF + bge rerank）   —— 原 8093
  · 关系图谱（实体 / 关系边 / 事件向量）      —— 原 8094
  · 人物档案（印象 / 态度 / 特征）            —— 原来是插件里的逻辑

它只读记忆库（sqlite），写入仍走记忆服务 8001（避免双写打架），
所有调用方只认这一个地址。

接口：
  GET  /health
  POST /recall      {q, top, person, scope, rerank}
  POST /remember    {title, content, person_id, tags}      → 转 8001
  POST /relation    {subject, predicate, object, note, evidence}
  POST /why         {subject, q}
  GET  /relations?entity=
  POST /entity      {id, name, features}
  GET  /profile?person_id=user:10001
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "retrieval"))
sys.path.insert(0, os.path.join(HERE, "..", "graph"))

import hybrid as H                      # noqa: E402
import reranker_onnx as R               # noqa: E402
import graph_server as G                # noqa: E402
import store as ST
import consolidate as CO

MEM_DB = os.environ.get("MEM_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "metadata.db")
MEM_INBOX = os.environ.get("MEM_INBOX", "http://127.0.0.1:8001/booku-memory/api/memories")
_lock = threading.Lock()
_idx = {}


def _db_sig():
    try:
        mt = 0.0
        for p in (MEM_DB, MEM_DB + "-wal"):
            try:
                mt = max(mt, os.path.getmtime(p))
            except OSError:
                pass
        n = 0
        con = sqlite3.connect(MEM_DB, timeout=5)
        try:
            n = con.execute("SELECT COUNT(*) FROM booku_memory_records WHERE is_deleted=0").fetchone()[0]
        finally:
            con.close()
        return (mt, n)
    except Exception:
        return (0.0, -1)


def _index():
    with _lock:
        sig = _db_sig()
        if not _idx or _idx.get("sig") != sig:
            _idx["docs"] = H.load_corpus(MEM_DB)
            _idx["bm"] = H.BM25(_idx["docs"])
            _idx["sig"] = sig
        return _idx


def _get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _importance(title, given=None):
    try:
        if given is not None and float(given) > 0:
            return max(0.05, min(1.0, float(given)))
    except Exception:
        pass
    t = (title or "")
    if t.startswith("日记") or t.startswith("自述"):
        return 0.9
    if t.startswith("群聊"):
        return 0.15
    if t.startswith("她管他叫") or t.startswith("印象") or t.startswith("态度"):
        return 0.8
    return 0.5


def _decay(ts, last_active, importance):
    """曲线：H = 7 + 365*imp^2；floor = 0.3*imp；w = floor + (1-floor)*2^(-d/H)
    重要度 0.95 → 半衰期约 336 天、永不归零；流水 0.15 → 约 15 天。"""
    now = time.time()
    base = max(float(ts or 0), float(last_active or 0), 0)
    if not base:
        return 0.0
    imp = max(0.05, min(1.0, float(importance or 0.5)))
    days = max(0.0, (now - base) / 86400.0)
    H = 7.0 + 365.0 * (imp ** 2)
    floor = 0.3 * imp
    w = floor + (1.0 - floor) * (2.0 ** (-days / H))
    return max(0.0, min(1.0, w))   # 理论上界 1；防浮点越界


def _bump(memory_id):
    try:
        con = sqlite3.connect(MEM_DB, timeout=10)
        con.execute("UPDATE booku_memory_records SET activation_count=COALESCE(activation_count,0)+1, "
                    "last_activated_at=? WHERE memory_id=?", (time.time(), memory_id))
        con.commit()
        con.close()
    except Exception as e:
        print("bump 失败:", repr(e), flush=True)


def _cleanup_flow():
    try:
        con = sqlite3.connect(MEM_DB, timeout=10)
        cut = time.time() - 14 * 86400
        n = con.execute("DELETE FROM booku_memory_records WHERE title LIKE '群聊·%' "
                        "AND COALESCE(activation_count,0)=0 "
                        "AND COALESCE(importance,0.15)<0.3 AND created_at<?", (cut,)).rowcount
        con.commit()
        con.close()
        if n:
            print("清理群聊流水 %d 条" % n)
    except Exception:
        pass


def recall(q, top=6, person="", scope="all", rerank="bge", since=None, until=None):
    ent = _index()
    docs, bm = ent["docs"], ent["bm"]
    if person and scope == "person":
        # 只给：当前这个人的 + 她自己的(self:agent) + 公共的(空)
        allow = {"", person, "self:agent"}
        docs = [d for d in docs if (d.get("person") or "") in allow]
        bm = H.BM25(docs)
    if since or until:
        docs = [d for d in docs if (not since or float(d.get("ts") or 0) >= float(since))
                and (not until or float(d.get("ts") or 0) <= float(until))]
        bm = H.BM25(docs)
    if not (q or "").strip():
        cand = sorted(docs, key=lambda d: -(_importance(d.get("title"), d.get("importance")) * 1000
                                            + (d.get("ts") or 0) / 1e9))
        out = []
        for d in cand[:top]:
            imp = _importance(d.get("title"), d.get("importance"))
            dec = _decay(d.get("ts"), d.get("last_activated"), imp)
            out.append({"id": d["id"], "person": d.get("person") or "", "ts": d.get("ts") or 0,
                        "title": (d.get("title") or "")[:60],
                        "text": (d.get("text") or "")[:200],
                        "score": round(imp * dec, 4), "base": imp, "decay": round(dec, 3),
                        "importance": imp, "why": {"window": True}})
        top_score = out[0]["score"] if out else 0.0
        for x in out:
            x["score_norm"] = round(x["score"] / top_score, 4) if top_score else 0.0
            _bump(x["id"])
        return {"q": q, "mode": "time-window", "corpus": len(docs), "results": out}
    lex = bm.search(q, top=50)
    try:
        dense = H.dense_search(docs, q, top=50, cache_path=os.environ.get("HYBRID_CACHE", ""))
    except Exception as e:
        dense = []
        degraded = "（向量服务不可用，仅 BM25：%r）" % (e,)
    else:
        degraded = ""
    fused = H.rrf([x for x in (lex, dense) if x])
    if degraded and not fused:
        fused = lex
    if rerank == "bge":
        try:
            picked = R.rerank(q, docs, fused, top)
            mode = "bge-rerank"
        except Exception as e:
            picked = H.light_rerank(q, docs, fused, top)
            mode = "light-rerank（bge 失败：%r）" % (e,)
    elif rerank == "light":
        picked = H.light_rerank(q, docs, fused, top)
        mode = "light-rerank"
    else:
        picked = [(b, i, {}) for i, b in fused[:top]]
        mode = "rrf-only"
    out = []
    for sc, idx, why in picked:
        d = docs[idx]
        imp = _importance(d.get("title"), d.get("importance"))
        dec = _decay(d.get("ts"), d.get("last_activated"), imp)
        out.append({"id": d["id"], "person": d.get("person") or "", "ts": d.get("ts") or 0,
                    "title": (d.get("title") or "")[:60],
                    "text": (d.get("text") or "")[:200],
                    "score": round(float(sc) * dec, 4),
                    "base": round(float(sc), 4), "decay": round(dec, 3), "importance": imp,
                    "why": why})
    out.sort(key=lambda x: -x["score"])
    top_score = out[0]["score"] if out else 0.0
    for x in out:
        x["score_norm"] = round(x["score"] / top_score, 4) if top_score else 0.0
        _bump(x["id"])
    return {"q": q, "mode": mode, "corpus": len(docs), "results": out}


def remember(title, content, person_id="", tags=None, importance=None, ts=None):
    # 独立模式：不依赖任何平台，直接写本地库
    if os.environ.get("MEM_WRITE_MODE", "auto") == "local":
        r = ST.add(title, content, person=person_id, tags=tags or [], importance=importance, ts=ts)
        return {"ok": True, "id": r["id"], "merged": r["merged"], "mode": "local"}
    payload = {"title": (title or "无题")[:60], "content": content or "",
               "folder_id": "default", "bucket": "memory", "memory_type": "event",
               "status": "active", "person_id": person_id,
               "core_tags": tags or ["对话"], "diffusion_tags": ["core"],
               "opposing_tags": ["未分类"]}
    if ts:
        payload["ts"] = float(ts)
        payload["created_at"] = float(ts)
    req = urllib.request.Request(MEM_INBOX, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    if importance is not None:
        try:
            mid = (out.get("item") or {}).get("id") or out.get("id") or ""
            if mid:
                con = sqlite3.connect(MEM_DB, timeout=10)
                con.execute("UPDATE booku_memory_records SET importance=? WHERE memory_id=?",
                            (max(0.05, min(1.0, float(importance))), mid))
                con.commit()
                con.close()
        except Exception:
            pass
    return out


def _bind_refs(relation_id):
    """事件默认没 ref：按文字到记忆库里找最像的那条，绑上。找不到就留空。"""
    import sqlite3
    try:
        con = sqlite3.connect(G.DB_PATH, timeout=10)
        rows = con.execute("SELECT id,text FROM events WHERE relation_id=? AND (ref IS NULL OR ref='')",
                           (relation_id,)).fetchall()
        con.close()
        if not rows:
            return 0
        m = sqlite3.connect(MEM_DB, timeout=10)
        bound = 0
        for eid, text in rows:
            key = (text or "").strip()[:12]
            if not key:
                continue
            hit = m.execute("SELECT id FROM booku_memory_records WHERE content_snippet LIKE ? "
                            "ORDER BY created_at DESC LIMIT 1", ("%" + key + "%",)).fetchone()
            if hit:
                con2 = sqlite3.connect(G.DB_PATH, timeout=10)
                con2.execute("UPDATE events SET ref=? WHERE id=?", (hit[0], eid))
                con2.commit()
                con2.close()
                bound += 1
        m.close()
        return bound
    except Exception:
        return 0


def _expand(events, pad_hours=12):
    """把事件往回追一层：ref 指向的原话 + 同一天的记忆。"""
    import sqlite3
    out = []
    con = sqlite3.connect(MEM_DB, timeout=10)
    con.row_factory = sqlite3.Row
    for ev in events[:5]:
        item = {"text": ev.get("text"), "ts": ev.get("ts"), "source": None, "same_day": []}
        ref = (ev.get("ref") or "").strip()
        try:
            if not ref:
                key = (ev.get("text") or "").strip()[:12]
                if key:
                    hit = con.execute("SELECT memory_id FROM booku_memory_records WHERE content LIKE ? "
                                      "ORDER BY created_at DESC LIMIT 1", ("%" + key + "%",)).fetchone()
                    if hit:
                        ref = hit[0]
            if ref:
                row = con.execute("SELECT memory_id,title,content,created_at FROM booku_memory_records WHERE memory_id=?",
                                  (ref,)).fetchone()
                if row:
                    item["source"] = {"id": row["memory_id"], "title": row["title"] or "",
                                      "text": (row["content"] or "")[:300],
                                      "ts": row["created_at"]}
            ts = float(ev.get("ts") or 0)
            if ts:
                for r in con.execute(
                        "SELECT memory_id,title,content,created_at FROM booku_memory_records "
                        "WHERE created_at BETWEEN ? AND ? ORDER BY ABS(created_at-?) LIMIT 3",
                        (ts - pad_hours * 3600, ts + pad_hours * 3600, ts)).fetchall():
                    if item["source"] and r["memory_id"] == item["source"]["id"]:
                        continue
                    item["same_day"].append({"id": r["memory_id"], "text": (r["content"] or "")[:160],
                                             "ts": r["created_at"]})
        except Exception:
            pass
        out.append(item)
    con.close()
    return out


def profile(person_id):
    out = {"impression": "", "attitude": "", "name": "", "features": {},
           "impression_ts": 0, "attitude_ts": 0}
    try:
        d = _get(MEM_INBOX + "?" + urllib.parse.urlencode({"person_id": person_id, "limit": 200}))
        for it in d.get("items") or []:
            md = it.get("metadata") or {}
            tags = " ".join(str(x) for x in (md.get("core_tags") or []))
            body = (it.get("content_snippet") or it.get("title") or "").strip()
            _ts = md.get("updated_at") or md.get("created_at") or 0
            if "印象" in tags and not out["impression"]:
                out["impression"] = body[:300]
                out["impression_ts"] = _ts
            if "态度" in tags and not out["attitude"]:
                out["attitude"] = body[:300]
                out["attitude_ts"] = _ts
    except Exception:
        pass
    con = G._conn()
    row = con.execute("SELECT name,features FROM entities WHERE id=?", (person_id,)).fetchone()
    con.close()
    if row:
        out["name"] = row[0] or ""
        try:
            out["features"] = json.loads(row[1] or "{}")
        except Exception:
            out["features"] = {}
    return out


PANEL_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>记忆内核 · 召回面板</title>
<style>
:root{--bg:#0a0e14;--panel:#111822;--panel2:#16202c;--line:#1f2a38;--text:#d8dde5;--dim:#7a8798;--faint:#4d5968;--gold:#c9a96a;--red:#b5483a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:30px 22px 70px}
header{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:1px solid var(--line);padding-bottom:18px}
h1{margin:0;font-size:25px;letter-spacing:2px}
h1 i{display:inline-block;width:8px;height:22px;background:var(--red);margin-right:10px;vertical-align:-3px}
.sub{color:var(--dim);font-size:13px;margin-top:6px}
.st{text-align:right;font-size:12px;color:var(--dim)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--gold);margin-right:6px}
.qp{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:20px;margin-top:26px}
.row{display:flex;gap:10px}
#q{flex:1;background:var(--panel2);border:1px solid var(--line);border-radius:4px;color:var(--text);padding:13px 15px;font-size:16px;outline:none}
#q:focus{border-color:var(--gold)}
#go{background:var(--gold);color:#1a1408;border:0;border-radius:4px;padding:0 26px;font-weight:600;letter-spacing:2px;cursor:pointer}
#go:disabled{opacity:.5}
.params{display:flex;gap:26px;margin-top:14px;padding-top:12px;border-top:1px dashed var(--line);font-size:12px;color:var(--dim);flex-wrap:wrap}
.params input[type=range]{width:120px;accent-color:var(--gold)}
.val{color:var(--gold);font-family:ui-monospace,monospace}
.meta{display:flex;justify-content:space-between;margin:24px 0 12px;font-size:12px;color:var(--dim)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;margin-bottom:12px;padding:16px 20px}
.card:hover{border-color:#2c3a4d}
.hd{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:8px}
.src{font-size:11px;color:var(--faint);letter-spacing:1px}
.title{font-size:16px;font-weight:600;letter-spacing:1px}
.score{font-family:ui-monospace,monospace;color:var(--gold);font-size:17px;text-align:right}
.quote{background:var(--panel2);border-left:3px solid var(--gold);padding:12px 16px;margin:8px 0 10px;border-radius:0 4px 4px 0;font-family:"Songti SC",serif;font-size:15px;line-height:1.9;color:#e8e2d4}
.tags{display:flex;gap:6px;flex-wrap:wrap}
.tag{font-size:11px;padding:2px 9px;border-radius:3px;background:rgba(201,169,106,.15);color:var(--gold)}
.tag.k{background:rgba(181,72,58,.15);color:#d9766a}
.empty{padding:50px 20px;text-align:center;color:var(--faint);border:1px dashed var(--line);border-radius:6px}
</style></head><body><div class="wrap">
<header><div><h1><i></i>记忆内核 · 召回面板</h1>
<div class="sub">真实数据：BM25 + bge 512 维 + RRF + bge-reranker</div></div>
<div class="st"><div><span class="dot"></span><span id="st">连接中…</span></div><div id="cnt">—</div></div></header>
<div class="qp"><div class="row">
<input id="q" placeholder="输入一句语义，例如：他为什么对记忆库这么在意" autofocus>
<button id="go">召 回</button></div>
<div class="params">
<div><label>topK </label><input type="range" id="k" min="1" max="12" value="6"><span class="val" id="kv">6</span></div>
<div><label>阈值 </label><input type="range" id="th" min="0" max="100" value="10"><span class="val" id="thv">0.10</span></div>
<div><label>精排 </label><select id="rk" style="background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:3px;padding:2px 6px">
<option value="bge">bge rerank</option><option value="light">轻量重排</option><option value="off">只看融合</option></select></div>
</div></div>
<div class="meta"><span>召回结果</span><span id="info">等待查询</span></div>
<div id="out"><div class="empty">输入一段语义，从记忆库里召回原文</div></div>
</div><script>
const $=s=>document.querySelector(s);
const fmt=ts=>{if(!ts)return '?';const d=new Date(ts*1000);return (d.getMonth()+1).toString().padStart(2,'0')+'-'+d.getDate().toString().padStart(2,'0')+' '+d.getHours().toString().padStart(2,'0')+':'+d.getMinutes().toString().padStart(2,'0')};
async function health(){try{const r=await fetch('/health');const d=await r.json();
  $('#st').textContent='内核在线 · '+d.docs+' 条记忆 / '+d.relations+' 关系 / '+d.events+' 事件';
  $('#cnt').textContent='图库 '+d.entities+' 实体';}catch(e){$('#st').textContent='内核离线';}}
async function run(){const q=$('#q').value.trim();if(!q)return;
  $('#go').disabled=true;$('#info').textContent='检索中…';
  try{const r=await fetch('/recall',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({q,top:+$('#k').value,rerank:$('#rk').value})});
  const d=await r.json();const th=+$('#th').value/100;
  const rs=(d.results||[]).filter(x=>x.score>=th);
  $('#info').textContent=d.mode+' · 候选 '+d.corpus+' 条 · 命中 '+rs.length;
  $('#out').innerHTML=rs.length?rs.map(x=>`<div class="card"><div class="hd"><div>
    <div class="src">${x.person||'公共'} · ${fmt(x.ts)}</div><div class="title">${esc(x.title)}</div></div>
    <div class="score">${x.score.toFixed(3)}</div></div>
    <div class="quote">${esc(x.text)}</div>
    <div class="tags"><span class="tag k">${x.why&&x.why.base?'字面命中':'仅语义'}</span>
    <span class="tag">${esc(x.why&&x.why.rerank?'精排':'融合位')}</span></div></div>`).join('')
    :'<div class="empty">没有超过阈值的召回（调低阈值或换说法）</div>';
  }catch(e){$('#info').textContent='出错：'+e}
  $('#go').disabled=false;}
function esc(s){return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
$('#go').onclick=run;$('#q').onkeydown=e=>{if(e.key==='Enter')run()};
$('#k').oninput=e=>$('#kv').textContent=e.target.value;
$('#th').oninput=e=>$('#thv').textContent=(e.target.value/100).toFixed(2);
health();setInterval(health,15000);
</script></body></html>"""

class H2(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        try:
            if self.path.startswith("/panel"):
                b = PANEL_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            if self.path.startswith("/health"):
                con = G._conn()
                n = lambda t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                out = {"ok": True, "docs": len(_index()["docs"]), "entities": n("entities"),
                       "relations": n("relations"), "events": n("events"),
                       "mem_db": MEM_DB, "graph_db": G.DB_PATH}
                con.close()
                return self._send(200, out)
            if self.path.startswith("/list"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                params = {k: v[0] for k, v in qs.items()}
                url = MEM_INBOX + "?" + urllib.parse.urlencode(params)
                with urllib.request.urlopen(url, timeout=30) as r:
                    return self._send(200, json.load(r))
            if self.path.startswith("/profile"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self._send(200, profile((qs.get("person_id") or [""])[0]))
            if self.path.startswith("/relations"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                ent = (qs.get("entity") or [""])[0]
                return self._send(200, {"edges": G.current_relations(ent)})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if self.path.startswith("/recall"):
                return self._send(200, recall(d.get("q") or "", int(d.get("top") or 6),
                                              d.get("person") or "", d.get("scope") or "all",
                                              d.get("rerank") or "bge",
                                              since=d.get("since"), until=d.get("until")))
            if self.path.startswith("/consolidate"):
                return self._send(200, CO.run(db=MEM_DB,
                                             archive_days=int(d.get("archive_days") or 30),
                                             min_weight=float(d.get("min_weight") or 0.35)))
            if self.path.startswith("/sweep"):
                import turnlog as TL
                return self._send(200, {"closed": TL.sweep(db=MEM_DB)})
            if self.path.startswith("/context"):
                import context as CX
                return self._send(200, CX.build(
                    d.get("person") or "", d.get("q") or "",
                    int(d.get("top") or 5), int(d.get("max_chars") or 1200), db=MEM_DB,
                    include_self=bool(d.get("include_self")),
                    diary_days=int(d.get("diary_days") or 0),
                    min_score=float(d.get("min_score") or 0)))
            if self.path.startswith("/log"):
                import turnlog as TL
                r = TL.append(d.get("person") or "", d.get("user") or "", d.get("reply") or "",
                              d.get("ts"), db=MEM_DB)
                return self._send(200, r)
            if self.path.startswith("/ingest"):
                import autocapture as AC
                r = AC.capture_turn(d.get("user") or "", d.get("reply") or "",
                                    d.get("person") or "", d.get("ts"), db=MEM_DB)
                return self._send(200, r)
            if self.path.startswith("/experience"):
                # 任何插件做过的事：读帖、玩游戏、跑任务、出门看天气……都记成"经历"
                ev = d.get("text") or ""
                kind = d.get("kind") or "经历"
                title = d.get("title") or (kind + " " + time.strftime("%m-%d %H:%M"))
                r = remember(title, ev, d.get("person_id") or "self:agent",
                             [kind] + list(d.get("tags") or []),
                             importance=d.get("importance"))
                return self._send(200, {"ok": True, "id": r.get("id") or "", "kind": kind})
            if self.path.startswith("/remember"):
                return self._send(200, remember(d.get("title"), d.get("content"),
                                                d.get("person_id") or "", d.get("tags"),
                                                importance=d.get("importance")))
            if self.path.startswith("/relation"):
                r = G.add_relation(d.get("subject") or "", d.get("predicate") or "",
                                   d.get("object") or "", d.get("note") or "",
                                   d.get("evidence") or [])
                if r.get("ok"):
                    r["bound"] = _bind_refs(r.get("relation_id"))
                return self._send(200 if r.get("ok") else 400, r)
            if self.path.startswith("/why"):
                r = G.why(d.get("subject"), d.get("q"), int(d.get("k") or 5))
                if int(d.get("depth") or 1) >= 2:
                    r["chain"] = _expand(r.get("evidence") or [])
                return self._send(200, r)
            if self.path.startswith("/entity"):
                uid = str(d.get("id") or d.get("uid") or "").strip()
                if uid.isdigit():
                    uid = "user:" + uid
                if not uid:
                    return self._send(400, {"error": "id 必填"})
                G.upsert_entity(uid, d.get("name") or "", d.get("features") or {})
                _al = d.get("aliases") or []
                if _al:
                    con = G._conn()
                    import json as _json
                    row = con.execute("SELECT aliases FROM entities WHERE id=?", (uid,)).fetchone()
                    cur = _json.loads((row[0] if row else "") or "[]")
                    for a in _al:
                        if a and a not in cur:
                            cur.append(a)
                    con.execute("UPDATE entities SET aliases=? WHERE id=?", (_json.dumps(cur, ensure_ascii=False), uid))
                    con.commit()
                    con.close()
                return self._send(200, {"ok": True, "id": uid})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def log_message(self, *a):
        pass


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else ""
    port = int(arg or os.environ.get("CORE_PORT") or 8095)
    _cleanup_flow()
    print("记忆内核 http://127.0.0.1:%d  mem_db=%s  graph_db=%s" % (port, MEM_DB, G.DB_PATH))
    ThreadingHTTPServer(("127.0.0.1", port), H2).serve_forever()


if __name__ == "__main__":
    main()
