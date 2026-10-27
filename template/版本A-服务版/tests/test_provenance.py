# -*- coding: utf-8 -*-
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


class TestProvenance(unittest.TestCase):
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

    def test_write_and_read(self):
        r = S.add("测试", "这是一条测试", origin="model", author="mu",
                  source_event="turn-1", version=2, db=self.db)
        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT origin,author,source_event,version,created_at FROM booku_memory_records WHERE memory_id=?",
            (r["id"],)).fetchone()
        con.close()
        self.assertEqual(row[0], "model")
        self.assertEqual(row[1], "mu")
        self.assertEqual(row[2], "turn-1")
        self.assertEqual(row[3], 2)
        self.assertGreater(row[4], 0)

    def test_migrate_legacy(self):
        # 模拟旧库：先删掉来源列
        con = sqlite3.connect(self.db)
        con.execute("DROP TABLE booku_memory_records")
        con.execute("""CREATE TABLE booku_memory_records (
            memory_id TEXT PRIMARY KEY, title TEXT, content TEXT, person_id TEXT,
            folder_id TEXT, memory_type TEXT, importance REAL, created_at REAL,
            updated_at REAL, last_activated_at REAL, activation_count INTEGER,
            is_deleted INTEGER)""")
        con.execute("INSERT INTO booku_memory_records VALUES ('old','旧','内容','','','event',0.5,1,1,1,0,0)")
        con.commit()
        con.close()
        S.add("新", "新内容", db=self.db)
        con = sqlite3.connect(self.db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(booku_memory_records)")}
        old = con.execute("SELECT origin FROM booku_memory_records WHERE memory_id='old'").fetchone()[0]
        con.close()
        self.assertIn("origin", cols)
        self.assertEqual(old, "legacy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
