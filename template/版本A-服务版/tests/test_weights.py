# -*- coding: utf-8 -*-
"""权重边界：衰减权重必须在 [0,1]，排序分单独归一化"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import context as CX  # noqa: E402
import memory_core as MC  # noqa: E402
import store as S  # noqa: E402

SCHEMA = os.path.join(ROOT, "sql", "schema.sql")


class TestWeightBounds(unittest.TestCase):
    def test_decay_bounds(self):
        for imp in (0.05, 0.35, 0.8, 1.0):
            for days in (0, 1, 30, 36500):
                ts = 1000.0
                w1 = MC._decay(ts - days * 86400, ts, imp)
                w2 = CX._decay(ts - days * 86400, ts, imp)
                self.assertGreaterEqual(w1, 0.0)
                self.assertLessEqual(w1, 1.0)
                self.assertGreaterEqual(w2, 0.0)
                self.assertLessEqual(w2, 1.0)

    def test_future_timestamp(self):
        self.assertLessEqual(MC._decay(2000, 2000, 0.8), 1.0)
        self.assertLessEqual(CX._decay(2000, 2000, 0.8), 1.0)

    def test_context_fields_bounded(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = os.path.join(tmp.name, "m.db")
        con = sqlite3.connect(db)
        con.executescript(open(SCHEMA, encoding="utf-8").read())
        con.commit()
        con.close()
        S.add("带药", "他今天给我带了药", person="user:A", importance=0.8, db=db)
        S.add("考试", "明天要考试", person="user:A", importance=0.5, db=db)
        r = CX.build("user:A", "带药", db=db)
        for it in r["items"]:
            self.assertGreaterEqual(it["weight"], 0.0)
            self.assertLessEqual(it["weight"], 1.0)
            self.assertGreaterEqual(it["score_norm"], 0.0)
            self.assertLessEqual(it["score_norm"], 1.0)
        try:
            tmp.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
