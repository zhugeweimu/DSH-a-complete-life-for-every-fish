# -*- coding: utf-8 -*-
"""FAISS 向量索引：HNSW 粗筛 + 候选精确 cosine。

设计：
  · 索引是全库的，不做 person/时间过滤；过滤放在候选召回之后。
  · 新增记忆只增量 add；内容修改/删除才重建。
  · 索引层允许近似，检索层对候选重新精确打分。
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
_STATE = {"hashes": {}, "ids": [], "vecs": None, "index": None, "dim": 0}

VECTOR_DIR = os.path.abspath(os.environ.get(
    "VECTOR_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")))
INDEX_PATH = os.path.join(VECTOR_DIR, "vector.faiss")
META_PATH = os.path.join(VECTOR_DIR, "vector_meta.json")
VECS_PATH = os.path.join(VECTOR_DIR, "vector.npy")


def _h(d):
    x = hashlib.sha1()
    x.update(str(d.get("id") or "").encode("utf-8"))
    x.update(b"\x00")
    x.update((d.get("text") or "")[:2000].encode("utf-8"))
    return x.hexdigest()


def _norm(a):
    a = np.asarray(a, dtype="float32")
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


def _save(index, vecs, ids, hashes):
    os.makedirs(VECTOR_DIR, exist_ok=True)
    open(INDEX_PATH + ".tmp", "wb").write(faiss.serialize_index(index))
    os.replace(INDEX_PATH + ".tmp", INDEX_PATH)
    np.save(VECS_PATH + ".tmp.npy", vecs)
    os.replace(VECS_PATH + ".tmp.npy", VECS_PATH)
    with open(META_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"ids": ids, "hashes": hashes, "dim": int(vecs.shape[1])},
                  f, ensure_ascii=False)
    os.replace(META_PATH + ".tmp", META_PATH)


def _build(docs, embed_fn):
    if faiss is None:
        raise RuntimeError("faiss-cpu 未安装")
    vecs = _norm(embed_fn([d.get("text", "")[:2000] for d in docs]))
    index = faiss.IndexHNSWFlat(int(vecs.shape[1]), 32, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = 200
    index.add(vecs)
    ids = [d.get("id") for d in docs]
    hashes = {str(d.get("id")): _h(d) for d in docs}
    _save(index, vecs, ids, hashes)
    return index, vecs, ids, hashes


def _load():
    if not (os.path.isfile(INDEX_PATH) and os.path.isfile(META_PATH) and os.path.isfile(VECS_PATH)):
        return None
    with open(META_PATH, encoding="utf-8") as f:
        meta = json.load(f)
    if "hashes" not in meta:
        return None
    index = faiss.deserialize_index(np.frombuffer(open(INDEX_PATH, "rb").read(), dtype=np.uint8))
    vecs = np.load(VECS_PATH)
    return index, vecs, meta.get("ids") or [], meta.get("hashes") or {}


def _ensure(docs, embed_fn):
    if faiss is None:
        raise RuntimeError("faiss-cpu 未安装")
    cur = {str(d.get("id")): _h(d) for d in docs}
    if _STATE["index"] is not None and cur == _STATE["hashes"]:
        return _STATE["index"], _STATE["vecs"], _STATE["ids"]
    loaded = _load()
    if loaded:
        index, vecs, ids, old = loaded
        old_ids = [str(x) for x in ids]
        added = [d for d in docs if str(d.get("id")) not in old]
        changed = any(str(i) in cur and cur[str(i)] != old.get(str(i)) for i in old_ids)
        removed = any(str(i) not in cur for i in old_ids)
        if not changed and not removed:
            if added:
                nv = _norm(embed_fn([d.get("text", "")[:2000] for d in added]))
                index.add(nv)
                vecs = np.vstack([vecs, nv])
                ids = old_ids + [str(d.get("id")) for d in added]
                hashes = dict(old)
                hashes.update({str(d.get("id")): _h(d) for d in added})
                _save(index, vecs, ids, hashes)
            else:
                hashes = old
            _STATE.update({"hashes": cur, "ids": [str(x) for x in ids], "vecs": vecs,
                           "index": index, "dim": int(vecs.shape[1])})
            return index, vecs, _STATE["ids"]
    index, vecs, ids, hashes = _build(docs, embed_fn)
    _STATE.update({"hashes": cur, "ids": [str(x) for x in ids], "vecs": vecs,
                   "index": index, "dim": int(vecs.shape[1])})
    return index, vecs, _STATE["ids"]


def search(docs, q, embed_fn, top=80):
    """返回 [(exact_cosine, doc_index), ...]；索引全库，调用方负责过滤。"""
    with _LOCK:
        index, vecs, ids = _ensure(docs, embed_fn)
        pos = {str(x): i for i, x in enumerate(ids)}
        qv = _norm(embed_fn([q]))[0]
        index.hnsw.efSearch = max(64, min(256, int(top) * 2))
        _d, ind = index.search(qv.reshape(1, -1), min(int(top), len(ids)))
        out = []
        for i in ind[0]:
            if i < 0:
                continue
            di = pos.get(str(ids[i]))
            if di is not None:
                out.append((float(np.dot(vecs[i], qv)), di))
        out.sort(key=lambda x: -x[0])
        return out
