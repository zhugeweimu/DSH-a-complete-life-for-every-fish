# -*- coding: utf-8 -*-
"""schema 可建库、可插入、可查询"""
import os
import sqlite3
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "..", "sql", "schema.sql")


class TestSchema(unittest.TestCase):
    def test_apply_and_insert(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = os.path.join(d, "m.db")
            con = sqlite3.connect(db)
            con.executescript(open(SCHEMA, encoding="utf-8").read())
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("booku_memory_records", tables)
            self.assertIn("booku_memory_tags", tables)
            con.execute(
                "INSERT INTO booku_memory_records (memory_id,title,content,created_at,is_deleted)"
                " VALUES ('m1','带药','他给我带了药',1.0,0)")
            n = con.execute("SELECT COUNT(*) FROM booku_memory_records").fetchone()[0]
            self.assertEqual(n, 1)
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
