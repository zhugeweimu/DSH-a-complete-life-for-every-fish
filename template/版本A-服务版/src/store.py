# -*- coding: utf-8 -*-
"""store —— 独立写入层（不依赖任何平台）

三种用法：
  1) 库调用：store.add(...) / store.add_batch([...])
  2) 命令行：python src/store_cli.py add --title ... --content ...
  3) HTTP：POST /remember（memory_core 在 MEM_WRITE_MODE=local 时走这里）

写入纪律：
  · 先校验（空内容拒绝）
  · 再去重（精确指纹 + 近重 Jaccard > 0.8）
  · 重复则合并：激活 +1、更新时间、标签并集、内容取较长的
  · 只做软删除标记，不物理删
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import hybrid as H  # noqa: E402  （同目录）

DEFAULT_DB = os.environ.get("MEM_DB") or os.path.join(HERE, "..", "data", "metadata.db")


def _db(db=None):
    """解析并校验 MEM_DB：指向目录/不存在/0 字节一律 fail-fast，绝不自动建空库。"""
    path = os.path.abspath(db or os.environ.get("MEM_DB") or DEFAULT_DB)
    if os.path.isdir(path):
        raise RuntimeError("MEM_DB 指向目录而不是数据库文件：%s（0 字节默认库已停用，请显式设 MEM_DB）" % path)
    if not os.path.exists(path):
        raise RuntimeError("MEM_DB 不存在：%s（不会自动创建空库，请显式设 MEM_DB）" % path)
    if os.path.getsize(path) <= 0:
        raise RuntimeError("MEM_DB 是 0 字节空库：%s（拒绝使用，请显式设 MEM_DB）" % path)
    return path


def _now(ts=None):
    return float(ts if ts is not None else time.time())


VISIBILITIES = ("shared", "private")

# 私有分区三列：owner=归属人，visibility=shared/private，deletable=用户侧是否可删
PRIVATE_COLUMNS = (
    ("owner", "TEXT NOT NULL DEFAULT ''"),
    ("visibility", "TEXT NOT NULL DEFAULT 'shared'"),
    ("deletable", "INTEGER NOT NULL DEFAULT 1"),
)


def _ensure_provenance(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(booku_memory_records)")}
    added = False
    prov = (("origin", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("author", "TEXT NOT NULL DEFAULT ''"),
            ("source_event", "TEXT NOT NULL DEFAULT ''"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("bucket", "TEXT NOT NULL DEFAULT 'memory'"),
            ("namespace", "TEXT NOT NULL DEFAULT 'chat'"))
    for name, ddl in prov + PRIVATE_COLUMNS:
        if name not in cols:
            con.execute("ALTER TABLE booku_memory_records ADD COLUMN %s %s" % (name, ddl))
            if name == "origin":
                added = True
    if added:
        con.execute("UPDATE booku_memory_records SET origin='legacy' WHERE origin='unknown'")
    # 旧数据一律 shared；私有分区只允许显式写入 private
    con.execute("UPDATE booku_memory_records SET visibility='shared' "
                "WHERE visibility IS NULL OR visibility NOT IN ('shared','private')")
    con.execute("UPDATE booku_memory_records SET deletable=1 WHERE deletable IS NULL")
    con.commit()


def _norm_visibility(visibility):
    v = str(visibility or "shared").strip().lower()
    if v not in VISIBILITIES:
        raise ValueError("visibility 只能是 shared 或 private")
    return v


def assert_user_mutable(memory_id, actor="user", db=None):
    """用户侧不可改/不可删 private 记忆；返回行信息，违规抛 PermissionError。

    actor='self' 表示代理自己的私有工具（可带令牌），只有它才能动 private 记录。
    """
    con = sqlite3.connect(_db(db), timeout=10)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT memory_id, visibility, owner, deletable FROM booku_memory_records "
                      "WHERE memory_id=?", (memory_id,)).fetchone()
    con.close()
    if not row:
        raise KeyError("memory 不存在：%s" % memory_id)
    if (row["visibility"] or "shared") == "private" and actor != "self":
        raise PermissionError("private 记忆（owner=%s）不可由用户侧修改或删除" % (row["owner"] or ""))
    if int(row["deletable"] if row["deletable"] is not None else 1) == 0 and actor != "self":
        raise PermissionError("该记忆被标记为不可删除（deletable=0）")
    return dict(row)


def soft_delete(memory_id, actor="user", db=None):
    """软删除；private 或 deletable=0 时拒绝（actor='self' 除外）。只做 is_deleted 标记。"""
    assert_user_mutable(memory_id, actor=actor, db=db)
    con = sqlite3.connect(_db(db), timeout=10)
    try:
        con.execute("UPDATE booku_memory_records SET is_deleted=1, deleted_at=? WHERE memory_id=?",
                    (_now(), memory_id))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "id": memory_id, "deleted": True, "actor": actor}


def normalize_title(s):
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s[:80]


def fingerprint(title, content, person="", memory_type="", owner="", visibility="shared"):
    """记忆身份 = 标题|内容|人|类型|分区。分区进指纹，private 与 shared 同文不撞 id。"""
    raw = "%s|%s|%s|%s" % (normalize_title(title), (content or "").strip(), person or "", memory_type or "")
    if (visibility or "shared") != "shared" or owner:
        raw += "|%s|%s" % (visibility or "shared", owner or "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def jaccard(a, b):
    A, B = set(H.tokens(a)), set(H.tokens(b))
    return len(A & B) / max(1, len(A | B))


def _importance(title, given=None):
    """重要度启发式：0.9 日记/自述、0.15 群聊、0.5 其余（可调，见 docs/PARAMETERS.md）"""
    try:
        if given is not None and float(given) > 0:
            return max(0.05, min(1.0, float(given)))
    except Exception:
        pass
    t = title or ""
    if t.startswith("日记") or t.startswith("自述"):
        return 0.9
    if t.startswith("群聊"):
        return 0.15
    return 0.5


def find_duplicate(db, title, content, person="", memory_type="", owner="", visibility="shared"):
    """返回 (row|None, score, kind)，kind ∈ exact/near/none；不同类型不互相合并。
    分区边界不合并：owner/visibility 不同的记录各写各的（防止 shared 撞进 private）。"""
    con = sqlite3.connect(_db(db), timeout=10)
    con.row_factory = sqlite3.Row
    fp = fingerprint(title, content, person, memory_type, owner=owner, visibility=visibility)
    row = con.execute(
        "SELECT memory_id,title,content,person_id,activation_count,owner,visibility "
        "FROM booku_memory_records WHERE is_deleted=0 AND memory_id=?", (fp,)).fetchone()
    if row and not _same_partition(row, owner, visibility):
        row = None
    if row:
        con.close()
        return dict(row), 1.0, "exact"
    q = ("SELECT memory_id,title,content,person_id,activation_count,owner,visibility "
         "FROM booku_memory_records "
         "WHERE is_deleted=0 AND (person_id=? OR person_id='')")
    args = [person or ""]
    if memory_type:
        q += " AND memory_type=?"
        args.append(memory_type)
    q += " ORDER BY created_at DESC LIMIT 300"
    rows = con.execute(q, args).fetchall()
    con.close()
    best, best_s = None, 0.0
    for r in rows:
        if not _same_partition(r, owner, visibility):
            continue
        s = jaccard(title + " " + content, r["title"] + " " + r["content"])
        if s > best_s:
            best, best_s = dict(r), s
    # 个性化阈值：近重合并线 0.8，可调（见 docs/PARAMETERS.md）
    if best_s > 0.8:
        return best, best_s, "near"
    return None, best_s, "none"


def _same_partition(row, owner, visibility):
    rv = (row["visibility"] if "visibility" in row.keys() else "shared") or "shared"
    ro = (row["owner"] if "owner" in row.keys() else "") or ""
    return rv == visibility and ro == (owner or "")


def _merge(con, dup, title, content, tags, ts):
    new_content = content if len(content) > len(dup["content"] or "") else dup["content"]
    con.execute(
        "UPDATE booku_memory_records SET content=?, title=?, activation_count=activation_count+1, "
        "last_activated_at=?, updated_at=? WHERE memory_id=?",
        (new_content, normalize_title(title) or dup["title"], ts, ts, dup["memory_id"]))
    for t in tags or []:
        con.execute("INSERT INTO booku_memory_tags (memory_id,tag_type,tag_value) VALUES (?,?,?)",
                    (dup["memory_id"], "core", t))


def canon_person(person):
    p = str(person or "").strip()
    if p.startswith("qq:"):
        return "user:" + p
    return p


def _by_id(con, mid):
    """按 memory_id 取一行（含软删），不存在返回 None。"""
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT memory_id,title,content,person_id,activation_count,owner,visibility,is_deleted "
        "FROM booku_memory_records WHERE memory_id=?", (mid,)).fetchone()
    return dict(row) if row else None


def _revive(con, row, title, content, tags, ts):
    """软删的同内容记录：复活并合并，返回原 id（幂等，绝不 500）。"""
    con.execute(
        "UPDATE booku_memory_records SET is_deleted=0, deleted_at=0, updated_at=?, "
        "last_activated_at=?, activation_count=COALESCE(activation_count,0)+1, "
        "title=?, content=? WHERE memory_id=?",
        (ts, ts, normalize_title(title) or row.get("title") or "",
         content if len(content) > len(row.get("content") or "") else (row.get("content") or content),
         row["memory_id"]))
    for t in tags or []:
        con.execute("INSERT INTO booku_memory_tags (memory_id,tag_type,tag_value) VALUES (?,?,?)",
                    (row["memory_id"], "core", t))


def add(title, content, person="", tags=None, importance=None, folder="", memory_type="event",
        ts=None, db=None, origin="unknown", author="", source_event="", version=1,
        namespace="chat", owner="", visibility="shared", deletable=1):
    """写入一条；返回 {"id","merged","dup_id","score"}

    visibility=private 必须给 owner，且强制 deletable=0（用户侧不可删/不可改）。
    """
    person = canon_person(person)
    visibility = _norm_visibility(visibility)
    owner = str(owner or "").strip()
    if visibility == "private" and not owner:
        raise ValueError("private 记忆必须提供 owner")
    if visibility == "private":
        deletable = 0
    deletable = 1 if int(deletable) else 0

    title = normalize_title(title)
    content = (content or "").strip()
    if not content:
        raise ValueError("content 不能为空")
    if not title:
        title = content[:20]
    ts = _now(ts)
    con = sqlite3.connect(_db(db), timeout=10)
    try:
        _ensure_provenance(con)
        dup, score, kind = find_duplicate(db, title, content, person, memory_type,
                                          owner=owner, visibility=visibility)
        if dup:
            _merge(con, dup, title, content, tags, ts)
            con.commit()
            return {"id": dup["memory_id"], "merged": True, "dup_id": dup["memory_id"],
                    "kind": kind, "score": round(score, 3)}
        mid = fingerprint(title, content, person, memory_type, owner=owner, visibility=visibility)
        # 幂等：同内容已存在（含软删）→ 返回已有 id，不新增、不 500
        existing = _by_id(con, mid)
        if existing:
            if int(existing.get("is_deleted") or 0) != 0:
                _revive(con, existing, title, content, tags, ts)
                con.commit()
                return {"id": mid, "merged": True, "dup_id": mid, "kind": "revived",
                        "revived": True, "score": 1.0}
            _merge(con, existing, title, content, tags, ts)
            con.commit()
            return {"id": mid, "merged": True, "dup_id": mid, "kind": "exact",
                    "revived": False, "score": 1.0}
        imp = _importance(title, importance)
        vals = {
            "memory_id": mid, "title": title, "content": content,
            "person_id": person or "", "folder_id": folder or "",
            "bucket": "memory", "source": "memory_core",
            "namespace": namespace or "chat",
            "memory_type": memory_type, "status": "active",
            "importance": imp, "created_at": ts, "updated_at": ts,
            "last_activated_at": ts, "activation_count": 0,
            "is_archived": 0, "is_deleted": 0, "deleted_at": 0,
            "relation_memory_ids": "[]", "relation_aliases": "[]",
            "related_people": "[]", "event_start_at": 0, "event_end_at": 0,
            "knowledge_type": "", "address_or_coord": "", "place_type": "",
            "asset_type": "", "disposition_status": "", "procedure_type": "",
            "novelty_energy": 0, "origin": origin or "unknown",
            "author": author or "", "source_event": source_event or "",
            "version": int(version or 1),
            "owner": owner, "visibility": visibility, "deletable": deletable,
        }
        cols = [r[1] for r in con.execute("PRAGMA table_info(booku_memory_records)")]
        use = [c for c in cols if c in vals]
        try:
            con.execute("INSERT INTO booku_memory_records (%s) VALUES (%s)" %
                        (",".join(use), ",".join("?" for _ in use)),
                        [vals[c] for c in use])
        except sqlite3.IntegrityError:
            # 并发/历史残留撞主键或唯一键：退回幂等路径，返回已有 id 而不是 500
            row = _by_id(con, mid)
            if row is None:
                # 唯一键兜底：同 title+content+person+namespace+分区 的活跃行
                con.row_factory = sqlite3.Row
                r2 = con.execute(
                    "SELECT memory_id,title,content,person_id,activation_count,owner,visibility,"
                    "is_deleted FROM booku_memory_records WHERE title=? AND content=? "
                    "AND COALESCE(person_id,'')=? AND COALESCE(namespace,'chat')=? "
                    "AND COALESCE(visibility,'shared')=? AND COALESCE(owner,'')=? "
                    "AND COALESCE(is_deleted,0)=0 LIMIT 1",
                    (title, content, person or "", namespace or "chat", visibility, owner)).fetchone()
                row = dict(r2) if r2 else None
                if row is None:
                    raise
                mid = row["memory_id"]
            if int(row.get("is_deleted") or 0) != 0:
                _revive(con, row, title, content, tags, ts)
            else:
                _merge(con, row, title, content, tags, ts)
            con.commit()
            return {"id": mid, "merged": True, "dup_id": mid,
                    "kind": "revived" if int(row.get("is_deleted") or 0) else "exact",
                    "revived": bool(int(row.get("is_deleted") or 0)), "score": 1.0}
        for t in tags or []:
            con.execute("INSERT INTO booku_memory_tags (memory_id,tag_type,tag_value) VALUES (?,?,?)",
                        (mid, "core", t))
        con.commit()
        return {"id": mid, "merged": False, "dup_id": None, "kind": kind, "score": round(score, 3),
                "revived": False}
    finally:
        con.close()


def add_batch(items, db=None):
    return [add(item.get("title", ""), item.get("content", ""),
                person=item.get("person", item.get("person_id", "")),
                tags=item.get("tags") or [], importance=item.get("importance"),
                folder=item.get("folder", ""), memory_type=item.get("memory_type", "event"),
                ts=item.get("ts"), db=db,
        origin=item.get("origin", "unknown"), author=item.get("author", ""),
        source_event=item.get("source_event", ""), version=item.get("version", 1),
        namespace=item.get("namespace", "chat"),
        owner=item.get("owner", ""), visibility=item.get("visibility", "shared"),
        deletable=item.get("deletable", 1)) for item in items]


def ingest_jsonl(path, db=None):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(add_batch([json.loads(line)], db=db)[0])
    return out


def ingest_markdown(path, db=None):
    """按标题切分：以 # / ## / ### 开头的行为标题，其余为内容"""
    title, buf, out = "", [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if re.match(r"^#{1,3}\s+", line):
                if title or buf:
                    out.append(add(title, "\n".join(buf), db=db))
                title = re.sub(r"^#{1,3}\s+", "", line).strip()
                buf = []
            else:
                buf.append(line.rstrip())
    if title or buf:
        out.append(add(title, "\n".join(buf), db=db))
    return out


MEDIA_URI_RE = re.compile(
    r"data:image/(png|jpe?g|gif|webp|bmp|svg\+xml);base64,([A-Za-z0-9+/=\s]+)", re.I)
MEDIA_PATH_RE = re.compile(
    r"(?:!\[[^\]]*\]\(\s*|<img[^>]+src=[\"']|\[\[media:)([^)\"'\]]+?\.(?:png|jpe?g|gif|webp|bmp|svg))",
    re.I)
_MEDIA_EXT = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg", "gif": ".gif",
              "webp": ".webp", "bmp": ".bmp", "svg": ".svg", "svg+xml": ".svg"}


