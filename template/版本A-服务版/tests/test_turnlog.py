# -*- coding: utf-8 -*-
"""日志分层：每回合日志 + 30 分钟会话分段 + 会话事件"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import turnlog as TL  # noqa: E402

SCHEMA = os.path.join(ROOT, "sql", "schema.sql")


class TestTurnLog(unittest.TestCase):
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

    def events(self):
        con = sqlite3.connect(self.db)
        rows = con.execute("SELECT memory_type,importance,title FROM booku_memory_records").fetchall()
        con.close()
        return rows

    def test_same_session_no_event(self):
        TL.append("user:A", "我喜欢喝咖啡", ts=1000, db=self.db)
        r = TL.append("user:A", "明天要考试", ts=1000 + 600, db=self.db)
        self.assertIsNone(r["closed"])
        self.assertEqual(self.events(), [])

    def test_gap_closes_to_event(self):
        TL.append("user:A", "我喜欢喝咖啡", ts=1000, db=self.db)
        TL.append("user:A", "我明天要考试", ts=1000 + 600, db=self.db)
        r = TL.append("user:A", "今天好累", ts=1000 + 600 + 1801, db=self.db)
        self.assertIsNotNone(r["closed"])
        self.assertEqual(sorted(r["closed"]["kinds"]), ["plan", "preference"])
        self.assertEqual(len(r["closed"]["event_ids"]), 2)
        ev = self.events()
        self.assertEqual(len(ev), 2)
        self.assertEqual({e[0] for e in ev}, {"session:preference", "session:plan"})
        self.assertTrue(all(e[1] >= 0.7 for e in ev))

    def test_plain_session_low_importance(self):
        TL.append("user:A", "今天天气不错", ts=1000, db=self.db)
        r = TL.append("user:A", "随便聊聊", ts=1000 + 1801, db=self.db)
        self.assertIsNotNone(r["closed"])
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0][0], "session:plain")
        self.assertAlmostEqual(ev[0][1], TL.PLAIN_IMPORTANCE, places=3)

    def test_max_session_cap(self):
        base = 1000
        r = None
        for dt, txt in ((0, "a"), (1700, "b"), (3400, "c"), (5100, "d"), (6800, "e"), (7300, "f")):
            r = TL.append("user:A", txt, ts=base + dt, db=self.db)
        self.assertIsNotNone(r["closed"])
        self.assertEqual(len(self.events()), 1)

    def test_pending_sessions(self):
        TL.append("user:A", "你好呀", ts=1000, db=self.db)
        p = TL.pending_sessions(db=self.db)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["turns"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
