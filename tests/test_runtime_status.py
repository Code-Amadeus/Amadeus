"""Read-only runtime readiness aggregation tests."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.runtime_status import RuntimeStatusCollector


def _collector_with_fake_sections() -> RuntimeStatusCollector:
    collector = RuntimeStatusCollector()
    collector._server = lambda: {}
    collector._session = lambda: {}
    collector._chat = lambda: {}
    collector._tts = lambda: {"inferencer_loaded": True}
    collector._playback = lambda: {"initialized": True}
    collector._asr = lambda: {"manager_loaded": True, "backend_ready": True}
    collector._wake = lambda: {
        "initialized": True,
        "running": True,
        "status": "listening",
        "error": "",
    }
    collector._mic = lambda: {}
    collector._aec = lambda: {}
    collector._provider = lambda: {"configured": True}
    collector._coordinator = lambda: {}
    collector._wallpaper_handler = SimpleNamespace(
        _wallpaper_host=SimpleNamespace(_ready=True)
    )
    collector._provider_runtime_getter = lambda: SimpleNamespace(
        list_providers=lambda: ["browser", "locus"],
        _request_preparer=lambda request: request,
    )
    collector._work_ledger_getter = lambda: SimpleNamespace(
        _subscribed=True,
        store=object(),
    )
    return collector


def test_ready_matrix_reports_all_existing_singletons():
    ready = _collector_with_fake_sections().collect()["ready"]

    assert ready == {
        "tts": True,
        "asr": True,
        "wake": True,
        "wallpaper_bridge": True,
        "provider_runtime": True,
        "work_ledger": True,
        "overall": True,
    }


def test_failed_peek_degrades_to_false_without_breaking_snapshot():
    collector = _collector_with_fake_sections()

    def explode():
        raise RuntimeError("peek failed")

    collector._provider_runtime_getter = explode
    snapshot = collector.collect()

    assert snapshot["ready"]["provider_runtime"] is False
    assert snapshot["ready"]["overall"] is False
    assert "ts" in snapshot


def test_playback_activity_is_distinct_from_an_open_audio_stream():
    collector = RuntimeStatusCollector()
    ready = __import__("asyncio").Event()
    ready.set()
    manager = SimpleNamespace(
        player=SimpleNamespace(is_playing=True),
        player_is_ready=ready,
        playback_epoch=4,
        pending_audio={},
        next_seq_to_play=2,
    )
    collector._playback_manager_getter = lambda: manager

    idle = collector._playback()
    assert idle["stream_open"] is True
    assert idle["is_playing"] is False

    ready.clear()
    active = collector._playback()
    assert active["stream_open"] is True
    assert active["is_playing"] is True


def test_server_status_exposes_the_frozen_process_code_identity():
    collector = RuntimeStatusCollector()
    collector._port = 17777
    collector._code_identity = {
        "commit_sha": "abc123",
        "workspace_dirty": True,
        "workspace_fingerprint": "fingerprint-1",
        "source": "launcher",
    }

    server = collector._server()

    assert server["port"] == 17777
    assert server["code_identity"] == collector._code_identity


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all runtime status tests passed")


if __name__ == "__main__":
    _main()
