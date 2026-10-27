# -*- coding: utf-8 -*-
"""自动抽取：普通对话也能落记忆，不需要用户说“记住”"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import autocapture as AC  # noqa: E402

SCHEMA = os.path.join(ROOT, "sql", "schema.sql")


class TestAutoCapture(unittest.TestCase):
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

    def test_preference(self):
        r = AC.capture_turn("我特别喜欢吃辣，不喜欢甜的", person="user:A", ts=1000, db=self.db)
        self.assertTrue(r["stored"])
        self.assertEqual(r["stored"][0]["type"], "preference")

    def test_plan(self):
        r = AC.capture_turn("明天我要去医院复查", person="user:A", ts=1000, db=self.db)
        self.assertTrue(r["stored"])
        self.assertEqual(r["stored"][0]["type"], "plan")

    def test_fact(self):
        r = AC.capture_turn("我今年32岁，在杭州上班", person="user:A", ts=1000, db=self.db)
        self.assertTrue(any(x["type"] == "fact" for x in r["stored"]))

    def test_greeting_skipped(self):
        r = AC.capture_turn("你好", person="user:A", db=self.db)
        self.assertEqual(r["stored"], [])
        self.assertEqual(r["skipped"], "寒暄")

    def test_question_skipped(self):
        r = AC.capture_turn("今天天气怎么样？", person="user:A", db=self.db)
        self.assertEqual(r["stored"], [])

    def test_explicit_note(self):
        r = AC.capture_turn("记住，我下周三要去医院", person="user:A", db=self.db)
        types = {x["type"] for x in r["stored"]}
        self.assertIn("note", types)

    def test_dedupe_same_turn(self):
        AC.capture_turn("我喜欢喝咖啡", person="user:A", ts=1000, db=self.db)
        AC.capture_turn("我喜欢喝咖啡", person="user:A", ts=2000, db=self.db)
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
