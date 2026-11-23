# -*- coding: utf-8 -*-
"""memory_core —— 记忆内核（单进程，:8095）

把原来分散的三件事收进一个服务，共用一份代码和一处配置：
  · 检索（BM25 + 向量 + RRF + bge rerank）   —— 原 8093
  · 关系图谱（实体 / 关系边 / 事件向量）      —— 原 8094
  · 人物档案（印象 / 态度 / 特征）            —— 原来是插件里的逻辑

默认直接读写本地 sqlite（MEM_WRITE_MODE=local），不依赖 8001/Neo-MoFox；
旧库 schema 兼容读取，所有调用方只认 8095 这一个地址。

接口：
  GET  /health
  POST /recall      {q, top, person, scope, rerank}
  POST /remember    {title, content, person_id, tags}      → 本地 SQLite
  POST /relation    {subject, predicate, object, note, evidence}
  POST /why         {subject, q}
  GET  /relations?entity=
  POST /entity      {id, name, features}
  GET  /profile?person_id=user:10001
"""
from __future__ import annotations

import json
import re
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import identity as IDENT

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "retrieval"))
sys.path.insert(0, os.path.join(HERE, "..", "graph"))

import hybrid as H                      # noqa: E402
import reranker_onnx as R               # noqa: E402
import graph_server as G                # noqa: E402
import store as ST
import consolidate as CO
import distill as DL                    # noqa: E402

_default_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "metadata.db")
_alt_db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "mu-memory", "data", "booku_memory", "metadata.db")


def _pick_mem_db():
    """P1-3：默认 0 字节库已停用。只接受显式 MEM_DB 或有效的 mu-memory 库；
    解析不到就 fail-fast，绝不自动建空库。"""
    env = os.environ.get("MEM_DB", "").strip()
    if env:
        p = os.path.abspath(env)
        if os.path.isdir(p) or not os.path.exists(p) or os.path.getsize(p) <= 0:
            raise SystemExit("MEM_DB 不可用（目录/不存在/0 字节）：%s" % p)
        return p
    if os.path.isfile(_alt_db) and os.path.getsize(_alt_db) > 0:
        return os.path.abspath(_alt_db)
    raise SystemExit(
        "找不到可用记忆库：MEM_DB 未设置，且 %s 不存在或为 0 字节。"
        "请显式设置 MEM_DB（默认库已停用，不会自动建空库）。" % _alt_db)


MEM_DB = _pick_mem_db()
MEM_INBOX = os.environ.get("MEM_INBOX", "")  # 默认空：本地模式不再默认回落 8001（8001 已停用）

# 身份/别名属于本地私有数据，不进仓库：读 data/identity.json，
# 可用 MEM_IDENTITY_JSON 覆盖。文件缺失时用中性默认值，不崩。
IDENTITY_JSON = os.environ.get("MEM_IDENTITY_JSON") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "identity.json")


def _load_identity():
    try:
        with open(IDENTITY_JSON, encoding="utf-8") as fp:
            doc = json.load(fp)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


_IDENT_DOC = _load_identity()
CANONICAL = str(_IDENT_DOC.get("canonical") or "").strip() or "self:assistant"
SELF_NAME = CANONICAL.split(":", 1)[-1].strip()
USER_PERSON = str(_IDENT_DOC.get("user") or "").strip()
ALIASES = {str(k).strip(): str(v).strip()
           for k, v in (_IDENT_DOC.get("alias_map") or {}).items() if k and v}
SCHEMA_VERSION = "mumu-memory/2"
_lock = threading.Lock()
_idx = {}

# 并发准入：2026-11-21 事故里 871 个请求线程同时卡在 embed 上，把整机提交内存打满。
# 现在同时最多处理 MEM_MAX_INFLIGHT 个请求，超出的直接 503，线程立刻退出。
_MAX_INFLIGHT = max(1, int(os.environ.get("MEM_MAX_INFLIGHT") or 24))
_INFLIGHT = threading.BoundedSemaphore(_MAX_INFLIGHT)
_HTTP_TIMEOUT = int(os.environ.get("MEM_HTTP_TIMEOUT") or 60)


def _private_token():
    return os.environ.get("MEM_PRIVATE_TOKEN", "")


