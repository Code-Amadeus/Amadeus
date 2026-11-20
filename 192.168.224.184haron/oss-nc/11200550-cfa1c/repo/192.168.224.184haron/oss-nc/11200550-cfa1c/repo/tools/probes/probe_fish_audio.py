"""Probe Fish Audio requests, record streaming timing, and save PCM as WAV.

Run from the repository root with ``python -m tools.probes.probe_fish_audio``.
Credentials come from FISH_TTS_API_KEY (.env or the desktop secret store).
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
import json
from pathlib import Path
import time
import wave

import numpy as np

from tts.backend import TTSAudioChunk, TTSSynthesisRequest, TTSBackendError, TTSRuntimeAdapter
from tts.backends.fish_audio import FishAudioTTSBackend


async def probe(args: argparse.Namespace) -> dict:
    backend = FishAudioTTSBackend(model=args.model, latency=args.latency)
    started = time.perf_counter()
    text_times: list[float] = []
    first_audio = None
    input_complete = False
    audio_before_input_complete = False
    samples = 0
    audio_chunks = 0
    first_signal = None
    first_signal_available = None
    playback_cursor = 0.0
    estimated_first_sound = None
    underrun_seconds = 0.0

    async def text_source():
        nonlocal input_complete
        for index, chunk in enumerate(args.chunk):
            if index:
                await asyncio.sleep(args.gap)
            text_times.append(time.perf_counter() - started)
            yield chunk
        input_complete = True

    async def sentence_sessions():
        nonlocal input_complete
        runtime = TTSRuntimeAdapter(backend)
        for index, text in enumerate(args.chunk):
            if index:
                await asyncio.sleep(args.gap)
            text_times.append(time.perf_counter() - started)
            stream = runtime.infer_stream(text=text, ref_audio_path="")
            try:
                while True:
                    item = await asyncio.to_thread(next, stream, None)
                    if item is None:
                        break
                    sample_rate, audio, text = item
                    yield TTSAudioChunk(sample_rate, audio, text)
            finally:
                await asyncio.to_thread(stream.close)
        input_complete = True

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "model": backend._model,
        "reference_id": backend._reference_id,
        "latency": backend._latency,
        "flush_each_chunk": args.flush_each_chunk or args.sentence_sessions,
        "input_gap_seconds": args.gap,
        "sentence_sessions": args.sentence_sessions,
    }
    try:
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            audio_stream = sentence_sessions() if args.sentence_sessions else backend.synthesize_text_stream(
                TTSSynthesisRequest(""), text_source(), flush_each_chunk=args.flush_each_chunk
            )
            async with aclosing(audio_stream) as stream:
                async for chunk in stream:
                    arrived = time.perf_counter() - started
                    if first_audio is None:
                        first_audio = arrived
                        audio_before_input_complete = not input_complete
                    elif arrived > playback_cursor:
                        underrun_seconds += arrived - playback_cursor
                    play_start = max(arrived, playback_cursor)
                    # 10 ms RMS above -40 dBFS marks signal, not a perceptual speech detector.
                    if first_signal is None:
                        window = max(1, chunk.sample_rate // 100)
                        for offset in range(0, len(chunk.audio), window):
                            frame = chunk.audio[offset:offset + window]
                            if np.sqrt(np.mean(frame ** 2)) >= 0.01:
                                first_signal = (samples + offset) / chunk.sample_rate
                                first_signal_available = arrived
                                estimated_first_sound = play_start + offset / chunk.sample_rate
                                break
                    playback_cursor = play_start + chunk.audio.size / chunk.sample_rate
                    audio_chunks += 1
                    samples += chunk.audio.size
                    wav.writeframes((chunk.audio * 32768).astype("<i2").tobytes())
        result["status"] = "ok"
    except TTSBackendError as exc:
        result.update(status="error", error=str(exc))
    result.update(
        text_chunk_available_seconds=text_times,
        first_audio_seconds=first_audio,
        first_signal_audio_offset_seconds=first_signal,
        first_signal_available_seconds=first_signal_available,
        estimated_immediate_playback_first_sound_seconds=estimated_first_sound,
        estimated_immediate_playback_underrun_seconds=underrun_seconds,
        audio_before_input_complete=audio_before_input_complete,
        audio_chunks=audio_chunks,
        audio_seconds=samples / 44100,
        elapsed_seconds=time.perf_counter() - started,
        output=str(output.resolve()),
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk", action="append", help="Text chunk; repeat to stream several")
    parser.add_argument("--gap", type=float, default=1.0, help="Seconds between input chunks")
    parser.add_argument("--flush-each-chunk", action="store_true")
    parser.add_argument("--sentence-sessions", action="store_true", help="Use the real synchronous sentence adapter, one connection per local text chunk")
    parser.add_argument("--model", default=None, help="Override FISH_TTS_MODEL")
    parser.add_argument("--latency", choices=("normal", "balanced", "low"), default=None)
    parser.add_argument("--output", default="tmp/fish-audio-probe.wav")
    args = parser.parse_args()
    if not np.isfinite(args.gap) or args.gap < 0:
        parser.error("--gap must be non-negative and finite")
    args.chunk = args.chunk or ["こんにちは、牧瀬紅莉栖です。", "音声のストリーミングを確認しています。"]
    result = asyncio.run(probe(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
