"""Send one backend WebSocket request from a batch file.

Usage:
  python tools/ws_request.py tts.interrupt
  python tools/ws_request.py chat.abort "{}"
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.error
import urllib.request


def _http_request(method: str, params: dict) -> dict:
    payload = json.dumps(
        {
            "type": "req",
            "id": method,
            "method": method,
            "params": params,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:17777/ws",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        return {"status": resp.status, "body": resp.read().decode("utf-8", errors="replace")}


async def _websocket_request(method: str, params: dict) -> dict:
    try:
        import websockets
    except ModuleNotFoundError as exc:
        raise RuntimeError("The current Python environment does not have the websockets package.") from exc

    async with websockets.connect("ws://127.0.0.1:17777/ws") as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "req",
                    "id": method,
                    "method": method,
                    "params": params,
                },
                ensure_ascii=False,
            )
        )
        return json.loads(await ws.recv())


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python tools/ws_request.py <method> [params-json]")
        return 2

    method = sys.argv[1]
    params = {}
    if len(sys.argv) >= 3 and sys.argv[2].strip():
        params = json.loads(sys.argv[2])

    try:
        result = asyncio.run(_websocket_request(method, params))
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
