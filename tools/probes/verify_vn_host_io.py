"""Opt-in real host VN playback, window vision and microphone acceptance.

Start server.app separately. This uses a saved profile without changing it, opens
the game, speaks one short reply and (only with --checks microphone) opens the microphone.
It stops its VN session and only its launcher-owned game on exit. No synthetic
recognition event is injected; microphone success needs real spoken input.
"""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import time
import uuid

import websockets


def verify_audio(cache_dir, text, since):
    """Playback events alone also fire for a failed backend's silence chunk."""
    import numpy as np

    for path in Path(cache_dir).rglob("*.npz"):
        if path.stat().st_mtime < since:
            continue
        with np.load(path, allow_pickle=False) as entry:
            meta = json.loads(str(entry["meta_json"].item()))
            if meta.get("processed_text") != text:
                continue
            audio = entry["audio"]
            assert audio.size and np.isfinite(audio).all(), "Invalid synthesized audio"
            peak = float(np.abs(audio).max())
            assert peak > 0, "Playback contained only silence; synthesis did not pass"
            return {"path": str(path), "peak": peak, "samples": int(audio.size), "sample_rate": int(entry["sr"])}
    raise AssertionError("No freshly synthesized audio evidence; playback events are insufficient")


class Client:
    def __init__(self, socket):
        self.socket, self.pending, self.events = socket, {}, []
        self.changed = asyncio.Event()

    async def receive(self):
        try:
            async for raw in self.socket:
                item = json.loads(raw)
                if item.get("type") == "res" and item.get("id") in self.pending:
                    future = self.pending.pop(item["id"])
                    if not future.done():
                        future.set_result(item["params"])
                elif item.get("type") == "evt":
                    self.events.append(item)
                    self.changed.set()
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Test host disconnected"))
            self.pending.clear()

    async def call(self, method, params=None, timeout=120):
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.socket.send(json.dumps({"type": "req", "id": request_id, "method": method, "params": params or {}}))
        result = await asyncio.wait_for(future, timeout)
        if result.get("error"):
            raise RuntimeError(f"{method}: {result['error']}")
        return result

    async def event(self, method, after=0, predicate=lambda _p: True, timeout=90):
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                for event in self.events[after:]:
                    if event["method"] == method and predicate(event["params"]):
                        return event["params"]
                await self.changed.wait()


