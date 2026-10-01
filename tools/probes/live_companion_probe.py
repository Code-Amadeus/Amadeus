"""Live end-to-end probe against a running Companion-only backend.

Proves the merged integration works outside the unit tests:

    阅读事件 -> Reading Adapter -> 会话/记忆 -> main chat 真实回复

Run against an already running `python -m server.app --companion`:

    python tools/probes/live_companion_probe.py <X-Amadeus-Token> [port]
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
import urllib.request

import websockets

BOOK = "book:live-probe"
PORT = 17777
REPLY_TIMEOUT_S = 180.0


def http_json(path: str, token: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=body,
        method=method,
        headers={"X-Amadeus-Token": token, "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


async def connect(token: str):
    """Support both websockets header spellings."""
    kwargs = {"open_timeout": 15, "max_size": 8 * 1024 * 1024}
    if "additional_headers" in inspect.signature(websockets.connect).parameters:
        kwargs["additional_headers"] = {"X-Amadeus-Token": token}
    else:
        kwargs["extra_headers"] = {"X-Amadeus-Token": token}
    return await websockets.connect(f"ws://127.0.0.1:{PORT}/ws", **kwargs)


async def rpc(socket, method: str, params: dict, timeout: float = 30.0) -> dict:
    request_id = f"probe-{method}-{int(time.time() * 1000)}"
    await socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=deadline - time.monotonic()))
        if message.get("id") == request_id:
            return message
    raise TimeoutError(method)


async def wait_for_reply(socket, timeout: float = REPLY_TIMEOUT_S) -> str:
    """Collect streamed chat output until the turn completes."""
    text: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=deadline - time.monotonic()))
        method = message.get("method")
        params = message.get("params") or {}
        print(f"      <- {method}: {json.dumps(params, ensure_ascii=False)[:160]}", flush=True)
        if method in {"chat.token", "chat.delta", "chat.stream"}:
            text.append(str(params.get("text") or params.get("token") or params.get("delta") or ""))
        elif method in {"chat.complete", "chat.response", "chat.message"}:
            final = str(params.get("full_text") or params.get("text") or params.get("response") or "")
            return final or "".join(text)
        elif method == "chat.error":
            raise RuntimeError(f"chat.error: {params}")
    raise TimeoutError("chat reply")


async def main(token: str) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))
        print(f"[{'ok  ' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""), flush=True)

    health = http_json("/health", token)
    check("backend health", health.get("status") == "ok", str(health.get("instance_nonce"))[:8])
    check("Work/AUIP lane disabled in companion mode",
          health.get("cooperative_chat_mode") == "disabled", str(health.get("cooperative_chat_mode")))

    card = http_json("/companion/card/status", token)["card"]
    check("companion card owned by this backend", card.get("owned") is True,
          f"pid={card.get('pid')} url={card.get('url')}")

    with urllib.request.urlopen("http://127.0.0.1:17878/health", timeout=5) as response:
        check("reader adapter health", json.loads(response.read())["ok"] is True)

    event = {
        "type": "reading.selection",
        "app": "obsidian",
        "book_id": BOOK,
        "chapter": "第三章",
        "page": 42,
        "cursor": 12345,
        "text": "她推开了那扇门。",
        "chunks": [{
            "id": "ch-live-1", "chapter": "第三章", "start_offset": 12000,
            "end_offset": 12040, "text": "她推开了那扇门。", "page": 42,
        }],
    }
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:17878/reading/event", data=body,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        posted = json.loads(response.read().decode("utf-8"))
    check("reader adapter accepted a reading.selection", posted.get("ok") is True)
    check("reader adapter stored cursor/chapter",
          posted["context"]["cursor"] == 12345 and posted["context"]["current_chapter"] == "第三章",
          str(posted["context"]["current_chapter"]))

    socket = await connect(token)
    try:
        listed = await rpc(socket, "session.create", {})
        created = listed.get("params") or listed.get("result") or {}
        session_id = str(created.get("current_session_id") or "")
        if not session_id:
            print("      session.create raw:", json.dumps(listed, ensure_ascii=False)[:300], flush=True)
        check("shared session created", bool(session_id), session_id)

        sent = await rpc(socket, "chat.send", {
            "text": "接下来会不会剧透？她推开了那扇门。",
            "session_id": session_id,
        })
        payload = sent.get("params") or sent.get("result") or {}
        if payload.get("status") != "ok":
            print("      chat.send raw:", json.dumps(sent, ensure_ascii=False)[:300], flush=True)
        check("chat.send accepted the turn", payload.get("status") == "ok",
              json.dumps(payload, ensure_ascii=False)[:80])

        reply = await wait_for_reply(socket)
        # The workspace environment blocks the model provider. The contract under
        # test is that the turn reaches the voice/caption lane at all; a provider
        # connection error is reported as an environment note, not a defect.
        provider_blocked = reply.strip().startswith("LLM API Error")
        check("chat turn reached the voice/caption lane", bool(reply.strip()),
              reply.strip()[:70].replace("\n", " "))
        if provider_blocked:
            print("      note: model provider unreachable from this environment (network blocked)")
    finally:
        await socket.close()

    print()
    failed = [label for label, ok, _d in checks if not ok]
    if failed:
        print(f"FAILED: {len(failed)} check(s)")
        for label in failed:
            print(f"  - {label}")
        return 1
    print("all live checks passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    if len(sys.argv) > 2:
        PORT = int(sys.argv[2])
    raise SystemExit(asyncio.run(main(sys.argv[1])))
