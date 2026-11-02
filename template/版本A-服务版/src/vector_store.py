# -*- coding: utf-8 -*-
"""FAISS 向量索引：分块 + HNSW 粗筛 + 候选精确 cosine + 墓碑/compact。

设计：
  · 长记忆按约 280 字分块，解决 BGE 上下文截断。
  · HNSW 只粗筛；命中块再和原记忆做精确 cosine。
  · 新增块增量 add；修改/删除用墓碑，墓碑过多才 compact。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

import numpy as np

try:
    import faiss
except Exception:  # pragma: no cover
    faiss = None

_LOCK = threading.Lock()
_WARMING = False
_STATE = {"chunks": {}, "ids": [], "vecs": None, "index": None, "dim": 0, "doc_map": {}}

VECTOR_DIR = os.path.abspath(os.environ.get(
    "VECTOR_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")))
INDEX_PATH = os.path.join(VECTOR_DIR, "vector.faiss")
META_PATH = os.path.join(VECTOR_DIR, "vector_meta.json")
VECS_PATH = os.path.join(VECTOR_DIR, "vector.npy")
CHUNK = 280
OVERLAP = 40
COMPACT_RATIO = 0.30


def _h(s):
    return hashlib.sha1(str(s or "").encode("utf-8")).hexdigest()[:16]


def _norm(a):
    a = np.asarray(a, dtype="float32")
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


def _chunks(text):
    t = (text or "").strip()
    if not t:
        return [""]
    out = []
    i = 0
    while i < len(t):
        out.append(t[i:i + CHUNK])
        if i + CHUNK >= len(t):
            break
        i += CHUNK - OVERLAP
    return out


def _collect(docs):
    """返回 (chunk_id, doc_index, chunk_text, chunk_hash) 列表。"""
    out = []
    for di, d in enumerate(docs):
        did = str(d.get("id"))
        for ci, part in enumerate(_chunks(d.get("text", ""))):
            cid = did + "\x1f" + _h(part)
            out.append((cid, di, part, _h(part)))
    return out


def _save(index, vecs, ids, meta):
    os.makedirs(VECTOR_DIR, exist_ok=True)
    open(INDEX_PATH + ".tmp", "wb").write(faiss.serialize_index(index))
    os.replace(INDEX_PATH + ".tmp", INDEX_PATH)
    np.save(VECS_PATH + ".tmp.npy", vecs)
    os.replace(VECS_PATH + ".tmp.npy", VECS_PATH)
    with open(META_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(META_PATH + ".tmp", META_PATH)


def _build(docs, embed_fn):
    chunks = _collect(docs)
    if not chunks:
        raise RuntimeError("没有可索引内容")
    vecs = _norm(embed_fn([c[2] for c in chunks]))
    index = faiss.IndexHNSWFlat(int(vecs.shape[1]), 32, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = 200
    index.add(vecs)
    ids = [c[0] for c in chunks]
    meta = {"ids": ids, "hashes": {c[0]: c[3] for c in chunks},
            "docs": {c[0]: c[1] for c in chunks}, "dim": int(vecs.shape[1]),
            "tombstones": []}
    _save(index, vecs, ids, meta)
    return index, vecs, ids, meta


def _load():
    if not (os.path.isfile(INDEX_PATH) and os.path.isfile(META_PATH) and os.path.isfile(VECS_PATH)):
        return None
    with open(META_PATH, encoding="utf-8") as f:
        meta = json.load(f)
    if "hashes" not in meta or "docs" not in meta:
        return None
    index = faiss.deserialize_index(np.frombuffer(open(INDEX_PATH, "rb").read(), dtype=np.uint8))
    vecs = np.load(VECS_PATH)
    return index, vecs, meta


def _ensure(docs, embed_fn):
    if faiss is None:
        raise RuntimeError("faiss-cpu 未安装")
    chunks = _collect(docs)
    cur = {c[0]: c[3] for c in chunks}
    if _STATE["index"] is not None and cur == _STATE["chunks"]:
        return _STATE["index"], _STATE["vecs"], _STATE["ids"], _STATE["doc_map"]
    loaded = _load()
    if loaded:
        index, vecs, meta = loaded
        old = meta.get("hashes") or {}
        ids = [str(x) for x in (meta.get("ids") or [])]
        doc_map = {str(k): int(v) for k, v in (meta.get("docs") or {}).items()}
        tombs = set(str(x) for x in (meta.get("tombstones") or []))
        added = [c for c in chunks if c[0] not in old]
        removed = [cid for cid in ids if cid not in cur]
        changed = any(cid in cur and cur[cid] != old.get(cid) for cid in ids)
        for cid in removed:
            tombs.add(cid)
        if changed:
            # 同一记忆内容变化：按 doc 级旧块做墓碑，再补新块
            for c in chunks:
                pass
            for cid in ids:
                if cid not in cur:
                    tombs.add(cid)
        if removed or changed:
            # 旧块标墓碑；新块直接加
            pass
        if added:
            nv = _norm(embed_fn([c[2] for c in added]))
            index.add(nv)
            vecs = np.vstack([vecs, nv])
            ids += [c[0] for c in added]
            for cid, di, _p, hh in added:
                old[cid] = hh
                doc_map[cid] = di
        tomb_list = sorted(tombs)
        if len(tomb_list) > max(8, int(len(ids) * COMPACT_RATIO)):
            index, vecs, ids, meta = _build(docs, embed_fn)
            _STATE.update({"chunks": cur, "ids": ids, "vecs": vecs, "index": index,
                           "dim": int(vecs.shape[1]), "doc_map": meta["docs"]})
            return index, vecs, ids, meta["docs"]
        _save(index, vecs, ids, {"ids": ids, "hashes": old, "docs": doc_map,
                                 "dim": int(vecs.shape[1]), "tombstones": tomb_list})
        _STATE.update({"chunks": cur, "ids": ids, "vecs": vecs, "index": index,
                       "dim": int(vecs.shape[1]), "doc_map": doc_map})
        return index, vecs, ids, doc_map
    index, vecs, ids, meta = _build(docs, embed_fn)
    _STATE.update({"chunks": cur, "ids": ids, "vecs": vecs, "index": index,
                   "dim": int(vecs.shape[1]), "doc_map": meta["docs"]})
    return index, vecs, ids, meta["docs"]


def _tombstones():
    try:
        with open(META_PATH, encoding="utf-8") as f:
            return set(json.load(f).get("tombstones") or [])
    except Exception:
        return set()


def search(docs, q, embed_fn, top=80):
    """返回 [(exact_cosine, doc_index), ...]；分块命中后按记忆去重。"""
    if _STATE["index"] is None and _WARMING:
        raise RuntimeError("向量索引正在后台构建")
    with _LOCK:
        _ensure(docs, embed_fn)
        tombs = _tombstones()
    # query embedding 不占索引锁，4 路并发在这里生效
    qv = _norm(embed_fn([q]))[0]
    with _LOCK:
        index = _STATE["index"]
        vecs = _STATE["vecs"]
        ids = _STATE["ids"]
        doc_map = _STATE["doc_map"]
        if index is None:
            raise RuntimeError("向量索引未就绪")
        index.hnsw.efSearch = max(64, min(512, int(top) * 2))
        _d, ind = index.search(qv.reshape(1, -1), min(max(int(top) * 3, 120), len(ids)))
        best = {}
        for i in ind[0]:
            if i < 0:
                continue
            cid = str(ids[i])
            if cid in tombs:
                continue
            di = doc_map.get(cid)
            if di is None:
                continue
            sc = float(np.dot(vecs[i], qv))
            if sc > best.get(di, -1):
                best[di] = sc
        return sorted([(s, i) for i, s in best.items()], key=lambda x: -x[0])[:int(top)]


def warm(docs, embed_fn):
    global _WARMING
    _WARMING = True
    try:
        with _LOCK:
            _ensure(docs, embed_fn)
    finally:
        _WARMING = False


def ready():
    return _STATE["index"] is not None
