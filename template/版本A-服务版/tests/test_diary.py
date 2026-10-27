# -*- coding: utf-8 -*-
"""自述/日记/导出"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import diary as D  # noqa: E402
import vault as V  # noqa: E402

SCHEMA = os.path.join(ROOT, "sql", "schema.sql")


class TestDiary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "metadata.db")
        con = sqlite3.connect(self.db)
        con.executescript(open(SCHEMA, encoding="utf-8").read())
        con.commit()
        con.close()

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def test_write_diary(self):
        r = D.write_diary("今天把记忆系统跑通了", day="2026-10-27", db=self.db)
        self.assertTrue(os.path.exists(r["file"]))
        text = D.read_diary("2026-10-27", db=self.db)
        self.assertIn("跑通", text)
        self.assertEqual(D.list_diary(db=self.db), ["2026-10-27"])

    def test_write_self(self):
        D.write_self("我是一个会记住事情的 agent", day="2026-10-27", db=self.db)
        self.assertIn("会记住", D.read_self(db=self.db))

    def test_export_vault(self):
        D.write_diary("日记内容", day="2026-10-27", db=self.db)
        D.write_self("自述内容", day="2026-10-27", db=self.db)
        out = V.export(db=self.db)
        self.assertTrue(os.path.exists(os.path.join(out["out"], "index.md")))
        self.assertTrue(os.path.exists(os.path.join(out["out"], "自述.md")))
        self.assertTrue(os.path.isdir(os.path.join(out["out"], "日记")))
        self.assertTrue(os.path.isdir(os.path.join(out["out"], "memories")))

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            D.write_diary("  ", db=self.db)
        with self.assertRaises(ValueError):
            D.write_self("  ", db=self.db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
