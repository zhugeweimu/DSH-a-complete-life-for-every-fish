# -*- coding: utf-8 -*-
"""导入检查：模块可独立加载，且不含平台/情绪依赖"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)


class TestImports(unittest.TestCase):
    def test_imports(self):
        import hybrid      # noqa: F401
        import reranker_onnx  # noqa: F401
        import graph_server   # noqa: F401
        import memory_core    # noqa: F401
        import retrieval_server  # noqa: F401

    def test_no_foreign_refs(self):
        for fn in os.listdir(SRC):
            if not fn.endswith(".py"):
                continue
            src = open(os.path.join(SRC, fn), encoding="utf-8").read()
            for bad in ("affect", "astrbot", "qqaibot"):
                self.assertNotIn(bad, src, fn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