async def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {"started": time.time(), "checks": {}, "profile_id": args.profile_id}
    async with websockets.connect(f"ws://127.0.0.1:{args.port}/ws", max_size=8 * 1024 * 1024) as socket:
        client = Client(socket)
        receiver = asyncio.create_task(client.receive())
        owned = False
        session = "vn_host_io_" + uuid.uuid4().hex[:12]
        try:
            state = await client.call("vn.launch.status")
            if state.get("status") in {"starting", "active", "stopping"} or (state.get("runtime") or {}).get("status") == "active":
                raise RuntimeError("An existing VN session must not be replaced by this probe.")
            owned = True
            state = await client.call("vn.launch.start", {"profileId": args.profile_id, "sessionId": session,
                "runtime": {"voice_input": False, "vision_mode": "off", "speech_enabled": False,
                            "output_language": "ja", "summary_llm_enabled": False, "retrospective_llm_enabled": False}})
            session = state["sessionId"]
            report["session_id"] = session
            report["game_pid"] = state["game"]["pid"]
            await client.call("vn.mode.set", {"session_id": session, "commentary_paused": True})
            # Recorded text isolates the host I/O check from advancing a live game.
            await client.call("vn.line", {"text": "咖啡馆今天重新开门。", "metadata": {"source": "acceptance_recorded_input"}})
            if "playback" in args.checks:
                if not args.audio_cache_dir:
                    raise ValueError("Playback qualification requires --audio-cache-dir (a fresh host FIRST_SENTENCE_AUDIO_CACHE_DIR).")
                before = len(client.events)
                muted = await client.call("vn.player.ask", {"session_id": session, "text": "音声テストです。「聞こえているわ。」と一言だけ答えて。"})
                assert muted.get("status") == "ok" and muted.get("reaction", {}).get("decision") == "speak"
                assert muted["reaction"].get("reason_label") != "model_unavailable"
                await asyncio.sleep(.5)
                assert not any(row["method"] == "tts.sentence_start" for row in client.events[before:])
                report["checks"]["muted_text_answer_without_playback"] = True
                await client.call("vn.mode.set", {"session_id": session, "speech_enabled": True})
                before, audio_since = len(client.events), time.time()
                spoken = await client.call("vn.player.ask", {"session_id": session, "text": "音声テストです。「聞こえているわ。」と一言だけ答えて。"})
                started = await client.event("tts.sentence_start", before)
                ended = await client.event("tts.sentence_end", before,
                    predicate=lambda p: p.get("sentence_id") == started.get("sentence_id"))
                audio = verify_audio(args.audio_cache_dir, ended.get("text"), audio_since)
                report["checks"]["real_tts_playback"] = {"sentence_id": started["sentence_id"], "text": ended.get("text"), "audio": audio, "reaction": spoken.get("reaction", {}).get("speak", {}).get("text")}
                print(json.dumps({"phase": "tts_playback_passed", "output": str(output)}, ensure_ascii=False), flush=True)
                await client.call("vn.mode.set", {"session_id": session, "speech_enabled": False})
            status = await client.call("vn.status")
            if "vision" in args.checks:
                assert status["inputs"]["vision"]["available"], status["inputs"]["vision"]
                # Keep the default portrait visible: occluding surfaces must not
                # enter a window capture. Save a preview for visual review; the
                # normal question handler independently captures a fresh frame.
                await asyncio.sleep(args.settle_seconds)
                capture = await client.call("vn.launch.capture")
                visual = capture["visual_context"]
                frame_path = output / "game-window.jpg"
                frame_path.write_bytes(base64.b64decode(visual["frame"]["dataBase64"]))
                await client.call("vn.input.set", {"session_id": session, "vision_mode": "on_question"})
                answer = await client.call("vn.player.ask", {"session_id": session, "text": "今のゲーム画面に表示されているタイトルかメニューを一つ教えて。"})
                assert answer.get("status") == "ok" and answer.get("reaction", {}).get("reason_label") != "model_unavailable", answer
                report["checks"]["real_game_window_question"] = {"answer": answer.get("reaction", {}).get("speak", {}).get("text"), "game_pid": state["game"]["pid"], "frame": str(frame_path), "visual_review": "required"}
                print(json.dumps({"phase": "vision_answer_received", "answer": report["checks"]["real_game_window_question"]["answer"]}, ensure_ascii=False), flush=True)
                await client.call("vn.input.set", {"session_id": session, "vision_mode": "off"})
            if "microphone" in args.checks:
                before = len(client.events)
                status = await client.call("vn.input.set", {"session_id": session, "voice": True})
                assert status["inputs"]["voice"]["enabled"]
                print(json.dumps({"phase": "microphone_ready", "prompt": "请说：咖啡馆今天开门了吗？", "timeout_seconds": 180}, ensure_ascii=False), flush=True)
                recognition = await client.event("asr.recognized", before,
                    predicate=lambda p: p.get("source") == "vn_player" and p.get("source_payload", {}).get("session_id") == session, timeout=180)
                accepted = await client.event("vn.player.event", before,
                    predicate=lambda p: p.get("event", {}).get("params", {}).get("source") == "asr")
                response = await client.event("vn.reaction", before,
                    predicate=lambda p: p.get("source") == "player.ask")
                await client.call("vn.input.set", {"session_id": session, "voice": False})
                report["checks"]["real_microphone_question"] = {"recognized": recognition.get("text"), "accepted": accepted["event"]["text"], "answer": response["reaction"].get("speak", {}).get("text")}
            report["result"] = "needs_visual_review" if "vision" in args.checks else "passed"
        except BaseException as exc:
            report["result"], report["error"] = "failed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            try:
                if owned:
                    current = await client.call("vn.launch.status")
                    if current.get("sessionId") == session:
                        await client.call("vn.launch.stop", {"closeGame": True, "reason": "host_io_acceptance"})
                    status = await client.call("vn.status")
                    report["checks"]["session_stopped_and_microphone_off"] = status.get("status") == "stopped" and not status["inputs"]["voice"]["enabled"]
            except Exception as exc:
                report["cleanup_error"] = str(exc)
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17779)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--output", default="output/diagnostics/vn-host-io")
    parser.add_argument("--checks", nargs="+", choices=("playback", "vision", "microphone"), default=["vision"])
    parser.add_argument("--audio-cache-dir", help="Fresh local GPT-SoVITS cache directory used by this test host")
    parser.add_argument("--settle-seconds", type=float, default=10)
    asyncio.run(run(parser.parse_args()))
