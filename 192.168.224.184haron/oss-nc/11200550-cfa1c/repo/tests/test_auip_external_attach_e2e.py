from __future__ import annotations

import asyncio

from tools.e2e_auip_external_attach import run_simulation


def test_external_app_crosses_the_real_restricted_websocket_boundary() -> None:
    report = asyncio.run(run_simulation())
    assert report == {
        "ok": True,
        "transport": "real_websocket",
        "session": "closed",
        "final_revision": 2,
        "verified_action": "counter.increment",
        "participant_decisions": 1,
        "terminal_event": "counter.finished",
        "close_reason": "simulation_complete",
    }


if __name__ == "__main__":
    test_external_app_crosses_the_real_restricted_websocket_boundary()
    print("ok: AUIP external app completes the real restricted-WebSocket journey")
