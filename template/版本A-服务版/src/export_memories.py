# -*- coding: utf-8 -*-
"""导出/重放记忆库备份。

导出结构（目录 + zip，JSON 里不放图片字节）：
  <out>/memories_<tag>_<ts>/
    memories.json     数据（记录/标签；图片只留 [[media:media/xxx]] 引用）
    media/            图片 sidecar（原文件，sha256 记在 MANIFEST）
    MANIFEST.json     文件清单 + sha256 + 条数 + 重放命令
  <out>/memories_<tag>_<ts>.zip   以上目录的整包

用法：
  python src/export_memories.py                        # 默认路径，shared
  python src/export_memories.py --out D:\\备份          # 指定目录
  python src/export_memories.py --include-private      # 需 MEM_PRIVATE_TOKEN
  python src/export_memories.py --import D:\\备份\\memories_x\\   # 重放（可指定 --db）
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store as ST  # noqa: E402

DEFAULT_DB = os.environ.get("MEM_DB") or os.path.join(HERE, "..", "..", "mu-memory",
                                                      "data", "booku_memory", "metadata.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "data", "backup"))
    ap.add_argument("--include-private", action="store_true")
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--tag", default="cli")
    ap.add_argument("--import", dest="import_path", default="",
                    help="重放：备份目录或 memories.json（保留 memory_id 与 tags）")
    a = ap.parse_args()

    db = os.path.abspath(a.db)
    if a.import_path:
        if not os.path.exists(db):
            print("DB 不存在：%s（重放可以先给一个空库或已存在的库）" % db)
            return 2
        r = ST.import_export(a.import_path, db=db)
        print("imported records=%d media=%d db=%s" % (r["records"], r["media"], r["db"]))
        return 0

    if a.include_private and not os.environ.get("MEM_PRIVATE_TOKEN"):
        print("拒绝：--include-private 需要环境变量 MEM_PRIVATE_TOKEN（fail-closed）")
        return 2
    if not os.path.exists(db):
        print("DB 不存在：%s" % db)
        return 2
    r = ST.export(db=db, out_dir=os.path.abspath(a.out), include_private=a.include_private,
                  zip_it=not a.no_zip, tag=a.tag)
    print("dir=%s" % os.path.dirname(r["json"]))
    print("json=%s" % r["json"])
    print("media_dir=%s" % r["media_dir"])
    print("manifest=%s" % r["manifest"])
    print("zip=%s" % r["zip"])
    print("counts=%s" % r["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