def _can_private(headers):
    """私有分区开关：未配置令牌时一律拒绝（fail-closed）。"""
    tok = _private_token()
    return bool(tok) and (headers or {}).get("X-MEM-Private-Token") == tok


def _visible(d, include_private=False, owner=""):
    """private 记录默认对所有读取路径不可见。"""
    if (d.get("visibility") or "shared") != "private":
        return True
    if not include_private:
        return False
    return not owner or (d.get("owner") or "") == owner


def _canon_person(person):
    p = str(person or "").strip()
    try:
        return IDENT.canonical(p, MEM_DB) or ALIASES.get(p, p or CANONICAL)
    except Exception:
        return ALIASES.get(p, p or CANONICAL)


def _init_identity():
    con = sqlite3.connect(MEM_DB, timeout=10)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS person_aliases "
                    "(alias TEXT PRIMARY KEY, canonical TEXT NOT NULL)")
        for a, c in ALIASES.items():
            con.execute("INSERT OR REPLACE INTO person_aliases(alias,canonical) VALUES(?,?)", (a, c))
        con.commit()
    finally:
        con.close()


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


def _local_items(person_id="", limit=200, offset=0):
    person_id = _canon_person(person_id) if person_id else ""
    con = sqlite3.connect(MEM_DB, timeout=10)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(booku_memory_records)")}
    priv = " AND COALESCE(visibility,'shared')<>'private'" if "visibility" in cols else ""
    arch = " AND COALESCE(is_archived,0)=0" if "is_archived" in cols else ""
    sql = ("SELECT memory_id, title, content, person_id, created_at, updated_at, importance "
           "FROM booku_memory_records WHERE COALESCE(is_deleted,0)=0" + arch + priv)
    args = []
    if person_id:
        sql += " AND person_id=?"
        args.append(person_id)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    rows = con.execute(sql, args).fetchall()
    ids = [r["memory_id"] for r in rows]
    tags = {}
    if ids:
        q = ("SELECT memory_id, tag_value FROM booku_memory_tags WHERE memory_id IN (%s)"
             % ",".join("?" for _ in ids))
        for tr in con.execute(q, ids):
            tags.setdefault(tr["memory_id"], []).append(tr["tag_value"])
    con.close()
    return {"items": [{
        "id": r["memory_id"],
        "title": r["title"] or "",
        "content_snippet": (r["content"] or "")[:240],
        "metadata": {
            "created_at": r["created_at"] or 0,
            "updated_at": r["updated_at"] or 0,
            "importance": r["importance"] or 0,
            "person_id": r["person_id"] or "",
            "core_tags": tags.get(r["memory_id"], []),
        },
    } for r in rows]}


def _get(url, timeout=20):
    # 本地模式：/booku-memory/api/memories 已由本地 SQLite 承担，不再访问 8001
    if "/booku-memory/api/memories" in url:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        person = (qs.get("person_id") or [""])[0]
        limit = int((qs.get("limit") or ["200"])[0] or 200)
        offset = int((qs.get("offset") or ["0"])[0] or 0)
        return _local_items(person, limit, offset)
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


