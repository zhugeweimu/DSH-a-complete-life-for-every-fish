# -*- coding: utf-8 -*-
"""BM25：中文二元切分与检索"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
SCHEMA = os.path.join(HERE, "..", "sql", "schema.sql")
sys.path.insert(0, SRC)

import hybrid as H  # noqa: E402


class TestBM25(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "m.db")
        con = sqlite3.connect(self.db)
        con.executescript(open(SCHEMA, encoding="utf-8").read())
        for i, (title, content) in enumerate([
                ("带药", "他今天给我带了药"), ("考试", "明天要考试了很紧张"),
                ("烟花", "晚上一起看烟花")]):
            con.execute(
                "INSERT INTO booku_memory_records (memory_id,title,content,created_at,is_deleted)"
                " VALUES (?,?,?,?,0)", ("m%d" % i, title, content, float(i)))
        con.commit()
        con.close()

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def test_tokens(self):
        self.assertIn("带药", H.tokens("带药"))

    def test_search(self):
        docs = H.load_corpus(self.db)
        bm = H.BM25(docs)
        res = bm.search("带药", top=3)
        self.assertTrue(res)
        score, idx = res[0]
        self.assertEqual(docs[idx]["id"], "m0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
