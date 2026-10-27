# -*- coding: utf-8 -*-
"""图谱：建表、加关系、查关系"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)


class TestGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["GRAPH_DIR"] = self.tmp.name
        import graph_server as G
        self.G = G
        G.DATA_DIR = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_relation_roundtrip(self):
        G = self.G
        uid = G.upsert_entity("test:u1", "测试")
        G.upsert_entity("test:u2", "对方")
        G.add_relation("test:u1", "照顾", "test:u2", note="带药", ts=1000)
        rels = G.current_relations("test:u1")
        self.assertTrue(rels)
        self.assertEqual(rels[0]["predicate"], "照顾")


if __name__ == "__main__":
    unittest.main(verbosity=2)