def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _stash_media(media_dir, raw, ext, seen):
    """把媒体字节落成 sidecar 文件，返回相对引用 media/<name>。"""
    ext = ext if ext.startswith(".") else "." + ext
    name = hashlib.sha1(raw).hexdigest()[:16] + ext.lower()
    dst = os.path.join(media_dir, name)
    if name not in seen:
        if not os.path.exists(dst):
            with open(dst, "wb") as f:
                f.write(raw)
        seen[name] = {"file": "media/" + name, "bytes": len(raw), "sha256": _sha256_bytes(raw)}
    return "media/" + name, seen[name]


def _externalize_media(record, media_dir, seen, base_dir):
    """把记录里的图片从 JSON 里剥出来：data URI 解码落盘，本地路径拷进 sidecar。

    返回该记录的 media 引用列表；content 里的图片被替换成 [[media:media/xxx]]。
    """
    refs = []
    content = record.get("content") or ""
    if not content:
        return refs, content

    def _sub_uri(m):
        sub = m.group(1).lower()
        ext = _MEDIA_EXT.get(sub, ".png")
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
        except Exception:
            return m.group(0)
        rel, info = _stash_media(media_dir, raw, ext, seen)
        refs.append(dict(info, source="data-uri"))
        return "[[media:%s]]" % rel

    content = MEDIA_URI_RE.sub(_sub_uri, content)
    for mt in list(MEDIA_PATH_RE.finditer(content)):
        ref = (mt.group(1) or "").strip()
        if not ref or ref.startswith("media/") or ref.startswith("[[media:"):
            continue
        cand = ref if os.path.isabs(ref) else os.path.join(base_dir, ref)
        if not os.path.isfile(cand):
            continue
        ext = os.path.splitext(cand)[1].lower()
        if ext not in _MEDIA_EXT.values():
            continue
        with open(cand, "rb") as f:
            raw = f.read()
        rel, info = _stash_media(media_dir, raw, ext, seen)
        refs.append(dict(info, source="file", from_path=ref))
        content = content.replace(ref, rel)
    return refs, content


