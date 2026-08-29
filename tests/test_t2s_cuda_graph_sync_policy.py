"""CUDA Graph replay synchronization policy regression tests.

Runnable directly by tools/run_tests.py and compatible with pytest.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "GPT_SoVITS"))

from AR.models.t2s_model import Text2SemanticDecoder, _env_flag_enabled, _should_stop_on_eos
import AR.models.t2s_model as t2s_model


class _FakeGraph:
    def __init__(self) -> None:
        self.replays = 0

    def replay(self) -> None:
        self.replays += 1


def test_replay_does_not_synchronize_by_default():
    decoder = Text2SemanticDecoder.__new__(Text2SemanticDecoder)
    decoder.cuda_graph_replay_sync = False
    graph = _FakeGraph()
    sync_calls = []
    original = t2s_model.torch.cuda.synchronize
    t2s_model.torch.cuda.synchronize = lambda device=None: sync_calls.append(device)
    try:
        decoder._replay_cuda_graph(graph, "cuda:0")
    finally:
        t2s_model.torch.cuda.synchronize = original
    assert graph.replays == 1
    assert sync_calls == []


def test_replay_can_opt_in_to_diagnostic_synchronize():
    decoder = Text2SemanticDecoder.__new__(Text2SemanticDecoder)
    decoder.cuda_graph_replay_sync = True
    graph = _FakeGraph()
    sync_calls = []
    original = t2s_model.torch.cuda.synchronize
    t2s_model.torch.cuda.synchronize = lambda device=None: sync_calls.append(device)
    try:
        decoder._replay_cuda_graph(graph, "cuda:1")
    finally:
        t2s_model.torch.cuda.synchronize = original
    assert graph.replays == 1
    assert sync_calls == ["cuda:1"]


def test_env_flag_defaults_off_and_accepts_explicit_true():
    key = "T2S_TEST_REPLAY_SYNC_FLAG"
    original = os.environ.get(key)
    try:
        os.environ.pop(key, None)
        assert _env_flag_enabled(key, False) is False
        os.environ[key] = "true"
        assert _env_flag_enabled(key, False) is True
        os.environ[key] = "0"
        assert _env_flag_enabled(key, True) is False
    finally:
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


def test_eos_checks_preserve_greedy_and_sampled_semantics():
    torch = t2s_model.torch
    logits = torch.tensor([[0.1, 0.9, 0.0]])
    assert _should_stop_on_eos(logits, torch.tensor([[0]]), 1) is True
    assert _should_stop_on_eos(logits, torch.tensor([[2]]), 2) is True
    assert _should_stop_on_eos(logits, torch.tensor([[0]]), 2) is False


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all t2s CUDA Graph sync policy tests passed")


if __name__ == "__main__":
    _main()
