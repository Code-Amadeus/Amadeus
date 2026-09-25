"""Opt-in VN question → production bridge/TTS → real audio-device timing.

Runs in an isolated process with no game, microphone, desktop host or providers.
Uses the configured local TTS and VN model. Plays one warmup and four Japanese
answers or recorded-input comments (ABBA); excludes warmup and disables audio caching.
--baseline-delivery whole-speak isolates waiting for the complete speech body
from first-segment delivery, with identical prompts and streaming in both arms.
Reports device-write timing and nonzero audio evidence, not a microphone loopback.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def run(args, output):
    # These apply only to this probe process, not the user's saved settings.
    os.environ["FIRST_SENTENCE_AUDIO_CACHE_ENABLED"] = "0"
    os.environ["AEC_REALTIME_ENABLED"] = "0"
    from config import settings
    from server import vn_tts_bridge as bridge
    from tts import pipeline
    from tts.mouth_signal import MouthSignalRouter
    from tts.playback import PlaybackManager, StreamPlayerWithBuffer
    from tts.registry import create_tts_runtime
    from vn_player.runtime import VNPlayerRuntime
    from vn_player.prompt_layers import compose_messages
    from vn_player.schemas import VNProfile

    recorded = None
    if args.session:
        session = Path(args.session)
        def read(name):
            return [json.loads(row) for row in (session / name).read_text("utf-8").splitlines()]
        calls = [row for row in read("model_calls.jsonl") if row["lane"] == "immediate"]
        index = next(i for i, row in enumerate(calls) if row["ok"] and row["response"].get("decision") == "speak")
        profile = json.loads((session / "profile.json").read_text("utf-8"))
        start = next(row["payload"] for row in read("runtime_events.jsonl") if row["type"] == "vn.start")
        recorded = {"profile": profile, "lines": read("raw_lines.jsonl")[:index + 1],
                    "script_path": start["script_path"],
                    "messages": compose_messages(VNProfile(**profile), "immediate", calls[index]["request"]["clean_context"])}

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    player = StreamPlayerWithBuffer(MouthSignalRouter())
    playback = PlaybackManager(player)
    executor = ThreadPoolExecutor(max_workers=2)
    synthesizer = create_tts_runtime(settings.TTS_BACKEND)
    pipeline.configure(tts_runtime=synthesizer, tts_executor=executor, playback_manager=playback,
                       player=player, pending_sentence_items=queue,
                       exp_tts_semaphore=asyncio.Semaphore(pipeline.selectable_tts_concurrency(settings.EXP_TTS_MAX_CONCURRENCY)))
    rows, current, changed, completed = [], {}, asyncio.Event(), set()

    def finished(sentence_id, text):
        completed.add(sentence_id)
        changed.set()
    playback.on_sentence_complete = finished

    def log_event(record):
        message = record.getMessage()
        match = re.search(r"sentence_\d+_\d+_[a-f0-9]+", message)
        if not match:
            return
        metadata = bridge.get_vn_sentence_metadata(match[0]) or {}
        if metadata.get("line_id") != current.get("label"):
            return
        stage = None
        if "[VN TTS] enqueue" in message:
            stage = "enqueue_ms"
        elif "first audio chunk generated" in message:
            stage = "audio_ready_ms"
        elif "first sound started" in message:
            stage = "first_sound_ms"
        if stage:
            current.setdefault(stage, round((record.created - current["started_at"]) * 1000))
            changed.set()

    class Observer(logging.Handler):
        def emit(self, record):
            if not loop.is_closed():
                loop.call_soon_threadsafe(log_event, record)
    observer = Observer()
    logging.getLogger().addHandler(observer)
    logging.getLogger("PlaybackManager").setLevel(logging.INFO)
    original_write = player.write_audio_async

    async def observe_audio(audio, *positional, **kwargs):
        import numpy as np
        array = audio.cpu().detach().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        if array.size:
            current["peak"] = max(current.get("peak", 0), float(np.abs(array).max()))
            current["samples"] = current.get("samples", 0) + int(array.size)
        await original_write(audio, *positional, **kwargs)
    player.write_audio_async = observe_audio

    async def speak(payload):
        current.setdefault("speech_submitted_ms", round((time.time() - current["started_at"]) * 1000))
        receipt = await bridge.submit_vn_tts_confirmed(
            {**payload, "source": "vn_player", "line_id": current["label"],
             "voice_text_ja": payload["text"], "complete_turn": True}, pending_sentence_items=queue,
        )
        if receipt.get("status") != "queued":
            raise RuntimeError(f"Speech was not queued: {receipt.get('status')}")
        current["last_sentence_id"] = receipt.get("last_sentence_id") or receipt.get("sentence_id")

    async def wait_audio(timeout=90):
        async with asyncio.timeout(timeout):
            while current.get("last_sentence_id") not in completed:
                changed.clear()
                await changed.wait()
        if not current.get("first_sound_ms") or not current.get("peak", 0) > 0:
            raise AssertionError("No nonzero device-playback evidence; silence placeholders do not pass")

    workers = [asyncio.create_task(pipeline.play_sentence_worker()), asyncio.create_task(playback.run())]
    runtime = VNPlayerRuntime(output / "state", speak_callback=speak, speech_epoch=pipeline.current_tts_epoch)
    try:
        current = {"label": "warmup", "started_at": time.time()}
        await speak({"text": "準備できたわ。"})
        await wait_audio()
        print(json.dumps({"phase": "audio_warmup_passed", "peak": current["peak"]}), file=sys.__stdout__, flush=True)
        for index, stream in enumerate([False, True, True, False], 1):
            base = recorded["profile"] if recorded else {"prompt_pack": "base", "game_title": "VN latency experiment", "output_language": "ja"}
            await runtime.start({**base, "session_id": f"audio_trial_{index}",
                                 "script_path": recorded["script_path"] if recorded else "", "lookahead_llm_enabled": False,
                                 "summary_llm_enabled": False, "retrospective_llm_enabled": False})
            if recorded:
                await runtime.set_preferences({"session_id": runtime.profile.session_id, "commentary_paused": True})
                for line in recorded["lines"][:-1]:
                    await runtime.ingest_line(line)
                await runtime.set_preferences({"session_id": runtime.profile.session_id, "commentary_paused": False})
            complete = runtime.llm.complete_json
            async def measured(messages, *, lane, _complete=complete, _stream=stream, **kwargs):
                if recorded:
                    messages = recorded["messages"]
                if not _stream:
                    callback = kwargs.pop("on_ready", None)
                    if args.baseline_delivery == "whole-speak":
                        async def whole_speak(header, text_complete=True):
                            if text_complete and callback is not None:
                                await callback(header, True)
                        kwargs["on_ready"] = whole_speak
                    else:
                        lane = "benchmark_full_json"
                parsed, raw = await _complete(messages, lane=lane, **kwargs)
                current["model_decision"] = (parsed or {}).get("decision")
                return parsed, raw
            runtime.llm.complete_json = measured
            current = {"label": f"trial_{index}", "stream": stream or args.baseline_delivery == "whole-speak",
                       "delivery_mode": "first-segment" if stream else args.baseline_delivery, "started_at": time.time()}
            result = await runtime.ingest_line(recorded["lines"][-1]) if recorded else await runtime.player_intervention("ask", {
                "text": "音声確認のため、感情タグを使わず「聞こえているわ。」とだけ答えて。",
            })
            if result.get("status") != "ok" or result.get("reaction", {}).get("decision") != "speak" or current.get("model_decision") != "speak":
                raise RuntimeError("This model sample did not produce a valid admitted speech; do not fabricate audio latency")
            current["reaction_ready_ms"] = round((time.time() - current["started_at"]) * 1000)
            await wait_audio(45)
            rows.append(dict(current))
            print(json.dumps({"phase": "audio_trial", **current}), file=sys.__stdout__, flush=True)
            await runtime.stop()
        return {"status": "passed", "tts_backend": settings.TTS_BACKEND, "trials": rows,
                "input": "matched recorded comment with runtime guards" if recorded else "short VN question",
                "scope": "VN runtime, production bridge and shared TTS, nonzero physical device writes; no acoustic loopback"}
    finally:
        await runtime.stop()
        pipeline.interrupt_pending_tts()
        await playback.interrupt()
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for task in list(bridge._SUBTITLE_TASKS):
            task.cancel()
        await asyncio.gather(*list(bridge._SUBTITLE_TASKS), return_exceptions=True)
        player.cleanup()
        synthesizer.backend.close()
        executor.shutdown(wait=True)
        logging.getLogger().removeHandler(observer)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-model-and-audio", action="store_true")
    parser.add_argument("--output", default="output/diagnostics/vn-audio-latency")
    parser.add_argument("--session", help="Replay the first recorded spoken comment with its identical model prompt")
    parser.add_argument("--baseline-delivery", choices=("full-json", "whole-speak"), default="full-json")
    args = parser.parse_args()
    if not args.live_model_and_audio:
        raise SystemExit("Supply --live-model-and-audio to run models and play real audio.")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=output / "pipeline.log", level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", encoding="utf-8")
    with (output / "model-stdout.log").open("w", encoding="utf-8") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            try:
                report = asyncio.run(run(args, output))
            except Exception as exc:
                logging.exception("Audio latency probe failed")
                report = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(output / "report.json")}), flush=True)
    raise SystemExit(0 if report["status"] == "passed" else 1)
