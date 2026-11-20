"""Measure real output-device preparation between idle voice turns.

Opens the default PyAudio output device without calling a model. Optional silent
PCM followed by a device-buffer drain approximates a completed playback turn.
Run the same command before/after a change; results are preparation latency, not
end-to-end speech latency. Every resource belongs to this probe process.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.chat_runtime import ChatRuntime
from server.handlers.tts_handler import TtsHandler
from tts.playback import PlaybackManager, StreamPlayer


class SilentMouth:
    def publish_mouth_value(self, _value):
        pass


async def measure(rounds: int, silence_ms: int = 0) -> dict:
    player = StreamPlayer(SilentMouth())
    manager = PlaybackManager(player)
    handler = TtsHandler()
    handler.configure(manager, player)
    runtime = ChatRuntime()
    runtime.configure(pending_sentence_items=asyncio.Queue(), playback_manager=manager)
    rows = []
    try:
        started = time.perf_counter()
        await runtime.prepare_role_audio()
        cold_ms = (time.perf_counter() - started) * 1000
        if player.stream is None:
            raise RuntimeError("output device did not open; preparation cannot be measured")
        # Import/startup costs are outside the measured turn boundary.
        import tts.pipeline  # noqa: F401

        for index in range(rounds):
            if silence_ms:
                import numpy as np
                await player.write_audio_async(np.zeros(24 * silence_ms, dtype=np.float32))
                # write() reports submission, so let the silent PCM and output
                # buffer drain before declaring this an idle turn boundary.
                await asyncio.sleep(silence_ms / 1000 + player.stream.get_output_latency() + 0.05)
            previous_stream = player.stream
            started = time.perf_counter()
            await handler._interrupt({"source": "probe_idle_turn"})
            interrupted = time.perf_counter()
            await runtime.prepare_role_audio()
            prepared = time.perf_counter()
            if player.stream is None:
                raise RuntimeError("output device was unavailable after preparation")
            rows.append({
                "round": index + 1,
                "interrupt_ms": (interrupted - started) * 1000,
                "prepare_ms": (prepared - interrupted) * 1000,
                "total_ms": (prepared - started) * 1000,
                "stream_reused": previous_stream is player.stream,
            })
        return {
            "scope": "real default output device; idle interrupt then 24kHz preparation; no model calls",
            "silence_ms_before_each_round": silence_ms,
            "cold_prepare_ms": cold_ms,
            "rounds": rows,
            "summary": {
                name: {
                    "min": min(row[name] for row in rows),
                    "median": statistics.median(row[name] for row in rows),
                    "max": max(row[name] for row in rows),
                }
                for name in ("interrupt_ms", "prepare_ms", "total_ms")
            },
            "reused_count": sum(row["stream_reused"] for row in rows),
        }
    finally:
        player.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--silence-ms", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("rounds must be positive")
    if args.silence_ms < 0:
        parser.error("silence-ms must not be negative")
    report = asyncio.run(measure(args.rounds, args.silence_ms))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "reused_count": report["reused_count"]}))


if __name__ == "__main__":
    main()
