"""Deterministic playback/synthesis experiment; no models, network or audio.

The supplied rates are synthetic, not calibration for a particular machine.
This experiment isolates a ready LLM burst; bounded waits and late arrivals
are covered by the asynchronous contract tests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import inspect
import os
from pathlib import Path
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def simulate(scheduler_class, rates):
    from tts.contract import TTSRequest

    now = 0.15  # the opening has just become available
    playback_end = now + 0.8
    estimate = 0.6
    last_chars = None
    queue = asyncio.Queue()
    # A burst of clause-sized inputs from the LLM after a short opening.
    fragment_count = 96
    for seq in range(2, fragment_count + 2):
        queue.put_nowait(TTSRequest(sentence_id=f"sentence_{seq}_simulation",
                                   text="説明を続けると、" if seq < fragment_count + 1 else "これで説明終了。",
                                   source="chat", turn_id="synthetic"))
    growth = {}
    if 'last_synthesis_chars_getter' in inspect.signature(scheduler_class).parameters:
        growth['last_synthesis_chars_getter'] = lambda: last_chars
    scheduler = scheduler_class(
        cover_seconds_getter=lambda: max(0.0, playback_end - now),
        rtf_getter=lambda: estimate, deadline_enabled=True,
        cover_safety_margin_sec=1.5, chars_per_sec=7.5,
        **growth,
    )
    jobs = []
    count = 0
    while count < fragment_count:
        job = await scheduler.next_job(queue)
        duration = len(job.text) / 7.5
        before_change = min(job.consumed_count, max(0, 16 - count))
        rate = (before_change * rates[0] + (job.consumed_count - before_change) * rates[-1]) / job.consumed_count
        elapsed = 0.08 + duration * rate  # fixed per-request overhead + work
        now += elapsed
        gap = max(0.0, now - playback_end)
        playback_end = max(now, playback_end) + duration + 0.4
        observed = elapsed / duration
        estimate = max(observed, estimate * 0.7 + observed * 0.3)
        last_chars = len(job.text)
        count += job.consumed_count
        jobs.append({'fragments':job.consumed_count, 'chars':len(job.text),
                     'gap_seconds':round(gap, 4)})
    return {'jobs':len(jobs), 'second_unit_fragments':jobs[0]['fragments'],
            'second_unit_gap':jobs[0]['gap_seconds'],
            'max_chars':max(row['chars'] for row in jobs),
            'total_gap_seconds':round(sum(row['gap_seconds'] for row in jobs),4),
            'explicit_pause_seconds':round(len(jobs) * 0.4, 4),
            'groups':[row['fragments'] for row in jobs]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', help='Optional Git ref of the previous scheduler')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    os.environ.update(ENABLE_TTS_UTTERANCE_SCHEDULER='1', TTS_UTTERANCE_MIN_START_SEQ='2',
                      TTS_UTTERANCE_MAX_SENTENCES='2', TTS_UTTERANCE_MAX_CHARS='120',
                      TTS_UTTERANCE_FLUSH_TIMEOUT_MS='350')
    from tts.utterance_scheduler import TTSUtteranceScheduler
    strategies = {'adaptive':TTSUtteranceScheduler}
    if args.baseline_ref:
        source = subprocess.check_output(
            ['git', 'show', f'{args.baseline_ref}:tts/utterance_scheduler.py'], cwd=ROOT,
            encoding='utf-8-sig')
        module = types.ModuleType('_tts_baseline_scheduler')
        sys.modules[module.__name__] = module
        exec(compile(source, '<baseline scheduler>', 'exec'), module.__dict__)
        strategies['baseline'] = module.TTSUtteranceScheduler
    results = {}
    for name, rates in [('fast', [0.15]), ('medium', [0.6]), ('slow', [1.2]),
                        ('sudden_slowdown', [0.15, 1.2])]:
        results[name] = {label:asyncio.run(simulate(strategy, rates))
                         for label, strategy in strategies.items()}
    rendered = json.dumps(results, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
