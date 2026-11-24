# -*- coding: utf-8 -*-
"""目录层（/catalog）验收：真打 8095，不 mock。

跑法：python tests/test_catalog.py
服务没起来时用 workspace 的 _restart8095.py 重启一次（不是新起常驻服务）。
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("MEM_BASE", "http://127.0.0.1:8095")
WS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RESTART = os.path.join(WS, "_restart8095.py")
PERSON = os.environ.get("MEM_PERSON", "")


def _u(**kw):
    """空值参数不传，让服务端用自己的默认。"""
    return {k: v for k, v in kw.items() if v not in ("", None)}


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ready():
    try:
        return bool(get("/health", 3).get("ok"))
    except Exception:
        return False


def ensure_up():
    if ready():
        return False
    subprocess.run([sys.executable, RESTART], cwd=WS, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(40):
        time.sleep(0.5)
        if ready():
            return True
    raise SystemExit("8095 没起来")


def main():
    started = ensure_up()
    health = get("/health")
    cat = health.get("catalog") or {}
    assert cat.get("events", 0) > 0, health
    assert cat.get("blocks", 0) > 0, health
    print("[health] catalog=%s restarted=%s" % (json.dumps(cat, ensure_ascii=False), started))

    lst = get("/catalog?" + urllib.parse.urlencode(_u(person=PERSON, limit=20)))
    items = lst.get("items") or []
    assert len(items) >= 10, "目录条数 %d < 10" % len(items)
    fields = {"id", "kind", "title", "one_line", "start_ts", "end_ts", "n_items"}
    for it in items:
        missing = fields - set(it)
        assert not missing, "缺字段 %s: %s" % (missing, it)
        assert it["kind"] in ("event", "block"), it
        assert it["title"].strip() and it["one_line"].strip(), it
    kinds = {k: sum(1 for i in items if i["kind"] == k) for k in ("event", "block")}
    print("[catalog] total=%d kinds=%s" % (len(items), kinds))
    print("[catalog] first=%s | %s" % (items[0]["id"], items[0]["title"]))

    ev = next((i for i in items if i["kind"] == "event"), items[0])
    det = get("/catalog/" + urllib.parse.quote(ev["id"]))
    assert det.get("id") == ev["id"], det
    assert det.get("title"), det
    assert det.get("items"), "详情没有 items: %s" % ev["id"]
    for line in det["items"]:
        assert set(("record_id", "one_line", "ts")) <= set(line), line
        assert line["record_id"] and line["one_line"], line
    print("[detail] %s items=%d first=%s" % (det["id"], len(det["items"]),
                                             det["items"][0]["record_id"]))

    blk = next((i for i in items if i["kind"] == "block"), None)
    if blk:
        bdet = get("/catalog/" + urllib.parse.quote(blk["id"]))
        assert bdet.get("items"), "块详情没有 items: %s" % blk["id"]
        print("[detail block] %s items=%d" % (bdet["id"], len(bdet["items"])))

    for n in (2, 3):
        br = get("/catalog?" + urllib.parse.urlencode(
            _u(person=PERSON, limit=20, brief=1, n=n, max_chars=400)))
        text = br.get("text") or ""
        assert text.startswith("[目录]"), text
        assert len(text) <= 400, "目录段 %d 字 > 400" % len(text)
        assert len(text.split("\n")) <= n + 1, "条数超过 %d: %s" % (n, text)
        print("[brief n=%d] %d 字" % (n, len(text)))

    q = "这两天为什么反复改注入"
    qr = get("/catalog?" + urllib.parse.urlencode(
        _u(person=PERSON, limit=8, q=q, brief=1, n=3, max_chars=400)))
    hit = [i for i in (qr.get("items") or []) if "注入" in i["title"] or "注入" in i["one_line"]]
    assert hit, "按「%s」没找到注入相关的事: %s" % (
        q, [i["title"][:24] for i in (qr.get("items") or [])])
    print("[search] %s -> %s" % (q, [i["id"] for i in hit[:3]]))
    print("[search brief] %d 字\n%s" % (len(qr.get("text") or ""), qr.get("text")))
    print("OK")


if __name__ == "__main__":
    main()
