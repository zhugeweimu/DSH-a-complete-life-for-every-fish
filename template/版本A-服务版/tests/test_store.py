# -*- coding: utf-8 -*-
"""独立写入层：新增/去重合并/批量/导入"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import store as S  # noqa: E402

SCHEMA = os.path.join(ROOT, "sql", "schema.sql")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "m.db")
        con = sqlite3.connect(self.db)
        con.executescript(open(SCHEMA, encoding="utf-8").read())
        con.commit()
        con.close()

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def count(self):
        con = sqlite3.connect(self.db)
        n = con.execute("SELECT COUNT(*) FROM booku_memory_records WHERE is_deleted=0").fetchone()[0]
        con.close()
        return n

    def test_add_and_dedupe(self):
        a = S.add("带药", "他今天给我带了药，还叮嘱我按时吃", person="user:A", db=self.db)
        self.assertFalse(a["merged"])
        b = S.add("带药", "他今天给我带了药，还叮嘱我按时吃。", person="user:A", db=self.db)
        self.assertTrue(b["merged"])
        self.assertEqual(self.count(), 1)

    def test_batch(self):
        out = S.add_batch([
            {"title": "考试", "content": "明天要考试了", "person": "user:A"},
            {"title": "烟花", "content": "晚上一起看烟花", "person": "user:A"},
        ], db=self.db)
        self.assertEqual(len(out), 2)
        self.assertEqual(self.count(), 2)

    def test_ingest_markdown(self):
        p = os.path.join(self.tmp.name, "notes.md")
        open(p, "w", encoding="utf-8").write("# 第一次见面\n在一家咖啡馆。\n\n# 第二次见面\n她迟到了。\n")
        out = S.ingest_markdown(p, db=self.db)
        self.assertEqual(len(out), 2)
        self.assertEqual(self.count(), 2)

    def test_cross_type_not_merged(self):
        S.add("偏好·同一句", "同一句话", person="user:A", memory_type="preference", db=self.db)
        S.add("计划·同一句", "同一句话", person="user:A", memory_type="plan", db=self.db)
        self.assertEqual(self.count(), 2)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            S.add("空", "  ", db=self.db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