def export(db=None, out_dir="backup", include_private=False, zip_it=True, tag="manual"):
    """导出备份：memories.json（数据）+ media/（图片 sidecar）+ MANIFEST.json。

    图片不塞进 JSON：data URI 解成文件、本地图片拷成文件，JSON 里只留
    [[media:media/xxx.png]] 引用与 sha256。整包可重放（import_export）。
    include_private=False 时只导 shared；private 媒体不会进 sidecar。
    """
    import zipfile

    path = _db(db)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        _ensure_provenance(con)
        sql = "SELECT * FROM booku_memory_records"
        if not include_private:
            sql += " WHERE COALESCE(visibility,'shared')<>'private'"
        rows = [dict(r) for r in con.execute(sql)]
        ids = [r["memory_id"] for r in rows]
        tags = {}
        if ids:
            q = ("SELECT memory_id, tag_type, tag_value FROM booku_memory_tags WHERE memory_id IN (%s)"
                 % ",".join("?" for _ in ids))
            for tr in con.execute(q, ids):
                tags.setdefault(tr["memory_id"], []).append([tr["tag_type"], tr["tag_value"]])
    finally:
        con.close()

    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = os.path.join(out_dir, "memories_%s_%s" % (tag, stamp))
    os.makedirs(base, exist_ok=True)
    media_dir = os.path.join(base, "media")
    os.makedirs(media_dir, exist_ok=True)
    seen = {}
    for r in rows:
        refs, content = _externalize_media(r, media_dir, seen, os.path.dirname(os.path.abspath(path)))
        r["content"] = content
        r["media"] = refs
    payload = {
        "schema": "memory-core-export/2",
        "exported_at": time.time(),
        "db": os.path.abspath(path),
        "include_private": bool(include_private),
        "counts": {
            "records": len(rows),
            "active": sum(1 for r in rows if not int(r.get("is_deleted") or 0)),
            "private": sum(1 for r in rows if (r.get("visibility") or "shared") == "private"),
            "deleted": sum(1 for r in rows if int(r.get("is_deleted") or 0)),
            "media": len(seen),
        },
        "records": rows,
        "tags": tags,
    }
    jp = os.path.join(base, "memories.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    files = []
    for root, _dirs, names in os.walk(base):
        for n in sorted(names):
            fp = os.path.join(root, n)
            rel = os.path.relpath(fp, base).replace("\\", "/")
            with open(fp, "rb") as f:
                raw = f.read()
            files.append({"path": rel, "bytes": len(raw), "sha256": _sha256_bytes(raw)})
    manifest = {
        "schema": "memory-core-export-manifest/1",
        "exported_at": payload["exported_at"],
        "db": payload["db"],
        "counts": payload["counts"],
        "files": files,
        "replay": "python src/export_memories.py --import <本目录或 memories.json> [--db 目标库]",
    }
    mp = os.path.join(base, "MANIFEST.json")
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    out = {"json": jp, "counts": payload["counts"], "media_dir": media_dir,
           "manifest": mp, "zip": None}
    if zip_it:
        zp = base + ".zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, names in os.walk(base):
                for n in sorted(names):
                    fp = os.path.join(root, n)
                    zf.write(fp, arcname=os.path.relpath(fp, os.path.dirname(base)))
        out["zip"] = zp
    return out


def import_export(path, db=None, restore_data_uri=True):
    """重放备份：memories.json + media/ → 目标库（保留原 memory_id 与 tags）。

    返回 {"records","media","db"}；图片按需还原成 data URI（默认），
    找不到 sidecar 时保留 [[media:...]] 引用。
    """
    src = path
    if os.path.isdir(src):
        src = os.path.join(src, "memories.json")
    base = os.path.dirname(os.path.abspath(src))
    with open(src, encoding="utf-8") as f:
        payload = json.load(f)
    records = payload.get("records") or []
    tags = payload.get("tags") or {}
    con = sqlite3.connect(_db(db), timeout=10)
    try:
        _ensure_provenance(con)
        cols = [r[1] for r in con.execute("PRAGMA table_info(booku_memory_records)")]
        n_media = 0
        for r in records:
            content = r.get("content") or ""
            for ref in r.get("media") or []:
                rel = (ref.get("file") or "").replace("\\", "/")
                fp = os.path.join(base, rel)
                if not os.path.isfile(fp):
                    continue
                with open(fp, "rb") as f:
                    raw = f.read()
                ext = os.path.splitext(fp)[1].lstrip(".").lower()
                mime = "svg+xml" if ext == "svg" else ext
                if restore_data_uri:
                    uri = "data:image/%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))
                    content = content.replace("[[media:%s]]" % rel, uri)
                    content = content.replace("(%s)" % rel, "(%s)" % uri)
                n_media += 1
            row = dict(r)
            row["content"] = content
            use = [c for c in cols if c in row]
            con.execute("INSERT OR REPLACE INTO booku_memory_records (%s) VALUES (%s)" %
                        (",".join(use), ",".join("?" for _ in use)), [row[c] for c in use])
            mid = row.get("memory_id")
            if mid and mid in tags:
                con.execute("DELETE FROM booku_memory_tags WHERE memory_id=?", (mid,))
                for tt, tv in tags[mid]:
                    con.execute("INSERT INTO booku_memory_tags (memory_id,tag_type,tag_value)"
                                " VALUES (?,?,?)", (mid, tt, tv))
        con.commit()
    finally:
        con.close()
    return {"records": len(records), "media": n_media, "db": os.path.abspath(_db(db))}
