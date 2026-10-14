"""Real scheduler jobs and full TTS A/B against a saved nested decoder.

Uses a controlled ready queue, normal EOS and first/non-first production
profiles. No playback, ASR, real conversation text or model downloads. The
current inferencer owns the shared non-AR stack; only the decoder is swapped.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "GPT_SoVITS"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-root", type=Path, required=True)
    parser.add_argument("--baseline-source", type=Path, required=True)
    parser.add_argument("--baseline-buckets", type=int, nargs="+", help="Full-bucket control routing")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    from dotenv import load_dotenv
    load_dotenv(args.asset_root / ".env", override=False)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      TTS_RUNTIME_WARMUP="0", TTS_SESSION_WARMUP="0",
                      ENABLE_CUDA_GRAPH="1", ENABLE_CUDA_GRAPH_PRECAPTURE="1",
                      CUDA_GRAPH_PRECAPTURE_BUCKETS="512,768", TTS_KV_CACHE_LAYOUT="nested",
                      TTS_T2S_FLASH_ATTN="1", TTS_T2S_FLASH_ATTN_MODE="valid",
                      ENABLE_TTS_UTTERANCE_SCHEDULER="1", TTS_UTTERANCE_MIN_START_SEQ="2",
                      TTS_UTTERANCE_MAX_CHARS="160")
    os.environ.pop("TTS_UTTERANCE_MAX_SENTENCES", None)
    import numpy as np
    import torch
    import GPT_SoVITS.BigVGAN as vocoder_package
    vocoder_package.__path__ = [str(args.asset_root / "GPT_SoVITS" / "BigVGAN")]
    import local_tts_infer as local
    from config import settings
    from tts import pipeline
    from tts.contract import TTSRequest
    from tts.speech_onset import first_voiced_sample
    from tts.utterance_scheduler import TTSUtteranceScheduler
    from AR.models.t2s_flash_attn import apply_flash_attn_patch
    from tools.probes.benchmark_t2s_nested_kv import digest

    local.root_dir = str(args.asset_root)
    def absolute(value):
        path = Path(value)
        return str(path if path.is_absolute() else args.asset_root / path)
    print("Loading configured full TTS stack and saved nested decoder.", flush=True)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        infer = local.TTSInferencer(device="cuda:0", gpt_path=absolute(settings.TTS_GPT_MODEL_PATH),
                                   sovits_path=absolute(settings.TTS_SOVITS_MODEL_PATH))
    pipeline._tts_runtime = SimpleNamespace(backend_id="gpt_sovits", deployment="embedded", is_rocm=False,
                                            cuda_graph_enabled=infer.cuda_graph_enabled)
    current = infer.t2s_model.model
    spec = importlib.util.spec_from_file_location("AR.models.t2s_aggregation_baseline", args.baseline_source)
    baseline_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline_module
    spec.loader.exec_module(baseline_module)
    checkpoint = torch.load(absolute(settings.TTS_GPT_MODEL_PATH), map_location="cpu", weights_only=True)
    baseline = baseline_module.Text2SemanticDecoder(checkpoint["config"])
    baseline.load_state_dict(current.state_dict())
    baseline = baseline.half().to("cuda:0").eval()
    baseline.use_static_kv_cache = baseline.cuda_graph_enabled = True
    baseline.cuda_graph_replay_sync = False
    if args.baseline_buckets:
        baseline.kv_cache_buckets = args.baseline_buckets
    assert current.use_flash_attn_kvcache
    assert apply_flash_attn_patch(baseline, mode="valid")
    with redirect_stdout(io.StringIO()):
        assert all(baseline.precapture_cuda_graph([bucket for bucket in (448, 512, 768, 1024)
                                                     if bucket in baseline.kv_cache_buckets]).values())
    # Refuse automatic SDPA fallback for this FA2 evidence.
    for decoder in (baseline, current):
        for block in decoder.t2s_transformer_static.blocks:
            def strict_decode(x, k, v, pos, mask, valid=-1, sdpa=True, block=block):
                return block._finish_attention(x, block._flash_valid_len(x, k, v, pos)), k, v
            block.decode_next_token_with_static_cache = strict_decode
    texts = ["まず確認しましょう。", "最初に必要な条件を整理して、", "同じ方法でもう一度確かめましょう。",
             "次の作業では最初に資料を集めて、必要な情報を順番に確認し、",
             "途中で条件が変わった場合にはその時点で計画を見直して、",
             "問題がないことを確かめたら最後まで作業を進めましょう。", "最後に結果を確認します。"]

    async def jobs():
        queue = asyncio.Queue()
        for index, text in enumerate(texts):
            queue.put_nowait(TTSRequest(sentence_id=f"sentence_{index+1}_probe", text=text,
                                       is_first=index == 0, stream_tts=index == 0,
                                       source="chat", turn_id="probe"))
        scheduler = TTSUtteranceScheduler(deadline_enabled=False)
        values, consumed = [], 0
        while consumed < len(texts):
            job = await scheduler.next_job(queue)
            values.append(job)
            consumed += job.consumed_count
        return values
    planned = asyncio.run(jobs())
    assert any(job.consumed_count > 1 for job in planned)
    ref_audio, ref_text = absolute(settings.TTS_REF_AUDIO_JA), settings.TTS_REF_TEXT_JA
    report = {"purpose": __doc__, "torch": torch.__version__, "cuda": torch.version.cuda,
              "gpu": torch.cuda.get_device_name(0), "baseline_sha256": digest(args.baseline_source),
              "source_sha256": digest(ROOT / "GPT_SoVITS/AR/models/t2s_model.py"),
              "checkpoint_sha256": digest(absolute(settings.TTS_GPT_MODEL_PATH)),
              "baseline_buckets": args.baseline_buckets, "ready_queue": True, "deadline_enabled": False, "runs": [], "summary": []}
    models = {"baseline": baseline, "nested": current}

    def run(name, job, seed):
        decoder = models[name]
        infer.t2s_model.model = decoder
        original = decoder.infer_panel
        attempts = []
        def tracked(*values, **kwargs):
            prediction, index = original(*values, **kwargs)
            x, _, prompt = values[:3]
            attempts.append({"initial_kv": x.shape[1] + (prompt.shape[1] if prompt is not None else 0),
                             "generated_tokens": int(index),
                             "token_sha256": hashlib.sha256(prediction.cpu().numpy().tobytes()).hexdigest(),
                             "request_stats": {key: decoder.cuda_graph_stats.get(key) for key in (
                                 "start_bucket", "final_bucket", "promotions", "graph_replay_steps",
                                 "dynamic_steps", "static_steps", "capture_attempts")}})
            return prediction, index
        decoder.infer_panel = tracked
        prepared, params = pipeline._prepare_synthesis_request(job.text, job.is_first)
        assert params["enable_cuda_graph"]
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        torch.cuda.synchronize(0)
        before = len(decoder.bucket_graphs)
        chunks, first_ms, sample_rate = [], None, None
        start = time.perf_counter()
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                for sr, chunk, _ in infer.infer_stream(text=prepared, ref_audio_path=ref_audio,
                                                       prompt_text=ref_text, **params):
                    if chunk is not None and len(chunk):
                        audio = np.asarray(chunk, dtype=np.float32)
                        if first_ms is None and first_voiced_sample(audio, sr) is not None:
                            first_ms = (time.perf_counter() - start) * 1000
                        sample_rate = sr
                        chunks.append(audio)
                torch.cuda.synchronize(0)
        finally:
            decoder.infer_panel = original
        assert any(value["request_stats"]["graph_replay_steps"] > 0 for value in attempts)
        if not chunks:
            raise RuntimeError("No generated PCM")
        audio = np.concatenate(chunks)
        return {"layout": name, "chars": len(prepared), "fragments": job.consumed_count,
                "is_first": job.is_first, "sample_steps": params["sample_steps"], "cut": params["how_to_cut"],
                "elapsed_ms": (time.perf_counter() - start) * 1000, "first_voiced_chunk_ms": first_ms,
                "pcm_sha256": hashlib.sha256(audio.tobytes()).hexdigest(),
                "audio_seconds": len(audio) / sample_rate, "attempts": attempts,
                "new_graphs": len(decoder.bucket_graphs) - before, "graph_count": len(decoder.bucket_graphs)}

    # Complete real frontend/vocoder warmup before paired timing.
    for name in models:
        for job in planned:
            run(name, job, 1729)
    for index, job in enumerate(planned):
        pairs = []
        for repeat in range(args.repeats):
            order = ["baseline", "nested"] if repeat % 2 == 0 else ["nested", "baseline"]
            values = {name: run(name, job, 1729 + repeat) for name in order}
            equal_tokens = [value["token_sha256"] for value in values["baseline"]["attempts"]] == [
                value["token_sha256"] for value in values["nested"]["attempts"]]
            equal_pcm = values["baseline"]["pcm_sha256"] == values["nested"]["pcm_sha256"]
            row = {"job": index, "seed": 1729 + repeat, "order": order,
                   "tokens_equal": equal_tokens, "pcm_equal": equal_pcm, **values}
            pairs.append(row)
        report["runs"].extend(pairs)
        summary = {"job": index, "chars": pairs[0]["nested"]["chars"], "fragments": job.consumed_count,
                   "is_first": job.is_first, "pairs": len(pairs), "all_tokens_equal": all(row["tokens_equal"] for row in pairs),
                   "all_pcm_equal": all(row["pcm_equal"] for row in pairs),
                   "different_token_pairs": sum(not row["tokens_equal"] for row in pairs),
                   "baseline_median_ms": statistics.median(row["baseline"]["elapsed_ms"] for row in pairs),
                   "nested_median_ms": statistics.median(row["nested"]["elapsed_ms"] for row in pairs),
                   "baseline_first_voiced_median_ms": statistics.median(row["baseline"]["first_voiced_chunk_ms"] for row in pairs),
                   "nested_first_voiced_median_ms": statistics.median(row["nested"]["first_voiced_chunk_ms"] for row in pairs)}
        report["summary"].append(summary)
        print(json.dumps(summary), flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    infer.t2s_model.model = current


if __name__ == "__main__":
    main()
