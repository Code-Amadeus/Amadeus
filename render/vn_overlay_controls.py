"""Native VN controls use the same session API and state as the desktop page."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import queue
import threading
import time
from urllib.parse import urlsplit
import uuid

from websockets.sync.client import connect

from server.local_auth import AUTH_TOKEN_HEADER, LocalAuthPolicy


class VNOverlayControls:
    """One connection, one outstanding change; reconnect never replays a click."""

    def __init__(self, url: str):
        target = urlsplit(url)
        if target.scheme != "ws" or target.hostname not in {"127.0.0.1", "localhost"} or target.path != "/ws":
            raise ValueError("VN controls require the local backend /ws endpoint")
        self.url = url
        auth = LocalAuthPolicy.from_environment(os.environ)
        self._headers = {AUTH_TOKEN_HEADER: auth.token} if auth.required else {}
        self._lock = threading.Lock()
        self._state = {"connected": False, "inputs": {}, "pending": False, "error": ""}
        self._commands = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._socket = None
        self._thread = threading.Thread(target=self._run, name="vn-overlay-controls", daemon=True)
        self._thread.start()

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy(self._state)

    def set_inputs(self, session_id: str, **changes) -> bool:
        with self._lock:
            if (not self._state["connected"] or self._state["pending"] or not session_id
                    or session_id != self._state["inputs"].get("session_id")):
                return False
            self._state.update(pending=True, error="")
            self._commands.put_nowait({"type": "req", "id": uuid.uuid4().hex, "method": "vn.input.set",
                                      "params": {**changes, "session_id": session_id}})
            return True

    def close(self):
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        self._thread.join(timeout=4)

    def _run(self):
        while not self._stop.is_set():
            try:
                with connect(self.url, additional_headers=self._headers, open_timeout=3, close_timeout=1) as ws:
                    self._socket = ws
                    status_request = json.dumps({"type": "req", "id": "status", "method": "vn.status", "params": {}})
                    ws.send(status_request)
                    pending_id, deadline = "", time.monotonic() + 30
                    while not self._stop.is_set():
                        try:
                            command = self._commands.get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            pending_id, deadline = command["id"], time.monotonic() + 30
                            ws.send(json.dumps(command))
                        if time.monotonic() > deadline:
                            raise TimeoutError("VN control request timed out")
                        try:
                            message = json.loads(ws.recv(timeout=.1))
                        except TimeoutError:
                            continue
                        params = message.get("params") or {}
                        with self._lock:
                            is_status = (message.get("type") == "evt" and message.get("method") == "vn.status"
                                         or message.get("type") == "res" and message.get("id") == "status")
                            if is_status and isinstance(params.get("inputs"), dict):
                                previous_session = self._state["inputs"].get("session_id")
                                if previous_session and previous_session != params["inputs"].get("session_id"):
                                    self._state["error"] = ""
                                self._state.update(connected=True, inputs=params["inputs"])
                                if not pending_id:
                                    deadline = float("inf")
                            if message.get("type") == "res" and message.get("id") == pending_id:
                                self._state.update(pending=False, error=str(params.get("error") or ""))
                                pending_id, deadline = "", time.monotonic() + 30
                                # Read the current session after completion, including rejected changes.
                                ws.send(status_request)
            except Exception as exc:
                with self._lock:
                    self._state["error"] = str(exc)
            finally:
                self._socket = None
                with self._lock:
                    self._state.update(connected=False, pending=False, inputs={})
                    while not self._commands.empty():
                        self._commands.get_nowait()
            self._stop.wait(1)
