# -*- coding: utf-8 -*-
"""store_cli —— 独立写入命令行（不依赖任何平台）

用法：
  python src/store_cli.py add --title 带药 --content 他给我带了药 --person user:A --tag 关心
  python src/store_cli.py add --title 考试 --content 明天考试 --importance 0.6
  python src/store_cli.py batch data/memories.jsonl
  python src/store_cli.py ingest notes.md
  python src/store_cli.py list --person user:A
  python src/store_cli.py show m1234567890
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import store as S  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="memory-core 写入命令行")
    ap.add_argument("--db", default=None, help="记忆库路径（默认取 MEM_DB）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="写入一条")
    a.add_argument("--title", default="")
    a.add_argument("--content", required=True)
    a.add_argument("--person", default="")
    a.add_argument("--tag", action="append", default=[])
    a.add_argument("--importance", type=float, default=None)
    a.add_argument("--folder", default="")
    a.add_argument("--type", dest="mtype", default="event")
    a.add_argument("--ts", type=float, default=None)

    b = sub.add_parser("batch", help="从 JSONL 批量写入")
    b.add_argument("file")

    sub.add_parser("sweep", help="关闭空闲超过 30 分钟的会话，生成事件")
    pl = sub.add_parser("prune-logs", help="清理已结算的旧日志行（不影响事件）")
    pl.add_argument("--days", type=int, default=180)

    cl = sub.add_parser("cleanup", help="夜间巩固：软删除超期的普通事件")
    cl.add_argument("--days", type=int, default=30)
    cl.add_argument("--min-weight", type=float, default=0.35)

    cx = sub.add_parser("context", help="生成可注入的上下文（不自动注入）")
    cx.add_argument("--q", default="")
    cx.add_argument("--person", default="")
    cx.add_argument("--top", type=int, default=5)
    cx.add_argument("--max-chars", type=int, default=1200)

    lg = sub.add_parser("log", help="按对话轮追加日志，跨 30 分钟自动生成会话事件（JSONL）")
    lg.add_argument("file")

    tu = sub.add_parser("turns", help="按对话轮自动抽取并写入（JSONL: user/reply/person/ts）")
    tu.add_argument("file")

    i = sub.add_parser("ingest", help="从 Markdown 按标题切分导入")
    i.add_argument("file")

    l = sub.add_parser("list", help="列出最近记忆")
    l.add_argument("--person", default="")
    l.add_argument("--limit", type=int, default=10)

    s = sub.add_parser("show", help="查看一条")
    s.add_argument("id")

    args = ap.parse_args()
    db = args.db

    if args.cmd == "add":
        r = S.add(args.title, args.content, person=args.person, tags=args.tag,
                  importance=args.importance, folder=args.folder, memory_type=args.mtype,
                  ts=args.ts, db=db)
        print(json.dumps(r, ensure_ascii=False))
        return 0

    if args.cmd == "batch":
        out = S.ingest_jsonl(args.file, db=db)
        print(json.dumps({"written": len(out), "items": out}, ensure_ascii=False))
        return 0

    if args.cmd == "context":
        import context as CX
        r = CX.build(args.person, args.q, args.top, args.max_chars, db=db)
        print(r["text"])
        print(json.dumps(r["items"], ensure_ascii=False))
        return 0

    if args.cmd == "prune-logs":
        import turnlog as TL
        print(json.dumps(TL.prune_logs(args.days, db=db), ensure_ascii=False))
        return 0

    if args.cmd == "cleanup":
        import consolidate as CO
        print(json.dumps(CO.run(db=db, archive_days=args.days,
                                min_weight=args.min_weight), ensure_ascii=False))
        return 0

    if args.cmd == "sweep":
        import turnlog as TL
        print(json.dumps(TL.sweep(db=db), ensure_ascii=False))
        return 0

    if args.cmd == "log":
        import turnlog as TL
        out = []
        for line in open(args.file, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append(TL.append(d.get("person") or "", d.get("user") or "",
                                 d.get("reply") or "", d.get("ts"), db=db))
        print(json.dumps(out, ensure_ascii=False, indent=1))
        print(json.dumps({"sessions": TL.pending_sessions(db=db)}, ensure_ascii=False))
        return 0

    if args.cmd == "turns":
        import autocapture as AC
        written, skipped = 0, 0
        for line in open(args.file, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            r = AC.capture_turn(d.get("user") or "", d.get("reply") or "",
                                d.get("person") or "", d.get("ts"), db=db)
            written += len(r["stored"])
            skipped += 1 if r["skipped"] else 0
            print(json.dumps(r, ensure_ascii=False))
        print(json.dumps({"written": written, "skipped_turns": skipped}, ensure_ascii=False))
        return 0

    if args.cmd == "ingest":
        out = S.ingest_markdown(args.file, db=db)
        print(json.dumps({"written": len(out), "items": out}, ensure_ascii=False))
        return 0

    con = sqlite3.connect(S._db(db))
    con.row_factory = sqlite3.Row
    if args.cmd == "list":
        q = ("SELECT memory_id,title,person_id,importance,created_at FROM booku_memory_records "
             "WHERE is_deleted=0")
        params = []
        if args.person:
            q += " AND person_id=?"
            params.append(args.person)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(args.limit)
        for r in con.execute(q, params):
            print("%s  %-20s %-10s imp=%.2f" % (r["memory_id"], r["title"], r["person_id"] or "-",
                                                r["importance"]))
        con.close()
        return 0

    r = con.execute("SELECT * FROM booku_memory_records WHERE memory_id=?", (args.id,)).fetchone()
    con.close()
    if not r:
        print("未找到", file=sys.stderr)
        return 1
    print(json.dumps(dict(r), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
