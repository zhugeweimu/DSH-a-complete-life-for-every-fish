# -*- coding: utf-8 -*-
"""vault —— 把记忆导出成“用户能直接翻的文件夹”

默认位置：<记忆库目录>/vault/
  index.md              全部记忆的索引（按日期）
  memories/YYYY-MM-DD/  每条记忆一个 .md（frontmatter 带 id/时间/类型）
  日记/                 从 diary 目录镜像过来
  自述.md               从 diary/self.md 镜像过来
"""
from __future__ import annotations

import os
import re
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import hybrid as H  # noqa: E402


def _db(db=None):
    return db or os.environ.get("MEM_DB") or os.path.join(HERE, "..", "data", "metadata.db")


_RESERVED = {"CON","PRN","AUX","NUL", *( "COM%d" % i for i in range(1,10)), *( "LPT%d" % i for i in range(1,10))}

def _safe(s):
    s = re.sub(r"[\x00-\x1f\x7f]", "", s or "")
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", s)[:40].strip("_. ")
    if s.upper() in _RESERVED:
        s = "_" + s
    return s or "untitled"


def export(db=None, out=None):
    db = os.path.abspath(_db(db))
    out = out or os.path.join(os.path.dirname(db), "vault")
    docs = H.load_corpus(db)
    os.makedirs(os.path.join(out, "memories"), exist_ok=True)
    index = ["# 记忆索引", "", "共 %d 条。" % len(docs), ""]
    for d in sorted(docs, key=lambda x: -(float(x.get("ts") or 0))):
        day = time.strftime("%Y-%m-%d", time.localtime(float(d.get("ts") or 0)))
        folder = os.path.join(out, "memories", day)
        os.makedirs(folder, exist_ok=True)
        name = "%s_%s.md" % (d.get("id") or "mem", _safe(d.get("title")))
        body = (
            "---\nid: %s\ntype: %s\ntime: %s\nperson: %s\nimportance: %s\ntags: %s\n---\n\n"
            "# %s\n\n%s\n" % (
                d.get("id") or "", d.get("type") or "", day,
                d.get("person") or "", d.get("importance") if d.get("importance") is not None else "",
                ",".join(d.get("tags") or []), d.get("title") or "", d.get("text") or ""))
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(body)
        index.append("- %s [%s] %s" % (day, d.get("id") or "", d.get("title") or ""))

    with open(os.path.join(out, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(index) + "\n")

    # 日记 / 自述镜像
    diary_src = os.path.join(os.path.dirname(db), "diary")
    if os.path.isdir(diary_src):
        dst = os.path.join(out, "日记")
        os.makedirs(dst, exist_ok=True)
        for fn in os.listdir(diary_src):
            if not fn.endswith(".md"):
                continue
            src = os.path.join(diary_src, fn)
            if fn == "self.md":
                shutil.copyfile(src, os.path.join(out, "自述.md"))
            else:
                shutil.copyfile(src, os.path.join(dst, fn))
    return {"out": out, "memories": len(docs)}
