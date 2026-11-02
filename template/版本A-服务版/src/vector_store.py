# -*- coding: utf-8 -*-
"""FAISS vector index: HNSW coarse recall + exact cosine re-score.

索引层：HNSW，近似即可，只负责把候选捞出来。
检索层：对候选重新做精确 cosine，再交给 RRF / rerank。
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
_STATE = {"sig": "", "ids": [], "vecs": None, "index": None, "dim": 0}

VECTOR_DIR = os.path.abspath(os.environ.get("VECTOR_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")))
INDEX_PATH = os.path.join(VECTOR_DIR, "vector.faiss")
META_PATH = os.path.join(VECTOR_DIR, "vector_meta.json")
VECS_PATH = os.path.join(VECTOR_DIR, "vector.npy")


def _signature(docs):
    h = hashlib.sha1()
    for d in docs:
        h.update(str(d.get("id") or "").encode("utf-8"))
        h.update(b"\x00")
        h.update((d.get("text") or "")[:2000].encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


def _norm(a):
    a = np.asarray(a, dtype="float32")
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


def _build(docs, embed_fn):
    if faiss is None:
        raise RuntimeError("faiss-cpu 未安装")
    vecs = _norm(embed_fn([d.get("text", "")[:2000] for d in docs]))
    dim = int(vecs.shape[1])
    index = faiss.IndexHNSWFlat(dim, 32, faiss.METRIC_INNER_PRODUCT)
    index.hnsw.efConstruction = 200
    index.add(vecs)
    os.makedirs(VECTOR_DIR, exist_ok=True)
    open(INDEX_PATH + ".tmp", "wb").write(faiss.serialize_index(index))
    os.replace(INDEX_PATH + ".tmp", INDEX_PATH)
    np.save(VECS_PATH + ".tmp.npy", vecs)
    os.replace(VECS_PATH + ".tmp.npy", VECS_PATH)
    with open(META_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"sig": _signature(docs), "ids": [d.get("id") for d in docs], "dim": dim},
                  f, ensure_ascii=False)
    os.replace(META_PATH + ".tmp", META_PATH)
    return index, vecs, [d.get("id") for d in docs], _signature(docs)


def _ensure(docs, embed_fn):
    sig = _signature(docs)
    if _STATE["sig"] == sig and _STATE["index"] is not None:
        return _STATE["index"], _STATE["vecs"], _STATE["ids"]
    if faiss is None:
        raise RuntimeError("faiss-cpu 未安装")
    if os.path.isfile(INDEX_PATH) and os.path.isfile(META_PATH) and os.path.isfile(VECS_PATH):
        try:
            with open(META_PATH, encoding="utf-8") as f:
                meta = json.load(f)
            if meta.get("sig") == sig:
                index = faiss.deserialize_index(np.frombuffer(open(INDEX_PATH, "rb").read(), dtype=np.uint8))
                vecs = np.load(VECS_PATH)
                _STATE.update({"sig": sig, "ids": meta.get("ids") or [], "vecs": vecs,
                               "index": index, "dim": int(meta.get("dim") or vecs.shape[1])})
                return index, vecs, _STATE["ids"]
        except Exception:
            pass
    index, vecs, ids, sig = _build(docs, embed_fn)
    _STATE.update({"sig": sig, "ids": ids, "vecs": vecs, "index": index, "dim": vecs.shape[1]})
    return index, vecs, ids


def search(docs, q, embed_fn, top=80):
    """返回 [(exact_cosine, doc_index), ...]；HNSW 只负责粗筛。"""
    with _LOCK:
        index, vecs, ids = _ensure(docs, embed_fn)
        pos = {str(x): i for i, x in enumerate(ids)}
        qv = _norm(embed_fn([q]))[0]
        index.hnsw.efSearch = max(64, min(256, top * 2))
        _d, ind = index.search(qv.reshape(1, -1), min(top, len(ids)))
        out = []
        for i in ind[0]:
            if i < 0:
                continue
            exact = float(np.dot(vecs[i], qv))
            di = pos.get(str(ids[i]))
            if di is not None:
                out.append((exact, di))
        out.sort(key=lambda x: -x[0])
        return out