def recall(q, top=6, person="", scope="all", rerank="bge", since=None, until=None, namespace=None,
           include_private=False, owner=""):
    # namespace 不显式给时按环境变量兜底：8095 默认 chat，8097 设的是 dsh。
    # 原实现硬写 "chat"，拷到 8097 会把 dsh 盖掉。
    if not namespace:
        namespace = os.environ.get("MEM_DEFAULT_NAMESPACE", "chat")
    ent = _index()
    all_docs = ent["docs"]
    bm_all = ent["bm"]
    person = ST.canon_person(person)

    # 先做过滤条件，但向量索引始终基于全库；候选召回后再过滤
    allow = None
    if person and scope == "person":
        allow = {"", person, "self:agent"}
    docs = []
    for d in all_docs:
        if namespace and namespace != "all" and (d.get("namespace") or "chat") != namespace:
            continue
        if allow is not None and ST.canon_person(d.get("person")) not in allow:
            continue
        if not _visible(d, include_private, owner):
            continue
        if since and float(d.get("ts") or 0) < float(since):
            continue
        if until and float(d.get("ts") or 0) > float(until):
            continue
        docs.append(d)
    if not docs:
        return {"q": q, "mode": "empty", "corpus": 0, "results": []}

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

    bm = H.BM25(docs)
    lex = bm.search(q, top=50)
    dense = []
    dense_err = ""
    try:
        pos = {str(d["id"]): i for i, d in enumerate(docs)}
        want = max(200, min(len(all_docs), int(top) * 30))
        for sc, ai in H.dense_search(all_docs, q, top=want):
            did = str(all_docs[ai]["id"])
            if did in pos:
                dense.append((sc, pos[did]))
    except Exception as e:
        # 向量不可用就只走字面 + 轻量重排：不降级成"整库 embed"，也不报 500
        dense = []
        dense_err = repr(e)

    fused = H.rrf([x for x in (lex, dense) if x])
    # 实体/标签信号（Mem0 实体链接的最小版）：不建新表，直接用 core_tags
    try:
        ent = H.tag_match(docs, q)
        if ent:
            fused = H.rrf([x for x in (lex, dense, ent) if x])
    except Exception:
        pass
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
        rec = H.recency_factor(d.get("ts"))
        out.append({"id": d["id"], "person": d.get("person") or "", "ts": d.get("ts") or 0,
                    "title": (d.get("title") or "")[:60],
                    "text": (d.get("text") or "")[:200],
                    "score": round(float(sc) * dec * rec, 4),
                    "base": round(float(sc), 4), "decay": round(dec, 3),
                    "recency": round(rec, 3), "importance": imp,
                    "why": why})
    out.sort(key=lambda x: -x["score"])
    top_score = out[0]["score"] if out else 0.0
    for x in out:
        x["score_norm"] = round(x["score"] / top_score, 4) if top_score else 0.0
        _bump(x["id"])
    res = {"q": q, "mode": mode, "corpus": len(docs), "results": out}
    if dense_err:
        res["dense_error"] = dense_err
    return res


_WARM_LOCK = threading.Lock()


def _warm_async(retry_delay=60, attempts=6):
    """后台把向量索引追平；新写入不再等第一次查询才建索引。

    8081 暂时不可达时按 retry_delay 重试（有界次数）：既不放弃索引，
    也不把重试压到请求线程上（请求路径只降级成字面检索）。
    """
    import vector_store as _VS
    _VS._WARMING = True

    def run():
        if not _WARM_LOCK.acquire(blocking=False):
            return
        try:
            import vector_store as VS
            for i in range(max(1, int(attempts))):
                try:
                    VS.warm(H.load_corpus(MEM_DB), H.embed)
                    return
                except Exception as e:
                    print("vector warm failed (%d/%d): %r"
                          % (i + 1, int(attempts), e), flush=True)
                    if i + 1 < int(attempts):
                        VS._WARMING = True     # 重试间隙也别让请求线程去建索引
                        time.sleep(float(retry_delay))
        finally:
            _WARM_LOCK.release()
    threading.Thread(target=run, daemon=True).start()


# ---------- 8001 booku-memory 兼容层（CE 等旧客户端；内部转 store/recall） ----------
def booku_search(q, limit=5, person="", namespace="all"):
    """GET/POST /booku-memory/api/search → 本地 recall，返回 {items:[...]}。"""
    try:
        limit = max(1, min(200, int(limit or 5)))
    except Exception:
        limit = 5
    r = recall(q or "", top=limit, person=person or "", scope="all", rerank="light",
               namespace=namespace or "all")
    items = []
    for x in r.get("results") or []:
        txt = x.get("text") or ""
        items.append({"id": x.get("id") or "", "title": x.get("title") or "",
                      "content": txt, "text": txt, "source": x.get("id") or "memory-core",
                      "person_id": x.get("person") or "", "created_at": x.get("ts") or 0,
                      "score": x.get("score")})
    return {"items": items, "total": len(items), "mode": r.get("mode"), "q": q}


_TURN_MARK = re.compile(r"\[(?:桌|查|读|记|归|搜笔|读笔|日|地图|网|网链|网搜|搜网)[:：][^\]]*\]")
_TURN_SYS = re.compile(r"^\s*(?:系统|system|指令|instruction|提示词)\s*[:：]", re.I)


