# -*- coding: utf-8 -*-
"""夜间巩固：只软删除超期的普通事件"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import consolidate as C  # noqa: E402
import store as S  # noqa: E402

SCHEMA = os.path.join(ROOT, "sql", "schema.sql")


class TestConsolidate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "m.db")
        con = sqlite3.connect(self.db)
        con.executescript(open(SCHEMA, encoding="utf-8").read())
        con.commit()
        con.close()
        self.now = time.time()

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def state(self):
        con = sqlite3.connect(self.db)
        rows = dict(con.execute("SELECT title,is_deleted FROM booku_memory_records"))
        con.close()
        return rows

    def test_plain_archived_important_kept(self):
        S.add("普通事件", "随便聊聊", person="u", importance=0.35,
              memory_type="session:plain", ts=self.now - 40 * 86400, db=self.db)
        S.add("计划", "明天考试", person="u", importance=0.7,
              memory_type="session:plan", ts=self.now - 40 * 86400, db=self.db)
        r = C.run(db=self.db, now=self.now)
        self.assertEqual(len(r["archived"]), 1)
        st = self.state()
        self.assertEqual(st["普通事件"], 1)
        self.assertEqual(st["计划"], 0)

    def test_recent_not_archived(self):
        S.add("新事件", "今天的事", person="u", importance=0.35,
              memory_type="session:plain", ts=self.now - 86400, db=self.db)
        C.run(db=self.db, now=self.now)
        self.assertEqual(self.state()["新事件"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
