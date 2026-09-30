"""Test/probe-only hooks into the unchanged production Torch T2S blocks."""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import torch


ROOT = Path(__file__).resolve().parents[2]


def decoder_type():
    sys.path.insert(0, str(ROOT / "GPT_SoVITS"))
    from AR.models.t2s_model import Text2SemanticDecoder

    return Text2SemanticDecoder


def create_tiny_checkpoint(path, *, layers=2, seed=719):
    """Synthetic data only: generated from the actual production constructor."""
    config = {
        "model": {"hidden_dim": 16, "embedding_dim": 16, "head": 2,
                  "n_layer": layers, "phoneme_vocab_size": 23, "vocab_size": 17,
                  "EOS": 16, "dropout": 0},
        "data": {"max_sec": 20},
    }
    torch.manual_seed(seed)
    model = decoder_type()(config).eval()
    with torch.no_grad():
        model.ar_text_position.alpha.fill_(0.7)
        model.ar_audio_position.alpha.fill_(1.3)
        for name, parameter in model.named_parameters():
            if name.endswith("bias"):
                parameter.normal_(0.02, 0.01)
    torch.save({"config": config, "weight": {"model." + key: value
                for key, value in model.state_dict().items()}}, path)
    return Path(path)


def load_reference(checkpoint, device="cpu", inference_dtype="float32"):
    # Independently read the source, not the new converter's output.
    source = torch.load(checkpoint, map_location="cpu", weights_only=True)
    model = decoder_type()(source["config"]).eval()
    model.load_state_dict({key.removeprefix("model."): value for key, value in source["weight"].items()}, strict=True)
    model = model.to(device)
    if inference_dtype == "float16":
        model = model.half()
    elif inference_dtype != "float32":
        raise ValueError("Unsupported Torch probe dtype")
    return model


def synthetic_inputs(config, *, phone_length=7, prompt_length=9, steps=5, seed=73):
    rng = np.random.default_rng(seed)
    return {
        "phones": rng.integers(0, config.phoneme_vocab_size, (1, phone_length), dtype=np.int64),
        "prompt": rng.integers(0, config.eos, (1, prompt_length), dtype=np.int64),
        "bert": rng.normal(0, 0.1, (1, config.bert_dim, phone_length)).astype(np.float32),
        "history": rng.integers(0, config.eos, (1, steps), dtype=np.int64),
    }


class TorchTrace:
    """Capture original embeddings and each original inference block's output."""
    def __init__(self, model, *, capture_trace=True):
        self.model = model
        self.device = next(model.parameters()).device
        self.capture_trace = capture_trace

    @torch.inference_mode()
    def prefill(self, phones, prompt, bert):
        m = self.model
        phones = torch.as_tensor(phones, dtype=torch.long, device=self.device)
        prompt = torch.as_tensor(prompt, dtype=torch.long, device=self.device)
        bert = torch.as_tensor(bert, dtype=m.bert_proj.weight.dtype, device=self.device)
        text = m.ar_text_position(m.ar_text_embedding(phones) + m.bert_proj(bert.transpose(1, 2)))
        audio = m.ar_audio_position(m.ar_audio_embedding(prompt))
        value = torch.cat((text, audio), dim=1)
        trace = {"embedding": value} if self.capture_trace else {}
        t, a = phones.shape[1], prompt.shape[1]
        # Construct exactly the source's two mask rows, independently of MLX.
        mask = torch.cat((
            torch.nn.functional.pad(torch.zeros(t, t, dtype=torch.bool), (0, a), value=True),
            torch.nn.functional.pad(torch.triu(torch.ones(a, a, dtype=torch.bool), diagonal=1),
                                    (t, 0), value=False),
        ), dim=0)[None, None].expand(1, m.num_head, t + a, t + a).to(self.device)
        if not self.capture_trace:
            value, keys, values = m.t2s_transformer.process_prompt(value, mask, None)
            return m.ar_predict_layer(value[:, -1]), (keys, values, a), trace
        cache = []
        for i, block in enumerate(m.t2s_transformer.blocks):
            value, k, v = block.process_prompt(value, mask, None)
            cache.append((k, v))
            if self.capture_trace:
                trace[f"layer_{i}"] = value
        return m.ar_predict_layer(value[:, -1]), (cache, a), trace

    @torch.inference_mode()
    def decode_step(self, token, state):
        m = self.model
        if self.capture_trace:
            cache, offset = state
        else:
            keys, values, offset = state
        token = torch.as_tensor(token, dtype=torch.long, device=self.device)
        value = m.ar_audio_embedding(token)
        value = value * m.ar_audio_position.x_scale + m.ar_audio_position.alpha * m.ar_audio_position.pe[:, offset:offset + 1]
        trace = {"embedding": value} if self.capture_trace else {}
        if not self.capture_trace:
            value, keys, values = m.t2s_transformer.decode_next_token(value, keys, values)
            return m.ar_predict_layer(value[:, -1]), (keys, values, offset + 1), trace
        next_cache = []
        for i, (block, (k, v)) in enumerate(zip(m.t2s_transformer.blocks, cache)):
            value, k, v = block.decode_next_token(value, k, v)
            next_cache.append((k, v))
            if self.capture_trace:
                trace[f"layer_{i}"] = value
        return m.ar_predict_layer(value[:, -1]), (next_cache, offset + 1), trace


