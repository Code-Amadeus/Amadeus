"""Companion-only routing: card ownership, credential hand-off and speech surface."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from server import companion_runtime
from server import vn_tts_bridge as bridge
from server.local_auth import (
    AUTH_MODE_ENV,
    AUTH_TOKEN_ENV,
    INSTANCE_NONCE_ENV,
    LocalAuthPolicy,
)


TOKEN = "t" * 32
NONCE = "n" * 18


@pytest.fixture(autouse=True)
def isolated_overlay_state(monkeypatch):
    monkeypatch.setattr(bridge, "_TASKS", set())
    monkeypatch.setattr(bridge, "_TASK_META", {})
    monkeypatch.setattr(bridge, "_SENTENCE_META", {})
    yield
    companion_runtime.set_default_overlay_url("")


def _card(**overrides) -> companion_runtime.CompanionCardHost:
    return companion_runtime.CompanionCardHost(
        Path("."),
        backend_url=overrides.pop("backend_url", ""),
        auth_policy=overrides.pop("auth_policy", LocalAuthPolicy.disabled()),
        **overrides,
    )


def _python_path() -> str:
    """A real interpreter path, so the Tk probe has something to accept."""
    return str(Path(sys.executable).resolve())


def test_desktop_credential_environment_passes_the_served_credential():
    captured = companion_runtime.desktop_credential_environment({
        AUTH_MODE_ENV: "required", AUTH_TOKEN_ENV: TOKEN, INSTANCE_NONCE_ENV: NONCE,
    })
    assert captured == {AUTH_MODE_ENV: "required", AUTH_TOKEN_ENV: TOKEN, INSTANCE_NONCE_ENV: NONCE}


def test_desktop_credential_environment_is_empty_without_a_complete_credential():
    assert companion_runtime.desktop_credential_environment({}) == {}
    assert companion_runtime.desktop_credential_environment({AUTH_TOKEN_ENV: TOKEN}) == {}


def test_card_child_environment_carries_the_captured_credential(monkeypatch):
    interpreter = _python_path()
    monkeypatch.setenv("VN_OVERLAY_PYTHON", interpreter)
    host = _card(
        backend_url="ws://127.0.0.1:17777/ws",
        credential_environment={
            AUTH_MODE_ENV: "required", AUTH_TOKEN_ENV: TOKEN, INSTANCE_NONCE_ENV: NONCE,
        },
    )
    environment = host._child_environment()
    assert Path(host._python).is_file()
    assert environment[AUTH_MODE_ENV] == "required"
    assert environment[AUTH_TOKEN_ENV] == TOKEN
    assert environment[INSTANCE_NONCE_ENV] == NONCE


def test_card_child_environment_stays_unauthenticated_without_a_credential(monkeypatch):
    monkeypatch.setenv("VN_OVERLAY_PYTHON", _python_path())
    environment = _card(backend_url="ws://127.0.0.1:17777/ws")._child_environment()
    assert environment[AUTH_MODE_ENV] == "disabled"
    assert AUTH_TOKEN_ENV not in environment


def test_card_status_reports_no_owned_process_before_start():
    status = _card().status()
    assert status["status"] == "not_started"
    assert status["running"] is False
    assert status["owned"] is False
    assert status["url"] == "http://127.0.0.1:8788/reaction"


def test_card_focus_and_visibility_address_the_card_own_endpoints():
    posted = []

    class _Response:
        status = 200

        def read(self, _size):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def fake_urlopen(request, timeout=None):
        posted.append((request.full_url, json.loads(request.data.decode("utf-8"))))
        return _Response()

    card_url = "http://127.0.0.1:8788/reaction"
    with patch("server.companion_runtime.request.urlopen", fake_urlopen):
        assert companion_runtime.post_card_focus(card_url) is True
        assert companion_runtime.post_card_visibility(card_url, False) is True
    assert posted == [
        ("http://127.0.0.1:8788/focus", {}),
        ("http://127.0.0.1:8788/visibility", {"visible": False}),
    ]


def test_only_the_owning_launch_publishes_a_speech_surface():
    _card()._publish_surface_url()
    assert bridge._companion_overlay_url() == "http://127.0.0.1:8788/reaction"
    companion_runtime.set_default_overlay_url("")
    assert bridge._companion_overlay_url() == ""


def test_companion_speech_uses_the_card_when_the_payload_names_no_overlay():
    async def run():
        pending: asyncio.Queue = asyncio.Queue()
        companion_runtime.set_default_overlay_url("http://127.0.0.1:8788/reaction")
        submitted = bridge.submit_vn_tts(
            {"text": "先看看这里的线索。", "display_text": "先看看这里的线索。"},
            pending_sentence_items=pending,
        )
        assert submitted["status"] == "queued"
        metadata = [meta for meta in bridge._TASK_META.values()][0]
        assert metadata["overlay_url"] == "http://127.0.0.1:8788/reaction"
        for task in list(bridge._TASKS):
            task.cancel()
        await asyncio.gather(*bridge._TASKS, return_exceptions=True)

    asyncio.run(run())


def test_an_explicit_session_overlay_url_still_wins():
    async def run():
        pending: asyncio.Queue = asyncio.Queue()
        companion_runtime.set_default_overlay_url("http://127.0.0.1:8788/reaction")
        bridge.submit_vn_tts(
            {"text": "VN line", "overlay_url": "http://127.0.0.1:9999/reaction"},
            pending_sentence_items=pending,
        )
        metadata = [meta for meta in bridge._TASK_META.values()][0]
        assert metadata["overlay_url"] == "http://127.0.0.1:9999/reaction"
        for task in list(bridge._TASKS):
            task.cancel()
        await asyncio.gather(*bridge._TASKS, return_exceptions=True)

    asyncio.run(run())


def test_companion_mode_turns_off_the_work_and_auip_lanes_only():
    app_source = (Path(companion_runtime.__file__).resolve().parents[1] / "server" / "app.py").read_text(
        encoding="utf-8"
    )
    assert "cooperative_chat_enabled = bool(settings.COOPERATIVE_CHAT_ENABLED) and not companion_only" in app_source
    assert "if bool(settings.AUIP_NARRATION_ENABLED) and not companion_only:" in app_source
