# -*- coding: utf-8 -*-
"""diary —— 自述与日记（最初版：纯追加，不做总结）

设计刻意保持简单：
  · 日记：一天一个文件，按行追加；同时写入记忆库（type=diary）
  · 自述：一个文件（self.md），按日期追加；同时写入记忆库（type=self）
  · 只追加、不覆盖；不做 LLM 总结、不做人格成长
"""
from __future__ import annotations

import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import store as S  # noqa: E402


def _root(db=None):
    path = os.path.abspath(db or os.environ.get("MEM_DB") or os.path.join(HERE, "..", "data", "metadata.db"))
    return os.path.join(os.path.dirname(path), "diary")


def _day(day=None):
    return day or time.strftime("%Y-%m-%d")


def write_diary(text, day=None, person="", db=None, ts=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("日记内容不能为空")
    day = _day(day)
    d = _root(db)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, day + ".md")
    line = "- %s %s\n" % (time.strftime("%H:%M", time.localtime(ts or time.time())), text.replace("\n", " "))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    r = S.add("日记·%s" % day, text, person=person, tags=["日记"],
              importance=0.9, memory_type="diary", ts=ts, db=db)
    return {"file": path, "id": r["id"], "merged": r["merged"]}


def write_self(text, day=None, person="", db=None, ts=None):
    text = (text or "").strip()
    if not text:
        raise ValueError("自述内容不能为空")
    day = _day(day)
    d = _root(db)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "self.md")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write("# 自述\n\n")
        f.write("## %s\n\n%s\n\n" % (day, text))
    r = S.add("自述·%s" % day, text, person=person, tags=["自述"],
              importance=0.9, memory_type="self", ts=ts, db=db)
    return {"file": path, "id": r["id"], "merged": r["merged"]}


def read_self(db=None, limit=8000):
    path = os.path.join(_root(db), "self.md")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()[-limit:]


def list_diary(db=None):
    d = _root(db)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-3] for f in os.listdir(d) if f.endswith(".md") and f != "self.md")


def read_diary(day, db=None):
    path = os.path.join(_root(db), _day(day) + ".md")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()
