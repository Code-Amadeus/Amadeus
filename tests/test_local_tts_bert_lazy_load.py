"""BERT features are only needed for Chinese text, so BERT loads on first use."""

import ast
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
source = ast.parse((ROOT / "local_tts_infer.py").read_text(encoding="utf-8"))
engine_class = next(node for node in source.body
                    if isinstance(node, ast.ClassDef) and node.name == "TTSInferencer")
methods = {node.name: node for node in engine_class.body if isinstance(node, ast.FunctionDef)}


class _Zeros:
    def to(self, *_args, **_kwargs):
        return self


class _Torch:
    """Just enough of torch for the zero-feature path, without the model stack."""

    float16 = "float16"
    float32 = "float32"

    @staticmethod
    def zeros(_shape, dtype=None):
        return _Zeros()


scope = {"torch": _Torch}
exec(compile(ast.Module(
    body=[methods[name] for name in ("_ensure_bert_model", "get_bert_inf", "get_bert_feature")],
    type_ignores=[],
), "local_tts_infer.py", "exec"), scope)


class LoadFailed(Exception):
    pass


class BertEngine:
    device = "cuda:0"
    is_half = True
    _ensure_bert_model = scope["_ensure_bert_model"]
    get_bert_inf = scope["get_bert_inf"]
    get_bert_feature = scope["get_bert_feature"]

    def __init__(self, *, fail=False):
        self.tokenizer = None
        self.bert_model = None
        self._bert_lock = threading.Lock()
        self.fail = fail
        self.loads = 0

    def _init_bert_model(self):
        self.loads += 1
        time.sleep(0.05)  # keep concurrent first requests inside the load
        if self.fail:
            raise LoadFailed
        self.tokenizer = object()
        self.bert_model = object()


def _calls(node, attribute):
    return [call for call in ast.walk(node) if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute) and call.func.attr == attribute]


def test_startup_does_not_load_bert():
    callers = sorted(name for name, node in methods.items() if _calls(node, "_init_bert_model"))
    assert callers == ["_ensure_bert_model"]


@pytest.mark.parametrize("language", ["all_ja", "ja", "en"])
def test_japanese_and_english_synthesis_never_loads_bert(language):
    engine = BertEngine()
    engine.get_bert_inf([1, 2, 3], None, "テスト", language)
    assert engine.loads == 0 and engine.bert_model is None


def test_concurrent_first_requests_load_bert_once():
    engine = BertEngine()
    workers = [threading.Thread(target=engine._ensure_bert_model) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert engine.loads == 1 and engine.bert_model is not None


def test_chinese_text_loads_bert_and_a_failed_load_is_retried():
    engine = BertEngine(fail=True)
    with pytest.raises(LoadFailed):
        engine.get_bert_inf([1, 2], [1, 1], "中文", "all_zh")
    assert engine.bert_model is None
    with pytest.raises(LoadFailed):
        engine.get_bert_inf([1, 2], [1, 1], "中文", "zh")
    assert engine.loads == 2
