from __future__ import annotations

import tempfile
from pathlib import Path

from server.auip_app_launcher import build_app_launch_url, parse_app_launch_url
from server.auip_contract import AuipProtocolError


def test_launch_descriptor_keeps_authority_in_a_local_single_use_fragment() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        entry = Path(tmp) / "gomoku.html"
        entry.write_text("<!doctype html><title>Gomoku</title>", encoding="utf-8")
        launch_url = build_app_launch_url(
            entry_path=str(entry),
            websocket_url="ws://127.0.0.1:18888/auip/ws",
            attach_ticket="one-time-ticket",
            expires_at=1234567890.0,
        )

    assert launch_url.startswith("file:")
    assert "?" not in launch_url
    assert "one-time-ticket" not in launch_url
    decoded = parse_app_launch_url(launch_url)
    assert decoded["webSocketUrl"] == "ws://127.0.0.1:18888/auip/ws"
    assert decoded["attachTicket"] == "one-time-ticket"
    assert decoded["expiresAt"] == 1234567890.0


def test_launcher_refuses_non_local_or_wrong_websocket_surfaces() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        entry = Path(tmp) / "app.html"
        entry.write_text("<!doctype html>", encoding="utf-8")
        for endpoint in (
            "wss://example.com/auip/ws",
            "ws://127.0.0.1:17777/ws",
            "http://127.0.0.1:17777/auip/ws",
        ):
            try:
                build_app_launch_url(
                    entry_path=str(entry),
                    websocket_url=endpoint,
                    attach_ticket="ticket",
                    expires_at=123.0,
                )
            except AuipProtocolError as error:
                assert error.code == "invalid_auip_endpoint"
            else:
                raise AssertionError(f"expected refusal for {endpoint}")


if __name__ == "__main__":
    test_launch_descriptor_keeps_authority_in_a_local_single_use_fragment()
    test_launcher_refuses_non_local_or_wrong_websocket_surfaces()
    print("ok: AUIP launcher exposes one local, fragment-bound Attach descriptor")
