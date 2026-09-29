"""Synthetic comparison with each revision's own feedback rule; no GPU/audio.

Models fixed overhead, a 45-character cost transition, delayed LLM arrivals,
sentence boundaries and abrupt rate changes. Audio length is synthetic; this
does not measure adapter segmentation, intelligibility or physical playback.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tts.contract import TTSRequest
from tts.deadline import SynthesisCostModel
from tts.utterance_scheduler import TTSUtteranceScheduler


def historical_scheduler(ref):
    original = sys.modules['tts.deadline']
    try:
        for path, name in [('tts/deadline.py', 'tts.deadline'),
                           ('tts/utterance_scheduler.py', '_scheduler_' + ref.replace('/', '_'))]:
            source = subprocess.check_output(['git', 'show', f'{ref}:{path}'], cwd=ROOT, encoding='utf-8-sig')
            module = types.ModuleType(name)
            sys.modules[name] = module
            exec(compile(source, path, 'exec'), module.__dict__)
        return module.TTSUtteranceScheduler
    finally:
        sys.modules['tts.deadline'] = original


async def simulate(strategy, sched_class, *, rate=.6, long_factor=2, overhead=.08,
                   arrival_interval=0, sentence_fragments=6, slowdown=None):
    now, playback_end = .15, .95
    estimate, last_chars = .6, None
    queue = asyncio.Queue()
    model = SynthesisCostModel()
    pending = [(i * arrival_interval,
                TTSRequest(sentence_id=f'sentence_{i+2}_simulation',
                           text='説明を続けると、' if (i+1) % sentence_fragments else 'これで説明終了。',
                           source='chat', turn_id='simulation')) for i in range(96)]

    def advance(value):
        nonlocal now
        now = value
        while pending and pending[0][0] <= now:
            queue.put_nowait(pending.pop(0)[1])

    def predict(text):
        tier = 32 if len(text) >= 45 else 16
        return model.predict(tier, len(text), initial_seconds_per_char=.6 / 7.5 * tier / 16)

    async def wait_for(awaitable, timeout):
        if queue.empty() and (not pending or pending[0][0] > now + timeout):
            advance(now + timeout)
            awaitable.close()
            raise asyncio.TimeoutError
        if queue.empty():
            advance(pending[0][0])
        return await awaitable

    loop = asyncio.get_running_loop()
    old_time, old_wait = loop.time, asyncio.wait_for
    loop.time = lambda: now
    asyncio.wait_for = wait_for
    os.environ.update(ENABLE_TTS_UTTERANCE_SCHEDULER='1', TTS_UTTERANCE_MIN_START_SEQ='2',
                      TTS_UTTERANCE_MAX_CHARS='120', TTS_UTTERANCE_FLUSH_TIMEOUT_MS='350')
    os.environ['TTS_UTTERANCE_MAX_SENTENCES'] = '2'
    kwargs = dict(cover_seconds_getter=lambda: max(0, playback_end - now), deadline_enabled=True,
                  cover_safety_margin_sec=1.5)
    if strategy == 'revised':
        os.environ.pop('TTS_UTTERANCE_MAX_SENTENCES')
        kwargs['synthesis_seconds_getter'] = predict
    else:
        kwargs.update(rtf_getter=lambda: estimate, chars_per_sec=7.5)
        if strategy == 'original_pr':
            kwargs['last_synthesis_chars_getter'] = lambda: last_chars
    sched = sched_class(**kwargs)
    rows, count = [], 0
    try:
        while count < 96:
            advance(now)
            if not sched._buffer and queue.empty():
                advance(pending[0][0])
            job = await sched.next_job(queue)
            chars = len(job.text)
            duration = chars / 7.5
            actual_rate = slowdown if slowdown is not None and count >= 16 else rate
            elapsed = overhead + duration * actual_rate * (long_factor if chars >= 45 else 1)
            advance(now + elapsed)
            gap = max(0, now - playback_end)
            playback_end = max(now, playback_end) + duration + .4
            if strategy == 'revised':
                model.observe(32 if chars >= 45 else 16, chars, elapsed)
            elif strategy == 'original_pr':
                sample = elapsed / duration
                estimate = max(sample, .7 * estimate + .3 * sample)
            else:
                # Main measured request wall time / produced audio (with tail).
                sample = elapsed / (duration + .4)
                estimate = .7 * estimate + .3 * sample
            last_chars = chars
            count += job.consumed_count
            rows.append(dict(fragments=job.consumed_count, chars=chars, gap=round(gap, 4)))
    finally:
        loop.time, asyncio.wait_for = old_time, old_wait
    return dict(jobs=len(rows), second_unit_gap=rows[0]['gap'],
                total_gap=round(sum(r['gap'] for r in rows), 4),
                max_chars=max(r['chars'] for r in rows), groups=[r['fragments'] for r in rows])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='00e3864')
    parser.add_argument('--original-pr-ref', default='e27f58f')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    strategies = {'main': historical_scheduler(args.baseline_ref),
                  'original_pr': historical_scheduler(args.original_pr_ref),
                  'revised': TTSUtteranceScheduler}
    cases = {'fast': dict(rate=.15), 'medium': dict(rate=.6), 'slow': dict(rate=1.2),
             'audit_long_sentence': dict(rate=.6, sentence_fragments=96),
             'fixed_overhead': dict(rate=.15, overhead=.5),
             'delayed_text': dict(rate=.6, arrival_interval=.45),
             'sudden_slowdown': dict(rate=.15, slowdown=1.2)}
    result = {name: {label: asyncio.run(simulate(label, cls, **case))
                     for label, cls in strategies.items()} for name, case in cases.items()}
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
