# -*- coding: utf-8 -*-
"""统一启动器：按需拉起检索 / 图谱 / 记忆内核

用法：
  python run.py --all                 # 检索 8096 + 图谱 8094 + 内核 8095
  python run.py --core                # 只起内核
  python run.py --retrieval --graph   # 只起检索和图谱
  python run.py --init-db             # 建库（schema.sql）

环境变量与 config.example.json 同名。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
PY = sys.executable


def _env(name, default):
    return os.environ.get(name, default)


def init_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(open(os.path.join(HERE, "sql", "schema.sql"), encoding="utf-8").read())
    con.commit()
    con.close()
    print("[init] db ready:", path)


def spawn(script, port, cwd=HERE):
    port = int(port)
    cmd = [PY, "-X", "utf8", os.path.join(SRC, script), str(port)]
    print("[run] %s :%d" % (script, port))
    return subprocess.Popen(cmd, cwd=cwd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", action="store_true")
    ap.add_argument("--retrieval", action="store_true")
    ap.add_argument("--graph", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--init-db", action="store_true")
    ap.add_argument("--db", default=os.path.join(HERE, "data", "metadata.db"))
    args = ap.parse_args()

    db = _env("MEM_DB", args.db)
    os.environ["MEM_DB"] = db

    if args.init_db:
        init_db(db)
        return 0

    if not os.path.exists(db):
        print("[warn] 记忆库不存在：%s（先运行 --init-db）" % db, file=sys.stderr)

    procs = []
    if args.all or args.retrieval:
        procs.append(spawn("retrieval_server.py", _env("PORT_RETRIEVAL", "8096")))
    if args.all or args.graph:
        procs.append(spawn("graph_server.py", _env("PORT_GRAPH", "8094")))
    if args.all or args.core:
        procs.append(spawn("memory_core.py", _env("PORT_CORE", "8095")))
    if not procs:
        ap.print_help()
        return 1

    print("[run] all started, Ctrl+C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
