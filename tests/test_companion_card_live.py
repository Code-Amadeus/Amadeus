"""Live preflight: the companion card really starts, answers, and is cleaned up.

This is the runnable half of the manual acceptance. It runs the repository-owned
Tk card through CompanionCardHost on a scratch port, asserts the health,
reaction, visibility and adoption contract, stops it, and proves the port is
gone. The microphone and on-screen checks still need a human.

Run it explicitly (it opens a real window):

    python -m pytest tests/test_companion_card_live.py -q -s
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from pathlib import Path

from server.companion_runtime import CompanionCardHost
from server.local_auth import LocalAuthPolicy

ROOT = Path(__file__).resolve().parents[1]
PORT = 8799


def _get(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict, timeout: float = 3.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _await_visible(expected: bool, timeout: float = 3.0) -> None:
    """The card applies queued controls on its own Tk loop, not in the POST."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = _get(f"http://127.0.0.1:{PORT}/health")
        if last.get("visible") is expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"card visibility never became {expected}: {last}")


def test_the_card_starts_answers_and_leaves_no_process_behind():
    async def run() -> None:
        host = CompanionCardHost(
            ROOT, backend_url="", auth_policy=LocalAuthPolicy.disabled(), port=PORT,
        )
        try:
            status = await host.ensure_running()
            assert status["running"] is True, status
            assert status["owned"] is True, status
            assert status["url"] == f"http://127.0.0.1:{PORT}/reaction", status

            health = _get(f"http://127.0.0.1:{PORT}/health")
            assert health.get("status") == "ok"
            assert health.get("companion_controls") is True

            _post(f"http://127.0.0.1:{PORT}/reaction", {
                "source": "vn_playback", "sentence_id": "smoke-1", "speaking": True,
                "display_text": "Companion-only 启动自检。", "emotion": "thinking",
            })
            _post(f"http://127.0.0.1:{PORT}/reaction", {
                "source": "vn_playback", "sentence_id": "smoke-1", "speaking": False,
            })

            await asyncio.to_thread(_post, f"http://127.0.0.1:{PORT}/visibility", {"visible": False})
            await asyncio.to_thread(_await_visible, False)
            await asyncio.to_thread(_post, f"http://127.0.0.1:{PORT}/focus", {})
            await asyncio.to_thread(_await_visible, True)

            adopted = await CompanionCardHost(
                ROOT, backend_url="", auth_policy=LocalAuthPolicy.disabled(), port=PORT,
            ).ensure_running()
            assert adopted["owned"] is False, adopted
        finally:
            await host.stop()

        await asyncio.sleep(0.4)
        try:
            _get(f"http://127.0.0.1:{PORT}/health", timeout=1.0)
        except Exception:
            return
        raise AssertionError("the card still answers after stop()")

    asyncio.run(run())
