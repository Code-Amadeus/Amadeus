"""Local, silent T2S length sweep; no model files or app state are modified."""
import argparse
import contextlib
import importlib
import io
import logging
import json
import os
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--code-root', type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument('--asset-root', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.code_root = args.code_root.resolve()
args.asset_root = args.asset_root.resolve()
args.output = args.output.resolve()
args.output.parent.mkdir(parents=True, exist_ok=True)
logging.disable(logging.INFO)
sys.path.insert(0, str(args.code_root))
os.chdir(args.code_root)
os.environ.update(ENABLE_CUDA_GRAPH='1', ENABLE_CUDA_GRAPH_PRECAPTURE='0',
                  TTS_RUNTIME_WARMUP='0', TTS_SESSION_WARMUP='0',
                  HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
from dotenv import load_dotenv
load_dotenv(args.asset_root / '.env', override=False)

import torch
import local_tts_infer as local
from tools.probes import probe_gsv_stability as probe
from config import settings

local.root_dir = str(args.asset_root)
probe.ROOT = args.asset_root

class SemanticOnly(local.TTSInferencer):
    def _load_bigvgan_model(self):
        self.bigvgan_model = None

clauses = [
    'まず条件を整理しましょう。',
    '短い説明から始めて、',
    '必要な情報が集まったところで、',
    '全体の流れを確認します。',
    '時間に余裕がある場合には、',
    '具体的な手順も合わせて説明できます。',
    '途中で条件が変わった場合は、',
    'その時点で計画を見直しましょう。',
]
texts = [''.join(clauses[:n]) for n in [1,2,3,4,5,6,8]]
cases = [probe.ProbeCase(f'prefix_{i+1}', text, 'length') for i,text in enumerate(texts)]
print('Loading local semantic models (no audio playback)...', flush=True)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    def asset_path(value):
        path = Path(value)
        return str(path if path.is_absolute() else args.asset_root / path)
    infer = SemanticOnly(device='cuda:0',
                         gpt_path=asset_path(settings.TTS_GPT_MODEL_PATH),
                         sovits_path=asset_path(settings.TTS_SOVITS_MODEL_PATH))
    contexts = probe._build_contexts(infer, cases, torch)
module = importlib.import_module(infer.t2s_model.model.__class__.__module__)
module.tqdm = lambda values: values
decoder = infer.t2s_model.model
print('Models ready; beginning fixed-seed cold/warm trials.', flush=True)
rows = []
for context in contexts:
    for repeat in range(2):
        torch.manual_seed(1729)
        torch.cuda.manual_seed_all(1729)
        keys_before = set(decoder.bucket_graphs)
        prompt_len = int(context.all_phoneme_len[0]) + int(context.prompt_semantic.shape[-1])
        output = io.StringIO()
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode(), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            prediction, idx = decoder.infer_panel(
                context.all_phoneme_ids, context.all_phoneme_len,
                context.prompt_semantic, context.bert,
                top_k=5, top_p=1, temperature=.6, repetition_penalty=1.35,
                early_stop_num=1000, enable_cuda_graph=True, enable_static_kv=True)
        torch.cuda.synchronize()
        elapsed = time.perf_counter()-started
        stats = decoder.cuda_graph_stats
        rows.append(dict(chars=len(context.case.text), repeat=repeat,
                         prompt_tokens=prompt_len, bucket=decoder._select_bucket(prompt_len),
                         generated_tokens=int(idx), elapsed_seconds=round(elapsed,4),
                         hit_token_limit=int(idx) >= 1000,
                         graph_steps=stats['graph_replay_steps'], total_steps=stats['total_steps'],
                         new_graph_keys=len(set(decoder.bucket_graphs)-keys_before),
                         dynamic_fallback='dynamic KV' in output.getvalue(),
                         peak_allocated_mb=round(torch.cuda.max_memory_allocated()/2**20)))
        print(json.dumps(rows[-1]), flush=True)
        args.output.write_text(json.dumps({'device':torch.cuda.get_device_name(),
                                          'scope':'semantic decoder only; no vocoder or acoustic timing',
                                          'rows':rows},indent=2), encoding='utf-8')
print('Length sweep complete.', flush=True)
