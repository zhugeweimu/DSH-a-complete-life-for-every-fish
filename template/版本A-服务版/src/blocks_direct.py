"""块层直读回落：8099 没起的时候，直接从 blocks.db 取块。

为什么要有这个：`context.py::_blocks()` 走 blockd 的 /compose，8099 一停块就整段消失，
块层就变成"只写不读"——2026-11-24 的复盘里就卡在这一点：块必须真的被读。
这里不走 HTTP、不起服务，只读 sqlite，用字面 BM25 粗排（块只有几十条，够用）。
"""

import math
import os
import re
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
BLOCKS_DB = os.environ.get("MEM_BLOCKS_DB") or os.path.normpath(
    os.path.join(_HERE, "..", "..", "blocks", "blocks.db"))

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tok(text):
    """中文按 2-gram、英文数字按整词切，够 BM25 用，不引第三方分词。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(text or "")).lower()
    out = []
    for w in s.split():
        if _CJK.match(w):
            out.extend(w[i:i + 2] for i in range(max(1, len(w) - 1)))
        else:
            out.append(w)
    return out


def _one_line(text, limit=90):
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    cut = max(s.rfind(c, 0, limit) for c in "。！？…；")
    return s[:cut + 1] if cut > 30 else s[:limit]


def load(agent="", limit=300):
    """读块：返回 [{id,title,summary,state,project,agent,start_ts,end_ts}]，不存在的库返回空。"""
    if not os.path.exists(BLOCKS_DB):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % BLOCKS_DB.replace("\\", "/"), uri=True, timeout=2)
        con.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        sql = "SELECT id,title,summary,state,project,agent,start_ts,end_ts FROM blocks"
        args = []
        if agent:
            sql += " WHERE agent=?"
            args.append(agent)
        sql += " ORDER BY end_ts DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in con.execute(sql, args)]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def search(q, top=3, agent=""):
    """字面 BM25（k1=1.5, b=0.75）返回 [(score, block)]；q 为空返回空。"""
    qs = _tok(q)
    if not qs:
        return []
    rows = load(agent=agent)
    if not rows:
        return []
    docs = []
    for r in rows:
        text = "%s %s" % (r.get("title") or "", r.get("summary") or "")
        docs.append((_tok(text), r))
    avg = sum(len(d) for d, _ in docs) / float(len(docs) or 1) or 1.0
    df = {}
    for toks, _ in docs:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    out = []
    for toks, r in docs:
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        for t in qs:
            if t not in tf:
                continue
            idf = math.log(1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))
            s += idf * (tf[t] * 2.5) / (tf[t] + 1.5 * (0.25 + 0.75 * len(toks) / avg))
        if s > 0:
            out.append((s, r))
    out.sort(key=lambda x: -x[0])
    return out[:top]


def compose_text(q, max_chars=560, agent=""):
    """给 _blocks() 的回落入口：命中几条块就拼成「标题：一句话」，超预算就少给一条。"""
    hits = search(q, top=4, agent=agent)
    if not hits:
        return ""
    lines = []
    used = 0
    for s, r in hits:
        line = "· %s：%s" % ((r.get("title") or "").strip(), _one_line(r.get("summary")))
        if used + len(line) > max_chars and lines:
            break
        lines.append(line)
        used += len(line)
        if used > max_chars:
            break
    return "\n".join(lines)
