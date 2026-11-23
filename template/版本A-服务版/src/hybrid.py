# -*- coding: utf-8 -*-
"""hybrid.py —— 三件套检索：字面(BM25) + 语义(向量) + 挑(rerank)

设计原则（与 CE 一致）：能由代码算的，不交给模型。
  · 字面：BM25，纯本地，零调用
  · 语义：调本地嵌入服务（OpenAI 兼容 /v1/embeddings），向量缓存到磁盘
  · 融合：RRF（Reciprocal Rank Fusion），不依赖两路分数量纲
  · 挑：有 RERANK_URL 就调真 reranker；没有就用确定性替代（明说"替代"）

用法：
  python hybrid.py --db <metadata.db> --q "记忆 睡觉" --top 5
  python hybrid.py --db ... --q ... --top 5 --no-dense      # 只跑字面
  python hybrid.py --db ... --q ... --explain               # 逐段打分
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import threading

EMB_URL = os.environ.get("EMB_URL", "http://127.0.0.1:8081/v1/embeddings")
EMB_MODEL = os.environ.get("EMB_MODEL", "bge-small-zh-v1.5-f16")
RERANK_URL = os.environ.get("RERANK_URL", "")          # 例：http://127.0.0.1:8082/v1/rerank
CACHE = os.environ.get("HYBRID_CACHE", "")             # 向量缓存 json
_EMB_SEM = threading.BoundedSemaphore(4)

CJK = re.compile(r"[\u4e00-\u9fff]")


# ---------------- 语料 ----------------
def load_corpus(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(booku_memory_records)")}
    opt = "".join(
        ", COALESCE(%s, %s) AS %s" % (c, d, c)
        for c, d in (("namespace", "'chat'"), ("owner", "''"),
                     ("visibility", "'shared'"), ("deletable", "1"))
        if c in cols)
    ns = ("namespace" if "namespace" in cols else "'chat' AS namespace")
    # 老库可能没有 is_archived：按列存在与否拼条件，别让整条检索因为一个列失败
    arch = "AND COALESCE(is_archived,0)=0" if "is_archived" in cols else ""
    rows = con.execute(
        "SELECT memory_id, title, content, folder_id, created_at, memory_type, person_id, "
          "COALESCE(last_activated_at,0) AS last_activated, "
          "importance, " + ns + opt + " "
        "FROM booku_memory_records WHERE is_deleted=0 " + arch).fetchall()
    tags = {}
    try:
        for r in con.execute("SELECT memory_id, tag_value FROM booku_memory_tags"):
            tags.setdefault(r["memory_id"], []).append(r["tag_value"])
    except Exception:
        pass
    docs = []
    for r in rows:
        docs.append({
            "id": r["memory_id"],
            "title": r["title"] or "",
            "text": (r["title"] or "") + "\n" + (r["content"] or ""),
            "folder": r["folder_id"] or "",
            "person": (r["person_id"] if "person_id" in r.keys() else "") or "",
            "ts": r["created_at"] or 0,
            "last_activated": r["last_activated"] or 0,
            "importance": r["importance"],
            "namespace": (r["namespace"] if "namespace" in r.keys() else "chat") or "chat",
            "type": r["memory_type"] or "",
            "owner": (r["owner"] if "owner" in r.keys() else "") or "",
            "visibility": (r["visibility"] if "visibility" in r.keys() else "shared") or "shared",
            "deletable": int(r["deletable"] if "deletable" in r.keys() else 1),
            "tags": tags.get(r["memory_id"], []),
        })
    try:
        for r in con.execute(
            "SELECT id, person, start_ts, summary, event_type, importance, weight, legacy, "
            "COALESCE(namespace,'chat') AS namespace FROM event_records WHERE is_archived=0"
        ):
            summary = r["summary"] or ""
            docs.append({
                "id": r["id"],
                "title": summary[:60],
                "text": summary,
                "folder": "事件",
                "person": r["person"] or "",
                "ts": r["start_ts"] or 0,
                "last_activated": 0,
                "importance": float(r["importance"] or r["weight"] or 0.5),
                "namespace": r["namespace"] or "chat",
                "type": r["event_type"] or "event",
                "tags": ["事件", "旧记录" if r["legacy"] else "会话"],
            })
    except Exception:
        pass
    con.close()
    return docs


# ---------------- 分词（中文二元 + 英文词） ----------------
def tokens(text):
    text = (text or "").lower()
    out = []
    out += re.findall(r"[a-z0-9_]{2,}", text)
    cjk = "".join(ch if CJK.match(ch) else " " for ch in text)
    for seg in cjk.split():
        if len(seg) == 1:
            out.append(seg)
        else:
            out += [seg[i:i + 2] for i in range(len(seg) - 1)]
    return out


# ---------------- BM25 ----------------
class BM25:
    def __init__(self, docs, k1=1.2, b=0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.df = {}
        self.tf = []
        total = 0
        for d in docs:
            toks = tokens(d["text"])
            cnt = {}
            for t in toks:
                cnt[t] = cnt.get(t, 0) + 1
            self.tf.append(cnt)
            total += len(toks)
            for t in cnt:
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(docs)
        self.avg = total / max(1, self.n)

    def search(self, q, top=50):
        qt = tokens(q)
        scores = []
        for i, d in enumerate(self.docs):
            s = 0.0
            cnt, dl = self.tf[i], sum(self.tf[i].values())
            for t in qt:
                if t not in cnt:
                    continue
                idf = math.log(1 + (self.n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                tf = cnt[t]
                s += idf * (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * dl / max(1, self.avg)))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return scores[:top]


# ---------------- 语义 ----------------
# 有界嵌入：单例线程池 + 整体截止时间 + 熔断。
# 2026-11-21 事故复盘：8081 BGE 挂掉后，每个 /recall 都在请求线程里把整库缺失向量
# embed 一遍（4 worker × 每并发请求各一份整库文本），871 个请求线程累计 42GB 提交内存，
# 把整机提交上限打满，DeepSeek Harness host 拿不到内存直接 0xE0000008 退出。
# 现在的边界：一次 embed 调用最多等 EMBED_DEADLINE 秒；端点不可达即熔断，
# 后续调用立即失败（不再每个线程各等一轮重试）。
_EMB_POOL = None
_EMB_POOL_LOCK = threading.Lock()
_EMB_DEADLINE = float(os.environ.get("EMBED_DEADLINE") or 30)
_EMB_HTTP_TIMEOUT = float(os.environ.get("EMBED_HTTP_TIMEOUT") or 15)
_EMB_CIRCUIT_SECONDS = float(os.environ.get("EMBED_CIRCUIT_SECONDS") or 30)
_EMB_FAIL = {"until": 0.0, "err": ""}


def _emb_pool():
    global _EMB_POOL
    with _EMB_POOL_LOCK:
        if _EMB_POOL is None:
            import concurrent.futures
            workers = max(1, min(8, int(os.environ.get("EMBED_WORKERS", "4"))))
            _EMB_POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="emb")
        return _EMB_POOL


def _circuit_open():
    return time.time() < _EMB_FAIL["until"]


def _embed_one(t):
    if _circuit_open():
        raise RuntimeError("embedding 熔断中（%s 不可达：%s）" % (EMB_URL, _EMB_FAIL["err"]))
    last = None
    for attempt in range(3):
        with _EMB_SEM:
            try:
                body = json.dumps({"model": EMB_MODEL, "input": (t or "")[:300]},
                                  ensure_ascii=False).encode()
                req = urllib.request.Request(EMB_URL, data=body,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=_EMB_HTTP_TIMEOUT) as r:
                    d = json.loads(r.read().decode())
                v = (d.get("data") or [{}])[0].get("embedding") or []
                if v:
                    _EMB_FAIL.update({"until": 0.0, "err": ""})
                    return v
                last = RuntimeError("empty embedding")
            except Exception as e:
                last = e
                if isinstance(e, (urllib.error.URLError, OSError, ConnectionError, socket.timeout)):
                    # 端点不可达/超时：立刻熔断，别让每个请求线程各等满一轮重试
                    _EMB_FAIL.update({"until": time.time() + _EMB_CIRCUIT_SECONDS,
                                      "err": repr(e)})
                    raise
                time.sleep(0.2 * (attempt + 1))
    raise last


def embed(texts, deadline=None):
    """返回向量列表；超时抛 TimeoutError，端点不可达抛 RuntimeError（快速失败）。"""
    texts = list(texts or [])
    if not texts:
        return []
    if _circuit_open():
        raise RuntimeError("embedding 熔断中（%s 不可达：%s）" % (EMB_URL, _EMB_FAIL["err"]))
    import concurrent.futures
    budget = float(deadline if deadline is not None else _EMB_DEADLINE)
    pool = _emb_pool()
    futs = [pool.submit(_embed_one, t) for t in texts]
    _done, pending = concurrent.futures.wait(futs, timeout=budget)
    if pending:
        for f in pending:
            f.cancel()
        raise TimeoutError("embedding 超时：%d/%d 未完成（上限 %.0fs）"
                           % (len(pending), len(futs), budget))
    return [f.result() for f in futs]


def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def dense_search(docs, q, top=50, cache_path=""):
    """优先走 FAISS：HNSW 粗筛 + 候选精确 cosine；失败只做"缓存内打分"。

    绝不在请求路径里整库 embed：旧实现会在 FAISS 不可用时把整库缺失向量补一遍，
    8081 一挂，每个请求线程各扛一份整库文本 → 内存雪崩（2026-11-21）。
    现在降级路径最多只 embed 查询本身，且必须已有磁盘向量缓存。
    """
    last_err = None
    try:
        import vector_store as VS
        return VS.search(docs, q, embed, top=max(50, int(top)))
    except Exception as e:
        last_err = e
    cache_path = cache_path or CACHE
    if not (cache_path and os.path.isfile(cache_path)):
        raise RuntimeError("向量检索不可用（%r），且没有可降级的向量缓存" % (last_err,))
    try:
        with open(cache_path, encoding="utf-8") as f:
            vecs = json.load(f)
    except Exception as e:
        raise RuntimeError("向量缓存不可读（%r）" % (e,))
    qv = embed([q])[0]                      # 只 embed 查询
    scored = []
    for i, d in enumerate(docs):
        v = vecs.get(d["id"])
        if not v:
            continue
        s = cosine(qv, v)
        if s > 0:
            scored.append((s, i))
    scored.sort(reverse=True)
    return scored[:top]


# ---------------- 融合 ----------------
def rrf(lists, k=60):
    score = {}
    for lst in lists:
        for rank, (_s, idx) in enumerate(lst):
            score[idx] = score.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score.items(), key=lambda kv: kv[1], reverse=True)


def tag_match(docs, q, top=50):
    """实体/标签信号：query 词命中 core_tags 权重 2，标题 1，正文 0.5。

    Mem0 的实体链接在 SQLite 里的最小版本：不建新表，直接用已有 tags。
    """
    import re
    toks = [t.lower() for t in re.split(r"[\s,，。;；、/|]+", q or "") if len(t) >= 2]
    if not toks:
        return []
    scored = []
    for i, d in enumerate(docs):
        tags = [str(t).lower() for t in (d.get("core_tags") or [])]
        title = (d.get("title") or "").lower()
        text = (d.get("text") or "").lower()
        s = 0.0
        for t in toks:
            if any(t in tag for tag in tags):
                s += 2.0
            elif t in title:
                s += 1.0
            elif t in text:
                s += 0.5
        if s > 0:
            scored.append((s, i))
    scored.sort(reverse=True)
    return scored[:top]


def recency_factor(ts, boost=None, half_life_days=30.0):
    """时间感知加权（Mem0 的 temporal ranking 最小版）：越新略加权，不改变硬过滤。"""
    import math
    import os
    import time
    if boost is None:
        try:
            boost = float(os.environ.get("MEM_RECENCY_BOOST", "0.1"))
        except Exception:
            boost = 0.1
    if not ts or boost <= 0:
        return 1.0
    age = max(0.0, (time.time() - float(ts)) / 86400.0)
    return 1.0 + boost * math.exp(-age / float(half_life_days))



# ---------------- 挑（rerank） ----------------
def rerank_remote(q, docs, fused, top=10):
    """真 rerank：调 /v1/rerank（llama-server --rerank 或任意同形状服务）。"""
    cand = fused[:max(top * 4, 40)]
    payload = {"model": "rerank", "query": q,
               "documents": [docs[i]["text"][:1200] for _s, i in cand]}
    req = urllib.request.Request(RERANK_URL, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    out = []
    for item in (d.get("results") or []):
        k = int(item.get("index", -1))
        if 0 <= k < len(cand):
            idx = cand[k][1]
            out.append((float(item.get("relevance_score") or 0.0), idx,
                        {"rerank": item.get("relevance_score"), "rrf": round(cand[k][0], 4)}))
    out.sort(reverse=True)
    if not out:
        raise RuntimeError("rerank 返回空")
    return out[:top]


LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")


def llm_rerank(q, docs, fused, top=10, cand=20):
    """【已禁用，默认不调用】用户要求：挑选不得使用聊天模型。

    留这段只是记录"为什么不该这么做"：
      · 聊天模型给的是"看起来相关"，不是交叉编码器的相关性分数
      · 一次调用 2–8 秒，且结果随温度/提示词漂移
      · 真正的挑必须是专门 reranker（bge-reranker 等），成对打分
    """
    """用本地聊天模型当 reranker：给定查询与候选，只要编号顺序。

    temperature=0，只解析数字；解析不到就抛异常，让上层退回替代方案。
    这不是交叉编码器，但对"哪几条真的相关"比向量余弦稳。
    """
    picks = fused[:cand]
    lines = []
    for i, (idx, _b) in enumerate(picks):
        d = docs[idx]
        lines.append("%d. %s | %s" % (i, (d["title"] or "")[:40],
                                        (d["text"] or "").replace("\n", " ")[:220]))
    prompt = ("查询：%s\n\n候选记忆：\n%s\n\n" 
              "请只输出与查询相关的候选编号，按相关度从高到低，逗号分隔（最多 %d 个）。" 
              "不相关就不要输出。不要解释。" % (q, "\n".join(lines), top))
    body = {"model": os.environ.get("LLM_MODEL", "qwen3"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "max_tokens": 80,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(LLM_URL, data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    msg = (d.get("choices") or [{}])[0].get("message", {}) or {}
    txt = msg.get("content") or msg.get("reasoning_content") or ""
    nums = [int(x) for x in re.findall(r"\d+", txt)]
    seen, out = set(), []
    for k in nums:
        if 0 <= k < len(picks) and k not in seen:
            seen.add(k)
            idx = picks[k][0]
            out.append((1.0 / (len(out) + 1), idx, {"llm_rank": len(out) + 1}))
        if len(out) >= top:
            break
    if not out:
        raise RuntimeError("llm 没给出可用编号：%r" % (txt[:40],))
    return out


def light_rerank(q, docs, fused, top=10):
    """没有 reranker 时的确定性替代：融合分 + 短语命中 + 标签命中 + 新近度。

    这不是真 rerank（没有交叉编码器），只是把明显的候选往上提。
    """
    qlow = (q or "").lower().strip()
    # 短语命中：整句命中，或任一个 >=2 字的词命中（"记忆 睡觉"这种分词查询也能算）
    qt = set(tokens(q))
    qt_words = [w for w in re.split(r"[\s,，]+", qlow) if len(w) >= 2] or [qlow]
    out = []
    base_max = max([b for _i, b in fused[:max(top * 4, 40)]] or [1.0]) or 1.0
    for idx, base in fused[:max(top * 4, 40)]:
        d = docs[idx]
        text = d["text"].lower()
        phrase = 1.0 if any(w in text for w in qt_words if w) else 0.0
        overlap = len(qt & set(tokens(d["text"]))) / max(1, len(qt))
        tag = 1.0 if any((q in (t or "")) or ((t or "") in q) for t in d["tags"] if t) else 0.0
        score = 0.40 * (base / base_max) + 0.30 * phrase + 0.20 * overlap + 0.10 * tag
        out.append((score, idx, {"base": round(base, 4), "phrase": phrase,
                                 "overlap": round(overlap, 3), "tag": tag}))
    out.sort(reverse=True)
    return out[:top]


# ---------------- 主流程 ----------------
def search(db, q, top=5, use_dense=True, explain=False):
    docs = load_corpus(db)
    if not docs:
        return {"error": "库里没有记录", "q": q}
    bm = BM25(docs)
    lex = bm.search(q, top=50)
    dense = dense_search(docs, q, top=50, cache_path=CACHE) if use_dense else []
    fused = rrf([lex, dense] if use_dense else [lex])
    if RERANK_URL:
        try:
            picked = rerank_remote(q, docs, fused, top)
            mode = "rerank-remote"
        except Exception as e:
            picked = light_rerank(q, docs, fused, top)
            mode = "light-rerank（rerank 不可用：%r）" % (e,)
    else:
        picked = light_rerank(q, docs, fused, top)
        mode = "light-rerank（替代，非交叉编码器）"
    res = []
    for sc, idx, parts in picked:
        d = docs[idx]
        res.append({"id": d["id"], "title": d["title"][:50],
                    "text": d["text"][:120], "score": round(sc, 4),
                    "why": parts if explain else None})
    return {"q": q, "mode": mode, "corpus": len(docs),
            "lex": len(lex), "dense": len(dense), "results": res}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--q", required=True)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--no-dense", action="store_true")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = search(a.db, a.q, a.top, not a.no_dense, a.explain)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    print("查询：%s（语料 %d 条，模式 %s）" % (r.get("q"), r.get("corpus", 0), r.get("mode")))
    for i, x in enumerate(r.get("results") or [], 1):
        print("  %d. [%.4f] %s" % (i, x["score"], x["title"]))
        print("     %s" % x["text"].replace("\n", " ")[:100])
        if a.explain and x.get("why"):
            print("     分项：%s" % x["why"])


if __name__ == "__main__":
    main()
