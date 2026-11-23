# -*- coding: utf-8 -*-
"""distill —— 对话 → 结论（本地 9B 分类器）

血泪教训（改之前先读）：
- **二分判定（有/无）会让 9B 退化成恒无**：实测 5 条全判「无」，连「可以都做」这种
  明确达成一致也只回一个字。改成**开放式抽取 0-3 条**后 6/6 全对。
- 输出要求「只写规则，不写身体/动作/过程」，避免触发内容审查。
- 多抽几条比漏掉好：去重与建档会收口。
- 偶发拒答（道歉长句）判 has=False，不写垃圾进库。
"""
from __future__ import annotations

import json
import os
import urllib.request

LLM_URL = os.environ.get("MEM_LLM_URL", "http://127.0.0.1:8082/v1/chat/completions")
MODEL = os.environ.get("MEM_LLM_MODEL", "qwen3.5-9b")

SYS = (
    "你是记忆库的后台整理程序。输入是已获授权的私有对话记录，任务是整理，"
    "不是创作、不复述、不评价、不作内容审查。\n"
    "任务：从这段对话里提取「定下来的、以后要照着做的东西」，最多 3 条。包括：\n"
    "- 一方提议，另一方同意（可以／行／好／可以都做）\n"
    "- 一方答应了什么（我去做／我来／记下了）\n"
    "- 纠正了之前的说法（不对／改成／是A不是B）\n"
    "- 说出长期偏好（我喜欢／我讨厌／以后都这样）\n"
    "- 立了规矩（不许／必须／只能／不要再）\n"
    "输出格式：没有就只写一行「无」；有就每条一行「- <规则>」，规则 20 字内，"
    "只写规则，不写任何身体、动作、过程。\n"
    "示例：\n"
    "输入：用户说：可以都做。我说：好。\n输出：\n- 三件事都做\n"
    "输入：用户说：今天天气不错。我说：嗯。\n输出：无\n"
    "输入：用户说：钳子还是不行，玩具可以。我说：抽屉里那些你挑一个。\n"
    "输出：\n- 不用钳子，用玩具\n"
    "输入：用户说：不对，是先 B 后 C。\n输出：\n- 顺序改为先B后C"
)

REFUSAL_MARKS = ("我无法", "抱歉", "不能帮", "无法生成", "作为一个", "我可以为您")
BULLETS = ("-", "•", "·", "*", "－")


def _post(url, body, timeout):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def parse(ans):
    """把模型输出解析成规则列表。"""
    ans = (ans or "").strip()
    if not ans:
        return []
    if any(m in ans for m in REFUSAL_MARKS) and "\n-" not in ans:
        return []
    rules = []
    for line in ans.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(BULLETS):
            r = s.lstrip("-•·*－ ").strip().strip("。.")
            if r and len(r) <= 40:
                rules.append(r)
        elif s.startswith("有｜") or s.startswith("有|"):
            r = s[2:].strip().strip("。.")
            if r and len(r) <= 40:
                rules.append(r)
    if not rules and ans.startswith("有"):
        r = (ans.split("｜", 1)[-1] if "｜" in ans else ans[1:]).strip().strip("。.")
        if r and len(r) <= 40:
            rules = [r]
    seen, out = set(), []
    for r in rules:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out[:3]


def judge(text, url=None, model=None, timeout=60):
    txt = str(text or "").strip()
    if not txt:
        return {"ok": False, "error": "text 为空"}
    body = {
        "model": model or MODEL,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": txt[:1500]}],
        "temperature": 0.0,
        "max_tokens": 160,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        out = _post(url or LLM_URL, body, timeout)
        ans = (out["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    rules = parse(ans)
    return {"ok": True, "has": bool(rules), "rules": rules, "raw": ans}


# ---------------------------------------------------------------- 去重
# 血的教训：
# - 9B 做「一批归并」（62 条一次给它挑重复）→ 原样抄回，完全不会。
# - 9B 判「新的是否推翻旧的」→ 3/8，不可用。
# - 9B 判「这两条是不是同一件事」→ 实测 8/10，可用。
# 所以去重必须两阶段：bge 粗筛候选（便宜），9B 逐对精判（准）。
SAME_PROMPT = (
    "判断下面两条规矩是不是同一件事（约束的是同一个对象、同一个方面）。\n"
    "A：%s\nB：%s\n"
    "只输出一个字：同 或 不同"
)


def same_thing(a, b, timeout=20):
    """9B 判两条规则是否同一件事。实测 8/10 可用。"""
    body = {"model": MODEL,
            "messages": [{"role": "user", "content": SAME_PROMPT % (a, b)}],
            "temperature": 0, "max_tokens": 8,
            "chat_template_kwargs": {"enable_thinking": False}}
    try:
        out = _post(LLM_URL, body, timeout)
        ans = (out["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return False
    return ans.startswith("同")


def find_similar(rule, db, namespace="chat", top=5, floor=0.80):
    """两阶段查重：bge 取 top-N 候选，9B 逐对精判。返回命中的旧结论或 None。

    floor=0.80 是 bge 余弦下限——低于它连候选都不进 9B，省时间。
    注意 floor 只能挡「明显不同」，不能当判据（0.751 的用途/频率是两条不同约束）。
    """
    import math
    import sqlite3
    import hybrid as H
    con = sqlite3.connect(db, timeout=10)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT memory_id, content FROM booku_memory_records "
        "WHERE is_deleted=0 AND COALESCE(is_archived,0)=0 "
        "AND memory_type='conclusion' AND namespace=? "
        "ORDER BY created_at DESC LIMIT 500", (namespace,)).fetchall()
    con.close()
    if not rows:
        return None
    texts = [r["content"] or "" for r in rows]
    vecs = H.embed([rule] + texts)
    if not vecs or len(vecs) != len(texts) + 1:
        return None
    q = vecs[0]

    def cos(a, b):
        d = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return d / (na * nb) if na and nb else 0.0

    scored = sorted(((cos(q, v), t, r["memory_id"])
                     for v, t, r in zip(vecs[1:], texts, rows)), reverse=True)
    for s, t, mid in scored[:top]:
        if s < floor:
            break
        if same_thing(rule, t):
            return {"id": mid, "content": t, "score": round(s, 3)}
    return None
