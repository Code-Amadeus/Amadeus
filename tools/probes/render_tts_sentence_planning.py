"""Silent full guarded synthesis A/B with synthetic text and matching settings.

Replays a ready LLM burst and measures producer-ready gaps against an audio
ledger, not the physical speaker. Saves WAVs for listening; does not operate the
application or change model/settings files. Cold/warm order is explicit.
"""
from __future__ import annotations
import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=2)
    args = parser.parse_args()
    asset_root = args.asset_root.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      TTS_RUNTIME_WARMUP='0', TTS_SESSION_WARMUP='0', ENABLE_CUDA_GRAPH='1',
                      ENABLE_TTS_UTTERANCE_SCHEDULER='1', TTS_UTTERANCE_MIN_START_SEQ='2',
                      TTS_UTTERANCE_MAX_CHARS='120', TTS_UTTERANCE_FLUSH_TIMEOUT_MS='350')
    from dotenv import load_dotenv
    load_dotenv(asset_root / '.env', override=False)
    import numpy as np
    import soundfile as sf
    import torch
    # This probe changes scheduling, not the vocoder. Use the installation's
    # BigVGAN package so its per-source-directory compiled CUDA cache is shared.
    import GPT_SoVITS.BigVGAN as vocoder_package
    vocoder_package.__path__ = [str(asset_root / 'GPT_SoVITS' / 'BigVGAN')]
    import local_tts_infer as local
    from config import settings
    from tts import pipeline
    from tts.contract import TTSRequest
    from tts.deadline import SynthesisCostModel
    from tts.utterance_scheduler import TTSUtteranceScheduler
    from tools.probes.simulate_tts_aggregation import historical_scheduler

    local.root_dir = str(asset_root)
    logging.disable(logging.INFO)
    def absolute(value):
        path = Path(value)
        return str(path if path.is_absolute() else asset_root / path)
    print('Loading configured full TTS stack; no audio playback.', flush=True)
    with (output / 'model-load.log').open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        infer = local.TTSInferencer(device='cuda:0', gpt_path=absolute(settings.TTS_GPT_MODEL_PATH),
                                   sovits_path=absolute(settings.TTS_SOVITS_MODEL_PATH))
    pipeline._tts_runtime = SimpleNamespace(backend_id='gpt_sovits', deployment='embedded', is_rocm=False)
    fixed = historical_scheduler('codex/tts-scheduling-facts')
    ref_audio = absolute(settings.TTS_REF_AUDIO_JA)
    ref_text = settings.TTS_REF_TEXT_JA
    texts = ['まず確認しましょう。', '最初に設定を確認して、', '必要な情報を整理し、',
             '条件がそろったところで、', '全体の流れを確認します。',
             '時間に余裕がある場合には、', '具体的な手順を説明して、', '途中で条件が変わったら、',
             'その時点で計画を見直しましょう。', '最後に結果を確認します。',
             '次の作業では、', '最初に資料を集めて、', '順番に内容を確認し、',
             '必要な条件を整理して、', '問題がないことを確かめたら、', '最後まで作業を進めましょう。']
    decoder = infer.t2s_model.model
    original = type(decoder).infer_panel
    attempts = []
    def tracked(self, *a, **kw):
        prediction, idx = original(self, *a, **kw)
        attempts.append({'tokens': int(idx), 'limit': int(kw.get('early_stop_num') or 0)})
        return prediction, idx
    type(decoder).infer_panel = tracked
    all_rows = []

    async def trial(strategy, trial_index):
        nonlocal attempts
        pipeline._synthesis_cost = SynthesisCostModel()
        cover_end, estimate = 0.0, .6
        queue = asyncio.Queue()
        for i, text in enumerate(texts):
            queue.put_nowait(TTSRequest(sentence_id=f'sentence_{i+1}_probe', text=text,
                                       is_first=i == 0, stream_tts=i == 0, source='chat', turn_id='probe'))
        kwargs = dict(cover_seconds_getter=lambda: max(0, cover_end - time.perf_counter()),
                      deadline_enabled=True, cover_safety_margin_sec=1.5)
        if strategy == 'revised':
            os.environ.pop('TTS_UTTERANCE_MAX_SENTENCES', None)
            sched = TTSUtteranceScheduler(**kwargs, synthesis_seconds_getter=pipeline.predict_synthesis_seconds)
        else:
            os.environ['TTS_UTTERANCE_MAX_SENTENCES'] = '2'
            sched = fixed(**kwargs, rtf_getter=lambda: estimate, chars_per_sec=7.5)
        consumed, audio_parts, rows = 0, [], []
        while consumed < len(texts):
            job = await sched.next_job(queue)
            prepared = pipeline._prepare_synthesis_text(job.text)
            params = pipeline.get_sovits_params(prepared, job.is_first)
            attempts = []
            torch.manual_seed(1729 + consumed)
            torch.cuda.manual_seed_all(1729 + consumed)
            torch.cuda.synchronize()
            start = time.perf_counter()
            first_at = None
            chunks = []
            sample_rate = None
            keys_before = len(decoder.bucket_graphs)
            with (output / f'{trial_index}-{strategy}-{consumed}.log').open('w', encoding='utf-8') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                for sr, chunk, _ in infer.infer_stream(text=prepared, ref_audio_path=ref_audio,
                                                       prompt_text=ref_text, **params):
                    if chunk is not None and len(chunk):
                        sample_rate = sr
                        stamp = time.perf_counter()
                        if first_at is None:
                            first_at = stamp
                        chunks.append(np.asarray(chunk, dtype=np.float32))
                        if job.is_first:
                            cover_end = max(cover_end, stamp) + len(chunk) / sr
                torch.cuda.synchronize()
            end = time.perf_counter()
            if not chunks:
                raise RuntimeError('probe received no audio')
            sr = sample_rate
            audio = np.concatenate(chunks)
            elapsed = end - start
            gap = 0.0 if job.is_first else max(0, end - cover_end)
            if not job.is_first:
                if gap:
                    audio_parts.append(np.zeros(round(gap * sr), dtype=np.float32))
                cover_end = max(cover_end, end) + len(audio) / sr
                pipeline._observe_synthesis(elapsed, prepared, params, job.utterance_id)
                estimate = .7 * estimate + .3 * elapsed / (len(job.text) / 7.5)
            audio_parts.append(audio)
            row = dict(strategy=strategy, trial=trial_index, seq=consumed+1, fragments=job.consumed_count,
                       chars=len(prepared), sample_steps=params['sample_steps'], cut=params['how_to_cut'],
                       producer_seconds=round(elapsed, 4), first_chunk_seconds=round(first_at-start, 4),
                       audio_seconds=round(len(audio)/sr, 4), gap_seconds=round(gap, 4),
                       attempts=attempts, all_silent=not bool(np.any(audio)),
                       new_graph_keys=len(decoder.bucket_graphs)-keys_before,
                       graph_stats=dict(decoder.cuda_graph_stats))
            print(json.dumps(row), flush=True)
            rows.append(row)
            consumed += job.consumed_count
        sf.write(output / f'{trial_index}-{strategy}.wav', np.concatenate(audio_parts), sr, subtype='PCM_16')
        return rows
    try:
        for repeat in range(args.rounds):
            for strategy in (('fixed', 'revised') if repeat % 2 == 0 else ('revised', 'fixed')):
                all_rows.extend(asyncio.run(trial(strategy, len(all_rows))))
                (output / 'manifest.json').write_text(json.dumps(all_rows, indent=2), encoding='utf-8')
    finally:
        type(decoder).infer_panel = original


if __name__ == '__main__':
    main()
