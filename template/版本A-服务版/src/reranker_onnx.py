# -*- coding: utf-8 -*-
"""reranker_onnx —— 本地 BGE reranker（交叉编码器，ONNX Runtime）

为什么是它：
  embedding 是"查询、文档各自编码再算距离"；reranker 是"（查询, 文档）成对打分"。
  只有成对打分才能判断"这段能不能回答这个问题"。

模型：Xenova/bge-reranker-base（ONNX，约 1.06 GB fp32）
依赖：onnxruntime + tokenizers（numpy 已有）
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading

import numpy as np

MODEL_DIR = os.environ.get("RERANK_MODEL_DIR",
                           r"D:\AI\llm\models\reranker\bge-reranker-base")
MAX_LEN = int(os.environ.get("RERANK_MAX_LEN") or 192)

_lock = threading.Lock()
_sess = None
_tok = None


def _load():
    global _sess, _tok
    with _lock:
        if _sess is not None:
            return _sess, _tok
        import onnxruntime as ort
        from tokenizers import Tokenizer
        # 优先 int8（快约 3 倍），没有再退回 fp32
        cands = [os.path.join(MODEL_DIR, "onnx", "model_int8.onnx"),
                 os.path.join(MODEL_DIR, "onnx", "model.onnx")]
        onnx_path = next((x for x in cands if os.path.isfile(x)), cands[-1])
        tok_path = os.path.join(MODEL_DIR, "tokenizer.json")
        if not (os.path.isfile(onnx_path) and os.path.isfile(tok_path)):
            raise FileNotFoundError("reranker 模型不完整：%s" % MODEL_DIR)
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, min(8, (os.cpu_count() or 4)))
        so.log_severity_level = 3
        _sess = ort.InferenceSession(onnx_path, sess_options=so,
                                     providers=["CPUExecutionProvider"])
        _tok = Tokenizer.from_file(tok_path)
        _tok.enable_truncation(max_length=MAX_LEN)
        _tok.enable_padding(pad_id=0, pad_token="[PAD]")
        return _sess, _tok


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def score_pairs(pairs):
    """[(query, doc), ...] → [0..1] 分数。"""
    sess, tok = _load()
    encs = tok.encode_batch([(q, d) for q, d in pairs])
    feed = {
        "input_ids": np.array([e.ids for e in encs], dtype=np.int64),
        "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
    }
    names = {i.name for i in sess.get_inputs()}
    if "token_type_ids" in names:
        feed["token_type_ids"] = np.array([e.type_ids for e in encs], dtype=np.int64)
    out = sess.run(None, feed)[0]
    logits = np.array(out).reshape(len(pairs), -1)[:, 0]
    return [_sigmoid(float(x)) for x in logits]


def rerank(q, docs, fused, top=10, cand=10):
    """对融合后的候选做交叉编码器精排。返回 [(score, idx, why)]。"""
    picks = fused[:cand]
    pairs = [(q, ((docs[i]["title"] or "") + "\n" + (docs[i]["text"] or ""))[:500])
             for i, _b in picks]
    scores = score_pairs(pairs)
    out = [(s, picks[k][0], {"bge": round(s, 4), "rrf": round(picks[k][1], 4)})
           for k, s in enumerate(scores)]
    out.sort(reverse=True)
    return out[:top]


def health():
    try:
        s = score_pairs([("测试", "这是一段测试文本")])[0]
        return True, "ok score=%.3f model=%s" % (s, MODEL_DIR)
    except Exception as e:
        return False, repr(e)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ok, msg = health()
    print(("可用：" if ok else "不可用：") + msg)