def compare_arrays(reference, candidate, *, atol, rtol):
    if isinstance(reference, torch.Tensor):
        reference = reference.detach().cpu().numpy()
    expected = np.asarray(reference, dtype=np.float32)
    actual = np.asarray(candidate, dtype=np.float32)
    difference = actual.astype(np.float64) - expected.astype(np.float64)
    return {
        "finite": bool(np.isfinite(actual).all() and np.isfinite(expected).all()),
        "passed": bool(np.isfinite(actual).all() and np.isfinite(expected).all()
                       and np.allclose(actual, expected, atol=atol, rtol=rtol)),
        "max_abs": float(np.max(np.abs(difference))),
        "rmse": float(np.sqrt(np.mean(difference ** 2))),
        "atol": atol, "rtol": rtol,
    }


def compare_logits(reference, candidate, *, atol, rtol):
    result = compare_arrays(reference, candidate, atol=atol, rtol=rtol)
    expected = np.asarray(reference.detach().cpu().numpy(), dtype=np.float32).reshape(-1)
    actual = np.asarray(candidate, dtype=np.float32).reshape(-1)
    count = min(5, expected.size)
    top_expected = np.argsort(-expected)[:count]
    top_actual = np.argsort(-actual)[:count]
    result.update({
        "top_logit_margin_reference": float(np.sort(expected)[-1] - np.sort(expected)[-2]),
        "top_logit_margin_candidate": float(np.sort(actual)[-1] - np.sort(actual)[-2]),
        "top5_overlap": len(set(top_expected) & set(top_actual)) / count,
        "top1_agrees": bool(top_expected[0] == top_actual[0]),
    })
    return result


def validate_numerics(checkpoint, artifact, inputs, *, device="cpu", atol=1e-3, rtol=1e-3,
                      inference_dtype="float32", model_loader=None):
    import mlx.core as mx
    from tts.semantic_mlx.model import T2SModel

    oracle = TorchTrace(load_reference(checkpoint))
    mlx_device = mx.cpu if device == "cpu" else mx.gpu
    rows = []
    with mx.stream(mlx_device):
        model = (model_loader or T2SModel.from_artifact)(artifact, inference_dtype=inference_dtype)
        phones, prompt = (mx.array(inputs[key].astype(np.int32)) for key in ("phones", "prompt"))
        bert = mx.array(inputs["bert"])
        actual_trace = {}
        actual, cache = model.prefill(phones, prompt, bert, trace=actual_trace)
        expected, state, expected_trace = oracle.prefill(inputs["phones"], inputs["prompt"], inputs["bert"])
        for step in range(inputs["history"].shape[1] + 1):
            mx.eval(actual, list(actual_trace.values()))
            for name in expected_trace:
                rows.append({"step": step, "tensor": name,
                             **compare_arrays(expected_trace[name], actual_trace[name], atol=atol, rtol=rtol)})
            rows.append({"step": step, "tensor": "logits",
                         **compare_logits(expected, actual, atol=atol, rtol=rtol)})
            # Compare cached decoding with a full recomputation of the SAME history.
            if step:
                full_prompt = mx.concatenate((prompt, mx.array(inputs["history"][:, :step].astype(np.int32))), axis=1)
                full, _ = model.prefill(phones, full_prompt, bert)
                mx.eval(full)
                rows.append({"step": step, "tensor": "cached_vs_full",
                             **compare_arrays(full, actual, atol=atol, rtol=rtol)})
            if step < inputs["history"].shape[1]:
                token = inputs["history"][:, step:step + 1]
                actual_trace = {}
                actual, cache = model.decode_step(mx.array(token.astype(np.int32)), cache, trace=actual_trace)
                expected, state, expected_trace = oracle.decode_step(token, state)
    return {"status": "passed" if all(row["passed"] for row in rows) else "failed",
            "purpose": "numerical_test", "mlx_device": device,
            "torch_device": "cpu",
            "inference_dtype": inference_dtype,
            "comparisons": len(rows), "rows": rows}