def _clean_turn(text):
    """过滤工具标记/系统提示行；只留下可给人看的对话文本。"""
    out = []
    for line in str(text or "").splitlines():
        if _TURN_SYS.match(line):
            continue
        line = _TURN_MARK.sub("", line).strip()
        if line:
            out.append(line)
    return "\n".join(out).strip()


def turns(person="", limit=20):
    """本地 turn_logs → 正序 user/assistant 轮次（limit 上限 20）。

    返回 {"person","limit","turns":[{"role","text","ts","session_id"}]}；无记录返回空列表。
    """
    try:
        limit = max(1, min(20, int(limit or 20)))
    except Exception:
        limit = 20
    raw = (person or "").strip()
    p = ST.canon_person(raw)
    cands = [x for x in dict.fromkeys([raw, p, p[5:] if p.startswith("user:") else "",
                                       raw[5:] if raw.startswith("user:") else ""]) if x]
    con = sqlite3.connect(MEM_DB, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        # 别名跨通道：工作台的 dashboard 账号归到主人 QQ（旧库可能没有该表，容忍）
        try:
            for c in list(cands):
                row = con.execute(
                    "SELECT canonical FROM person_aliases WHERE alias=?", (c,)
                ).fetchone()
                if row and row[0]:
                    for v in (row[0], row[0][5:] if row[0].startswith("user:") else ""):
                        if v and v not in cands:
                            cands.append(v)
        except sqlite3.OperationalError:
            pass
        q = ("SELECT id,ts,user_text,reply_text,session_id FROM turn_logs "
             "WHERE person IN (%s) ORDER BY ts DESC, id DESC LIMIT ?"
             % ",".join("?" for _ in cands)) if cands else ""
        rows = con.execute(q, cands + [limit]).fetchall() if cands else []
    finally:
        con.close()
    out = []
    for r in reversed(rows):
        for role, txt in (("user", _clean_turn(r["user_text"])),
                          ("assistant", _clean_turn(r["reply_text"]))):
            if not txt:
                continue
            item = {"role": role, "text": txt, "ts": r["ts"] or 0,
                    "session_id": r["session_id"] or ""}
            if out and out[-1]["role"] == role:
                out[-1] = item          # 连续同角色：保留最新一条，保证交替
            else:
                out.append(item)
    while out and out[0]["role"] != "user":
        out.pop(0)
    return {"person": p, "limit": limit, "turns": out}


def booku_list(limit=200, offset=0, person="", folder=""):
    """GET /booku-memory/api/memories → 列表（默认不含 private）。"""
    con = sqlite3.connect(MEM_DB, timeout=10)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(booku_memory_records)")}
    priv = " AND COALESCE(visibility,'shared')<>'private'" if "visibility" in cols else ""
    where, args = ["COALESCE(is_deleted,0)=0" + priv], []
    if person:
        where.append("person_id=?")
        args.append(person)
    if folder:
        where.append("folder_id=?")
        args.append(folder)
    w = " WHERE " + " AND ".join(where)
    total = con.execute("SELECT COUNT(*) FROM booku_memory_records" + w, args).fetchone()[0]
    rows = con.execute(
        "SELECT memory_id, title, content, person_id, folder_id, created_at, updated_at, "
        "importance, namespace FROM booku_memory_records" + w +
        " ORDER BY created_at DESC LIMIT ? OFFSET ?", args + [int(limit), int(offset)]).fetchall()
    con.close()
    items = [{"id": r["memory_id"], "title": r["title"] or "",
              "content": r["content"] or "", "content_snippet": (r["content"] or "")[:240],
              "text": r["content"] or "", "person_id": r["person_id"] or "",
              "folder_id": r["folder_id"] or "", "created_at": r["created_at"] or 0,
              "updated_at": r["updated_at"] or 0, "importance": r["importance"],
              "namespace": r["namespace"] or "chat"} for r in rows]
    return {"items": items, "total": int(total)}


def booku_remember(d):
    """POST /booku-memory/api/memories（booku 形状）→ 本地 remember。"""
    tags = list(d.get("core_tags") or []) + list(d.get("diffusion_tags") or [])
    bucket = str(d.get("bucket") or "")
    ns = d.get("namespace") or ("game" if bucket == "game" else "chat")
    r = remember(d.get("title") or "", d.get("content") or d.get("text") or "",
                 d.get("person_id") or d.get("person") or "", tags,
                 importance=d.get("importance"), namespace=ns,
                 folder=d.get("folder_id") or "")
    return {"id": r.get("id") or "", "success": bool(r.get("ok")),
            "merged": r.get("merged"), "mode": r.get("mode"),
            "item": {"id": r.get("id") or "", "title": d.get("title") or "",
                     "folder_id": d.get("folder_id") or "", "status": "active"}}


def remember(title, content, person_id="", tags=None, importance=None, ts=None,
             origin="unknown", author="", source_event="", version=1, namespace="chat",
             owner="", visibility="shared", deletable=1, folder=""):
    orig = str(person_id or "")
    person_id = _canon_person(orig)
    if not author:
        author = "user" if orig.startswith("qq:") else (SELF_NAME if orig.startswith("agent:") else "")
    if origin == "unknown":
        origin = "chat" if orig.startswith("qq:") else ("game" if orig.startswith("agent:") else "unknown")
    # 独立模式：不依赖任何平台，直接写本地库
    if os.environ.get("MEM_WRITE_MODE", "local") == "local":
        r = ST.add(title, content, person=person_id, tags=tags or [], importance=importance, ts=ts,
                   origin=origin, author=author, source_event=source_event, version=version,
                   db=MEM_DB, namespace=namespace, owner=owner, visibility=visibility,
                   deletable=deletable, folder=folder)
        _warm_async()
        return {"ok": True, "id": r["id"], "merged": r["merged"],
                "revived": bool(r.get("revived")), "kind": r.get("kind") or "",
                "mode": "local"}
    if not MEM_INBOX:
        return {"ok": False, "mode": "inbox-unconfigured",
                "error": "MEM_INBOX 未配置：MEM_WRITE_MODE 非 local 时必须显式提供兼容地址（8001 已停用，不再默认回落）"}
    payload = {"title": (title or "无题")[:60], "content": content or "",
               "folder_id": "default", "bucket": "memory", "memory_type": "event",
               "status": "active", "person_id": person_id,
               "namespace": namespace or "chat",
               "core_tags": tags or ["对话"], "diffusion_tags": ["core"],
               "opposing_tags": ["未分类"],
        "origin": origin, "author": author,
        "source_event": source_event, "version": int(version or 1),
        "owner": owner, "visibility": visibility, "deletable": int(deletable)}
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
    person_id = ST.canon_person(person_id)
    out = {"impression": "", "attitude": "", "name": "", "features": {},
           "impression_ts": 0, "attitude_ts": 0}
    try:
        # 本地优先：直接查本地库的 印象/态度 标签，不依赖 MEM_INBOX / 已停用的 8001
        d = _local_items(person_id, 200)
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
    timeout = _HTTP_TIMEOUT          # 客户端不发完整请求时，socket 读超时，线程不会永久挂住
    protocol_version = "HTTP/1.0"
    ADMIT_WAIT = float(os.environ.get("MEM_ADMIT_WAIT") or 3)

    def _drain_request(self):
        """读掉请求行/头/体，避免关连接时 RST 把 503 响应冲掉。"""
        try:
            self.connection.settimeout(1.0)
            total, headers = 0, []
            while total < (1 << 20):
                line = self.rfile.readline(65537)
                if not line:
                    break
                total += len(line)
                if line in (b"\r\n", b"\n"):
                    break
                headers.append(line)
            clen = 0
            for h in headers:
                if h.lower().startswith(b"content-length:"):
                    try:
                        clen = int(h.split(b":", 1)[1].strip())
                    except Exception:
                        clen = 0
            if clen > 0:
                self.rfile.read(min(clen, 1 << 20))
        except Exception:
            pass

    def handle(self):
        """并发准入：先等 MEM_ADMIT_WAIT 秒，还满就 503，不再无限起线程堆在 embed 上。"""
        admitted = _INFLIGHT.acquire(blocking=False) or _INFLIGHT.acquire(timeout=self.ADMIT_WAIT)
        if not admitted:
            try:
                self._drain_request()
                b = json.dumps({"error": "too many concurrent requests",
                                "inflight_max": _MAX_INFLIGHT}).encode("utf-8")
                self.close_connection = True
                self.wfile.write(b"HTTP/1.0 503 Service Unavailable\r\n"
                                 b"Content-Type: application/json; charset=utf-8\r\n"
                                 b"Content-Length: " + str(len(b)).encode() + b"\r\n"
                                 b"Connection: close\r\n\r\n" + b)
                self.wfile.flush()
            except Exception:
                pass
            return
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # 客户端在「读请求行」阶段就被 RST 掉（healthz 探针、页面关掉、blockd 超时取消）。
            # _send 只挡住了写响应那一半，读这一半会一路冒到 socketserver.handle_error，
            # 每个探针堆一条 40 行栈 —— 2026-11-23 那 28204 行就是这么来的。
            pass
        finally:
            _INFLIGHT.release()

    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # 客户端先断开了（blockd 的 /recall 超时取消、或页面关掉），写到一半
            # 抛 WinError 10053 是对方的动作，不是这边的故障；别让它进错误日志，
            # 否则每次超时都往 err.log 里堆一条 40 行的栈。
            pass

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
            if self.path.startswith("/capabilities"):
                return self._send(200, {
                    "ok": True, "schema": SCHEMA_VERSION,
                    "canonical_person": CANONICAL, "aliases": ALIASES,
                    "view": ["chat", "game", ""],
                    "private_enabled": bool(_private_token()),
                })
            if self.path.startswith("/health"):
                con = G._conn()
                n = lambda t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                out = {"ok": True, "docs": len(_index()["docs"]), "entities": n("entities"),
                       "relations": n("relations"), "events": n("events"),
                       "mem_db": MEM_DB, "graph_db": G.DB_PATH}
                con.close()
                try:
                    import vector_store as _VS
                    out["vectors"] = {
                        "ready": bool(_VS.ready()),
                        "build_cooldown_s": round(max(0.0, _VS._BUILD_COOL["until"] - time.time()), 1),
                        "last_build_error": _VS._BUILD_COOL["err"] or "",
                    }
                except Exception as e:
                    out["vectors"] = {"ready": False, "error": repr(e)}
                out["embed"] = {"url": H.EMB_URL,
                                "circuit_open": bool(H._circuit_open()),
                                "circuit_s": round(max(0.0, H._EMB_FAIL["until"] - time.time()), 1),
                                "last_error": H._EMB_FAIL["err"] or ""}
                out["inflight_max"] = _MAX_INFLIGHT
                out["threads"] = threading.active_count()
                return self._send(200, out)
            if self.path.startswith("/booku-memory/api/search"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                p = {k: v[0] for k, v in qs.items()}
                return self._send(200, booku_search(p.get("q") or "", p.get("limit") or 5,
                                                    p.get("person_id") or p.get("person") or "",
                                                    p.get("namespace") or "all"))
            if self.path.startswith("/booku-memory/api/memories"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                p = {k: v[0] for k, v in qs.items()}
                return self._send(200, booku_list(int(p.get("limit") or 200),
                                                  int(p.get("offset") or 0),
                                                  p.get("person_id") or p.get("person") or "",
                                                  p.get("folder_id") or ""))
            if self.path.startswith("/turns"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                p = {k: v[0] for k, v in qs.items()}
                return self._send(200, turns(p.get("person") or "", p.get("limit") or 20))
            if self.path.startswith("/list"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                params = {k: v[0] for k, v in qs.items()}
                return self._send(200, _local_items(
                    params.get("person_id", ""),
                    int(params.get("limit", 200) or 200),
                    int(params.get("offset", 0) or 0)))
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
                can_priv = _can_private(self.headers)
                owner_q = d.get("owner") or d.get("person") or ""
                return self._send(200, recall(d.get("q") or "", int(d.get("top") or 6),
                                              d.get("person") or "", d.get("scope") or "all",
                                              d.get("rerank") or "bge",
                                              since=d.get("since"), until=d.get("until"),
                                              namespace=d.get("namespace") or "chat",
                                              include_private=bool(d.get("include_private")) and can_priv,
                                              owner=owner_q if can_priv else ""))
            if self.path.startswith("/consolidate"):
                return self._send(200, CO.run(db=MEM_DB,
                                             archive_days=int(d.get("archive_days") or 30),
                                             min_weight=float(d.get("min_weight") or 0.35)))
            if self.path.startswith("/sweep"):
                import turnlog as TL
                return self._send(200, {"closed": TL.sweep(db=MEM_DB)})
            if self.path.startswith("/decay"):
                import decay as DE
                return self._send(200, DE.run(db=MEM_DB))
            if self.path.startswith("/context"):
                import context as CX
                can_priv = _can_private(self.headers)
                return self._send(200, CX.build(
                    d.get("person") or "", d.get("q") or "",
                    int(d.get("top") or 5), int(d.get("max_chars") or 1200), db=MEM_DB,
                    include_self=bool(d.get("include_self")),
                    diary_days=int(d.get("diary_days") or 0),
                    min_score=float(d.get("min_score") or 0),
                    namespace=d.get("namespace") or "chat",
                    include_private=bool(d.get("include_private")) and can_priv,
                    # 只给 QQ 要「眼前」：DSH 那边能自己查，不需要塞注入包
                    eyes=bool(d.get("eyes")),
                    # 记忆块第五段（blockd 8099）：默认开，body {"blocks":0} 可关（A/B 用）
                    use_blocks=bool(d.get("blocks", 1)),
                    blocks_agent=d.get("blocks_agent") or "",
                    owner=(d.get("owner") or d.get("person") or "") if can_priv else ""))
            if self.path.startswith("/log"):
                import turnlog as TL
                r = TL.append(d.get("person") or "", d.get("user") or "", d.get("reply") or "",
                              d.get("ts"), db=MEM_DB)
                auto = {"enabled": False, "stored": []}
                if os.environ.get("MEM_AUTOCAPTURE", "1") != "0":
                    import autocapture as AC
                    auto = AC.capture_turn(d.get("user") or "", d.get("reply") or "",
                                          d.get("person") or "", d.get("ts"), db=MEM_DB)
                    auto["enabled"] = True
                r["auto_capture"] = auto
                _warm_async()
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
                             importance=d.get("importance"),
                             namespace=d.get("namespace") or ("game" if str(kind).startswith("游戏") else "chat"))
                return self._send(200, {"ok": True, "id": r.get("id") or "", "kind": kind})
            if self.path.startswith("/booku-memory/api/memories"):
                return self._send(200, booku_remember(d))
            if self.path.startswith("/booku-memory/api/search"):
                return self._send(200, booku_search(d.get("q") or "", d.get("limit") or 5,
                                                    d.get("person_id") or d.get("person") or "",
                                                    d.get("namespace") or "all"))
            if self.path.startswith("/emotion"):
                mid = str(d.get("memory_id") or "").strip()
                axes = d.get("axes") or {}
                if not mid:
                    return self._send(400, {"error": "memory_id required"})
                try:
                    con = sqlite3.connect(MEM_DB, timeout=10)
                    con.execute("CREATE TABLE IF NOT EXISTS booku_memory_emotions("
                                "memory_id TEXT PRIMARY KEY, axes_json TEXT, ts REAL, source TEXT)")
                    con.execute("INSERT INTO booku_memory_emotions(memory_id,axes_json,ts,source) "
                                "VALUES(?,?,?,?) ON CONFLICT(memory_id) DO UPDATE SET "
                                "axes_json=excluded.axes_json,ts=excluded.ts,source=excluded.source",
                                (mid, json.dumps(axes, ensure_ascii=False),
                                 float(d.get("ts") or time.time()), str(d.get("source") or "avm")))
                    con.commit()
                    con.close()
                    return self._send(200, {"ok": True, "memory_id": mid})
                except Exception as e:
                    return self._send(500, {"error": repr(e)})
            if self.path.startswith("/conclude"):
                txt = str(d.get("text") or "").strip()
                if not txt:
                    return self._send(400, {"error": "text 必填"})
                j = DL.judge(txt)
                if not j.get("ok"):
                    return self._send(200, {"ok": False, "conclusions": [],
                                            "error": j.get("error")})
                rules = j.get("rules") or []
                if not rules:
                    return self._send(200, {"ok": True, "conclusions": [],
                                            "raw": j.get("raw") or ""})
                pid = _canon_person(str(d.get("person_id") or ""))
                ns = str(d.get("namespace") or "chat")
                dry = bool(d.get("dry_run"))
                saved = []
                for rule in rules:
                    dup = DL.find_similar(rule, MEM_DB, ns)
                    if dup:
                        saved.append({"rule": rule, "dup_of": dup, "skipped": True})
                        continue
                    if dry:
                        saved.append({"rule": rule, "dry_run": True})
                        continue
                    r = ST.add("结论·" + rule[:40], rule, person=pid, tags=["结论"],
                               memory_type="conclusion", db=MEM_DB, namespace=ns,
                               origin="chat", author=SELF_NAME)
                    saved.append({"id": r.get("id"), "rule": rule,
                                  "merged": r.get("merged"), "dup_id": r.get("dup_id")})
                return self._send(200, {"ok": True, "conclusions": saved,
                                        "raw": j.get("raw") or ""})
            if self.path.startswith("/remember"):
                vis = str(d.get("visibility") or "shared").strip().lower()
                owner_in = str(d.get("owner") or d.get("person_id") or "").strip()
                if vis not in ST.VISIBILITIES:
                    return self._send(400, {"error": "visibility 只能是 shared 或 private"})
                if vis == "private" and not _can_private(self.headers):
                    return self._send(403, {"error": "写 private 需要 X-MEM-Private-Token",
                                            "refused": "private"})
                return self._send(200, remember(d.get("title"), d.get("content"),
                                                d.get("person_id") or "", d.get("tags"),
                                                importance=d.get("importance"),
                                                namespace=d.get("namespace") or "chat",
                                                owner=owner_in if vis == "private" else "",
                                                visibility=vis,
                                                deletable=d.get("deletable", 1)))
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
            if self.path.startswith("/export"):
                can_priv = _can_private(self.headers)
                out = ST.export(db=MEM_DB, out_dir=os.path.join(HERE, "..", "data", "backup"),
                                include_private=bool(d.get("include_private")) and can_priv,
                                tag=str(d.get("tag") or "api"))
                return self._send(200, out)
            return self._send(404, {"error": "not found"})
        except PermissionError as e:
            self._send(403, {"error": str(e), "refused": "private"})
        except KeyError as e:
            self._send(404, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def do_DELETE(self):
        try:
            p = urllib.parse.urlparse(self.path)
            if not p.path.startswith("/memories/"):
                return self._send(404, {"error": "not found"})
            mid = p.path.split("/memories/", 1)[1].strip("/")
            actor = "self" if _can_private(self.headers) else "user"
            return self._send(200, ST.soft_delete(mid, actor=actor, db=MEM_DB))
        except PermissionError as e:
            self._send(403, {"error": str(e), "refused": "private"})
        except KeyError as e:
            self._send(404, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def do_PUT(self):
        self._send(405, {"error": "没有修改接口：记忆只做新增/合并；private 记录用户侧不可改"})

    def do_PATCH(self):
        self._send(405, {"error": "没有修改接口：记忆只做新增/合并；private 记录用户侧不可改"})

    def log_message(self, *a):
        pass


class CoreServer(ThreadingHTTPServer):
    """连接队列默认太短（5），突发并发时客户端会直接 connection refused。"""
    daemon_threads = True
    request_queue_size = max(8, int(os.environ.get("MEM_BACKLOG") or 128))

    def handle_error(self, request, client_address):
        # 兜底：客户端半路走人（10053 中止 / 10054 重置 / 10058 已关闭）是对方的动作，
        # 不是这边的故障，不该变成日志里一条 40 行的栈。真异常照旧交给 socketserver。
        if isinstance(sys.exc_info()[1],
                      (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].isdigit() else ""
    port = int(arg or os.environ.get("CORE_PORT") or 8095)
    _init_identity()
    _cleanup_flow()
    print("记忆内核 http://127.0.0.1:%d  mem_db=%s  graph_db=%s" % (port, MEM_DB, G.DB_PATH))
    print("嵌入端点 %s · 并发上限 %d · embed 截止 %.0fs"
          % (H.EMB_URL, _MAX_INFLIGHT, H._EMB_DEADLINE), flush=True)
    _warm_async()
    CoreServer(("127.0.0.1", port), H2).serve_forever()


if __name__ == "__main__":
    main()
