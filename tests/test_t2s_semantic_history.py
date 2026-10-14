"""Semantic history and EOS availability contracts for single-item AR decoding."""
from pathlib import Path
import sys

import pytest

torch = pytest.importorskip("torch", reason="local-model tier requires torch")
pytest.importorskip("torchmetrics", reason="local-model tier requires torchmetrics")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
import AR.models.t2s_model as upstream_module  # noqa: E402

Text2SemanticDecoder = upstream_module.Text2SemanticDecoder
eos_module = upstream_module
infer = Text2SemanticDecoder.infer_panel_naive

class _PassThrough:
    def process_prompt(self, x, *args):
        return x, [], []

    def decode_next_token(self, x, k_cache, v_cache):
        return x, k_cache, v_cache


class _Predict(torch.nn.Module):
    def __init__(self, stop_kind):
        super().__init__()
        self.stop_kind = stop_kind
        self.calls = 0

    def forward(self, x):
        logits = x.new_tensor([[0.1, 0.3, 0.8, 0.4, 0.2, -2.0]]).expand(x.shape[0], -1).clone()
        if self.stop_kind == "greedy" and self.calls >= 11:
            logits[:, 5] = 2.0
        self.calls += 1
        return logits


@pytest.mark.parametrize("prompt_dtype", [None, torch.int32, torch.int64])
@pytest.mark.parametrize("stop_kind", ["sampled", "greedy", "length", "limit"])
def test_history_prefix_and_original_stop_contract(monkeypatch, prompt_dtype, stop_kind):
    config = {"model": {"hidden_dim": 8, "embedding_dim": 8, "head": 2,
                       "n_layer": 1, "vocab_size": 6, "phoneme_vocab_size": 8,
                       "dropout": 0.0, "EOS": 5}}
    decoder = Text2SemanticDecoder(config).eval()
    decoder.use_static_kv_cache = decoder.cuda_graph_enabled = False
    decoder.t2s_transformer = _PassThrough()
    decoder.ar_predict_layer = _Predict(stop_kind)
    prompts = None if prompt_dtype is None else torch.tensor([[0, 1, 1]], dtype=prompt_dtype)
    prefix = [] if prompts is None else prompts[0].tolist()
    history = list(prefix)
    pointers, eos_calls = [], []
    original_eos = eos_module._should_stop_on_eos

    def sample(logits, previous_tokens, **kwargs):
        # No padding, unwritten suffix, or previous-request history may reach
        # the existing sampler, even at the final bounded decode step.
        assert previous_tokens[0].tolist() == history
        assert kwargs == {"top_k": 3, "top_p": 0.9, "temperature": 0.6, "repetition_penalty": 1.35}
        pointers.append(previous_tokens.untyped_storage().data_ptr())
        index = len(history) - len(prefix)
        token = 5 if stop_kind == "sampled" and index == 11 else (index % 4)
        history.append(token)
        return torch.tensor([[token]], dtype=torch.int32), None

    def eos(logits, samples, token):
        assert logits.shape[-1] > token
        eos_calls.append(True)
        return original_eos(logits, samples, token)

    monkeypatch.setattr(upstream_module, "sample", sample)
    monkeypatch.setattr(eos_module, "_should_stop_on_eos", eos)
    x = torch.tensor([[1, 2]])
    output, index = infer(decoder, x, torch.tensor([2]), prompts,
                          torch.zeros(1, 1024, 2), top_k=3, top_p=0.9,
                          temperature=0.6, repetition_penalty=1.35,
                          early_stop_num=3 if stop_kind == "length" else -1,
                          enable_static_kv=False, enable_cuda_graph=False)
    calls = {"sampled": 12, "greedy": 12, "length": 4, "limit": 1500}[stop_kind]
    assert len(history) - len(prefix) == calls
    assert output[0].tolist() == history[:-1]  # Existing terminal-token trim.
    assert index == (0 if prompts is None else calls - 1)
    assert output.dtype == (torch.int32 if prompts is None else prompt_dtype)
    assert len(set(pointers)) == 1
    assert len(eos_calls) == max(0, calls - 11)
    if prompts is not None:
        assert prompts[0].tolist() == prefix
