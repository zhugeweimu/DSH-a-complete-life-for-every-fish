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
import sqlite3
import sys
import time
import urllib.request

EMB_URL = os.environ.get("EMB_URL", "http://127.0.0.1:8081/v1/embeddings")
EMB_MODEL = os.environ.get("EMB_MODEL", "bge-small-zh-v1.5-f16")
RERANK_URL = os.environ.get("RERANK_URL", "")          # 例：http://127.0.0.1:8082/v1/rerank
CACHE = os.environ.get("HYBRID_CACHE", "")             # 向量缓存 json

CJK = re.compile(r"[\u4e00-\u9fff]")


# ---------------- 语料 ----------------
def load_corpus(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT memory_id, title, content, folder_id, created_at, memory_type, person_id, "
          "COALESCE(last_activated_at,0) AS last_activated, "
          "importance "
        "FROM booku_memory_records WHERE is_deleted=0").fetchall()
    tags = {}
    try:
        for r in con.execute("SELECT memory_id, tag_value FROM booku_memory_tags"):
            tags.setdefault(r["memory_id"], []).append(r["tag_value"])
    except Exception:
        pass
    con.close()
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
            "type": r["memory_type"] or "",
            "tags": tags.get(r["memory_id"], []),
        })
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
def embed(texts):
    out = []
    for t in texts:
        body = json.dumps({"model": EMB_MODEL, "input": t}, ensure_ascii=False).encode()
        req = urllib.request.Request(EMB_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        out.append((d.get("data") or [{}])[0].get("embedding") or [])
    return out


def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def dense_search(docs, q, top=50, cache_path=""):
    vecs = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                vecs = json.load(f)
        except Exception:
            vecs = {}
    missing = [d for d in docs if d["id"] not in vecs]
    if missing:
        got = embed([d["text"][:2000] for d in missing])
        for d, v in zip(missing, got):
            vecs[d["id"]] = v
        if cache_path:
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(vecs, f)
            os.replace(tmp, cache_path)
    qv = embed([q])[0]
    scored = [(cosine(qv, vecs.get(d["id"])), i) for i, d in enumerate(docs)]
    scored.sort(reverse=True)
    return scored[:top]


# ---------------- 融合 ----------------
def rrf(lists, k=60):
    score = {}
    for lst in lists:
        for rank, (_s, idx) in enumerate(lst):
            score[idx] = score.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score.items(), key=lambda kv: kv[1], reverse=True)


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
