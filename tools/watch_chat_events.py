from __future__ import annotations

import asyncio
import json
import sys
import time

import websockets


WATCH_METHODS = {
    "chat.token",
    "chat.complete",
    "chat.interrupted",
    "chat.error",
    "tts.status",
    "tts.sentence_start",
    "tts.sentence_end",
    "tts.turn_complete",
    "asr.recognized",
}


def compact(value: object, limit: int = 160) -> str:
    text = str(value or "").replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "..."


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:17777/ws"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0
    deadline = time.monotonic() + seconds
    print(f"[watch] connecting {url} for {seconds:.0f}s", flush=True)
    async with websockets.connect(url) as ws:
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get("type") != "evt":
                continue
            method = str(msg.get("method") or "")
            if method not in WATCH_METHODS:
                continue
            params = msg.get("params") or {}
            if method == "chat.token":
                print(
                    f"[evt] chat.token turn={params.get('turn_id','')} "
                    f"text={compact(params.get('token'))}",
                    flush=True,
                )
            elif method == "chat.complete":
                print(
                    f"[evt] chat.complete turn={params.get('turn_id','')} "
                    f"text={compact(params.get('full_text'))}",
                    flush=True,
                )
            elif method == "chat.interrupted":
                print(
                    f"[evt] chat.interrupted turn={params.get('turn_id','')} "
                    f"text={compact(params.get('text'))} completed={compact(params.get('completed_text'))}",
                    flush=True,
                )
            elif method == "asr.recognized":
                print(
                    f"[evt] asr.recognized source={params.get('source','')} "
                    f"final={params.get('is_final')} text={compact(params.get('text'))}",
                    flush=True,
                )
            elif method in {"tts.sentence_start", "tts.sentence_end"}:
                print(
                    f"[evt] {method} id={params.get('sentence_id','')} "
                    f"text={compact(params.get('text'))}",
                    flush=True,
                )
            else:
                print(f"[evt] {method} {json.dumps(params, ensure_ascii=False)}", flush=True)
    print("[watch] done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
