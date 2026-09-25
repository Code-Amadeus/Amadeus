"""The native child authenticates after bootstrap removes inherited credentials."""
import asyncio
from http import HTTPStatus
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import websockets

from server.handlers.vn_launch_handler import VNLaunchHandler
from server.local_auth import (
    AUTH_MODE_ENV, AUTH_TOKEN_ENV, INSTANCE_NONCE_ENV,
    LocalAuthPolicy, clear_inherited_auth_environment,
)


def configured_handler(tmp_path, auth, url="ws://127.0.0.1:17779/ws"):
    handler = VNLaunchHandler()
    handler.configure(tmp_path, runtime_start=AsyncMock(), runtime_stop=AsyncMock(),
                      runtime_status=AsyncMock(), runtime_line=AsyncMock(),
                      backend_url=url, auth_policy=auth)
    handler._manager._publish_status = AsyncMock()
    return handler


def test_launched_control_child_authenticates_after_parent_environment_is_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv(AUTH_MODE_ENV, "required")
    monkeypatch.setenv(AUTH_TOKEN_ENV, "test-only-token-" + "a" * 32)
    monkeypatch.setenv(INSTANCE_NONCE_ENV, "test-only-instance")
    auth = LocalAuthPolicy.from_environment(os.environ)
    clear_inherited_auth_environment(os.environ)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))

    # Exercise the actual child launch and native control client without a GUI.
    helper = tmp_path / "tools/vn_portrait_overlay_lite.py"
    helper.parent.mkdir()
    helper.write_text('''
import sys
import time
from render.vn_overlay_controls import VNOverlayControls
client = VNOverlayControls(sys.argv[sys.argv.index("--backend-url") + 1])
try:
    requested = False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = client.snapshot()
        if state["connected"] and not requested:
            requested = client.set_inputs("test-session", voice=True)
        if requested and not state["pending"] and state["inputs"]["voice"]["enabled"]:
            break
        time.sleep(.01)
    else:
        sys.exit(2)
finally:
    client.close()
''', encoding="utf-8")

    async def run():
        changes = []
        inputs = {"session_id": "test-session", "voice": {"enabled": False}}

        async def authenticate(connection, request):
            if auth.authenticate(request.headers) is None:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "Authentication required")

        async def connected(ws):
            async for raw in ws:
                message = json.loads(raw)
                if message["method"] == "vn.input.set":
                    changes.append(message["params"])
                    inputs["voice"]["enabled"] = message["params"]["voice"]
                await ws.send(json.dumps({"type": "res", "id": message["id"], "params": {"inputs": inputs}}))

        async with websockets.serve(connected, "127.0.0.1", 0, process_request=authenticate) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(url):
                    pytest.fail("Unauthenticated connections must still be rejected")
            handler = configured_handler(tmp_path, auth, url)
            manager = handler._manager
            try:
                with patch("server.vn_launch_manager._http_health", side_effect=[False, True]):
                    await manager._launch_overlay({"overlayHelper": str(helper)}, {})
                assert await asyncio.to_thread(manager._overlay_proc.wait, 10) == 0
                assert changes == [{"session_id": "test-session", "voice": True}]
                assert auth.token not in json.dumps(manager._state)
                assert auth.token not in " ".join(manager._overlay_proc.args)
                assert not any(name in os.environ for name in (AUTH_MODE_ENV, AUTH_TOKEN_ENV, INSTANCE_NONCE_ENV))
            finally:
                proc = manager._overlay_proc
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    await asyncio.to_thread(proc.wait, 5)
    asyncio.run(run())


def test_game_child_does_not_inherit_desktop_credentials(tmp_path, monkeypatch):
    auth = LocalAuthPolicy(mode="required", token="test-only-token-" + "a" * 32,
                           instance_nonce="test-only-instance")
    manager = configured_handler(tmp_path, auth)._manager
    for name, value in ((AUTH_MODE_ENV, "required"), (AUTH_TOKEN_ENV, auth.token),
                        (INSTANCE_NONCE_ENV, auth.instance_nonce)):
        monkeypatch.setenv(name, value)
    with patch("server.vn_launch_manager.subprocess.Popen") as spawn:
        manager._spawn(["game.exe"], cwd=tmp_path, hidden=False)
    assert not any(name in spawn.call_args.kwargs["env"]
                   for name in (AUTH_MODE_ENV, AUTH_TOKEN_ENV, INSTANCE_NONCE_ENV))
    assert os.environ[AUTH_TOKEN_ENV] == auth.token


def test_credentials_cannot_be_forwarded_to_an_external_overlay(tmp_path):
    helper = tmp_path / "external.py"
    helper.touch()
    auth = LocalAuthPolicy(mode="required", token="test-only-token-" + "a" * 32,
                           instance_nonce="test-only-instance")
    manager = configured_handler(tmp_path, auth)._manager
    manager._spawn = Mock()
    with patch("server.vn_launch_manager._http_health", return_value=False):
        with pytest.raises(ValueError, match="repository-owned"):
            asyncio.run(manager._launch_overlay({"overlayHelper": str(helper)}, {}))
    manager._spawn.assert_not_called()
