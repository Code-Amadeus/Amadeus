"""Bounded process client; recovery never resends an admitted synthesis request."""
from __future__ import annotations

import base64
import json
import math
import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from config.subprocess_environment import isolated_child_environment
from config.process_tree import ProcessTree

MAX_LINE = 8 * 1024 * 1024
MAX_CHUNK = 1024 * 1024
READY_TIMEOUT = 30.0
REQUEST_TIMEOUT = 60.0
SHUTDOWN_TIMEOUT = 5.0
RESTART_BACKOFF = (1.0, 5.0, 30.0)


class ExtensionUnavailable(RuntimeError):
    pass


class ExtensionProtocolError(ExtensionUnavailable):
    """Terminal connection/registration defect, distinct from crash recovery."""


@dataclass
class Pending:
    inbox: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=8))
    seq: int = 0
    sample_rate: int | None = None
    duration_ms: float = 0.0
    error: str = ""
    must_cancel: bool = False
    contribution_id: str = ""
    dispatched: bool = False
    responded: bool = False
    finished: threading.Event = field(default_factory=threading.Event)


class RunnerClient:
    def __init__(self, record: dict[str, Any], *, on_state=None) -> None:
        self.record = record
        self.manifest = record["manifest"]
        self.on_state = on_state or (lambda: None)
        self.state = "starting"
        self.detail = "Runner is starting"
        self.health = {item["id"]: ("starting", "Runner is starting") for item in self.manifest["contributes"].get("voice_backend", [])}
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._closing = threading.Event()
        self._shutdown_complete = threading.Event()
        self._pending: dict[str, Pending] = {}
        self._control: dict[str, queue.Queue] = {}
        self._cancelled: dict[str, float] = {}
        self._process: subprocess.Popen | None = None
        self._tree: ProcessTree | None = None
        self._thread: threading.Thread | None = None
        self._token = 0
        self._restarts: list[float] = []
        self._registration_checked = False
        self._terminal_error: ExtensionProtocolError | None = None
        self._last_traffic = time.monotonic()

    def _redact(self, text: str) -> str:
        for value in self.record.get("secrets", {}).values():
            if isinstance(value, str) and value:
                text = text.replace(value, "[redacted]")
        return text[:240]

    def _state(self, state: str, detail: str) -> None:
        with self._lock:
            self.state, self.detail = state, self._redact(detail)
        self.on_state()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._state("starting", "Runner is starting")
            self._thread = threading.Thread(target=self._supervise, daemon=True, name=f"extension-{self.manifest['id']}")
            self._thread.start()

    def _initialize(self) -> dict[str, Any]:
        data = Path(self.record["data_path"])
        paths = {"package": self.record["package_path"], "data": str(data), "cache": str(data / "cache"), "temp": str(data / "temp")}
        for key in ("data", "cache", "temp"):
            Path(paths[key]).mkdir(parents=True, exist_ok=True)
        return {"protocol": "amadeus.extension-rpc/v0", "host": {"name": "amadeus", "version": "0.16.1", "host_api": "0.1"},
                "package": {"id": self.manifest["id"], "version": self.manifest["version"]},
                "activation": {"id": uuid.uuid4().hex, "generation": self._token + 1},
                "config": self.record.get("config", {}), "secrets": self.record.get("secrets", {}), "grants": [],
                "paths": paths, "locale": "en-US"}

    def _launch(self) -> None:
        ready_expires = time.monotonic() + READY_TIMEOUT
        params = self._initialize()
        environment = isolated_child_environment()
        kwargs: dict[str, Any] = {"creationflags": subprocess.CREATE_NO_WINDOW | 0x4} if os.name == "nt" else {"start_new_session": True}  # CREATE_SUSPENDED
        if self._stop.is_set():
            raise ExtensionUnavailable("Runner stopped")
        process = subprocess.Popen([self.record["python_path"], "-I", "-X", "utf8", "-u", "-m", "amadeus_extension.runner", "--entry", self.manifest["runtime"]["entry"]],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=params["paths"]["data"], env=environment, **kwargs)
        tree = None
        try:
            tree = ProcessTree(process)
            tree.resume()
        except Exception:
            if tree:
                tree.terminate()
            else:
                process.kill()
                process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
            raise
        with self._lock:
            self._process, self._tree = process, tree
            self._token += 1
            token = self._token
            self._registration_checked = False
            self._terminal_error = None
            self._ready.clear()
            self.health = {key: ("starting", "Runner is starting") for key in self.health}
        if self._stop.is_set():
            self._dispose("Runner stopped")
            raise ExtensionUnavailable("Runner stopped")
        threading.Thread(target=self._read, args=(process, token), daemon=True, name="extension-wire").start()
        threading.Thread(target=self._stderr, args=(process, token), daemon=True, name="extension-stderr").start()
        result = self._rpc("initialize", params, timeout=READY_TIMEOUT)
        expected = sorted(("voice_backend", item["id"], item["contract"]) for item in self.manifest["contributes"].get("voice_backend", []))
        try:
            actual = sorted((item["kind"], item["id"], item["contract"]) for item in result["contributions"])
        except (KeyError, TypeError):
            raise ExtensionProtocolError("Runner registration mismatch") from None
        if actual != expected:
            raise ExtensionProtocolError("Runner registration mismatch")
        self._registration_checked = True
        if not self._ready.wait(max(0, ready_expires - time.monotonic())):
            raise ExtensionUnavailable("Runner ready timeout")
        self._state("active", "Runner is ready")

    def _supervise(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self._launch()
                    last_ping = time.monotonic()
                    while not self._stop.wait(0.1):
                        if self._terminal_error is not None:
                            raise self._terminal_error
                        process = self._process
                        if process is None or process.poll() is not None:
                            raise ExtensionUnavailable("Runner exited")
                        with self._lock:
                            idle = not self._pending
                        if idle and time.monotonic() - max(last_ping, self._last_traffic) >= 15:
                            self._rpc("$/ping", {}, timeout=5)
                            last_ping = time.monotonic()
                    break
                except Exception as caught:
                    if self._stop.is_set():
                        break
                    exc = self._terminal_error if self._terminal_error is not None else caught
                    detail = str(exc) if isinstance(exc, ExtensionUnavailable) else "Runner could not start"
                    self._state("error", detail)
                    self._dispose(detail)
                    if isinstance(exc, ExtensionProtocolError):
                        break
                    now = time.monotonic()
                    self._restarts = [stamp for stamp in self._restarts if now - stamp < 600]
                    if len(self._restarts) >= len(RESTART_BACKOFF):
                        self._state("error", detail + "; restart budget exhausted")
                        break
                    delay = RESTART_BACKOFF[len(self._restarts)]
                    self._restarts.append(now)
                    if self._stop.wait(delay):
                        break
                    self._state("starting", "Runner is recovering; failed utterances are not retried")
        finally:
            if self._closing.is_set():
                self._shutdown_complete.wait(SHUTDOWN_TIMEOUT)
            self._dispose("Runner stopped")

    def _send(self, method: str, params: dict[str, Any], request_id: str | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        raw = (json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        if len(raw) > MAX_LINE:
            raise ExtensionUnavailable("extension request exceeds wire limit")
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                raise ExtensionUnavailable("Runner is unavailable")
            try:
                process.stdin.write(raw)
                process.stdin.flush()
            except (OSError, ValueError):
                raise ExtensionUnavailable("Runner connection closed") from None

    def _rpc(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        request_id = uuid.uuid4().hex
        inbox: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._control[request_id] = inbox
        try:
            self._send(method, params, request_id)
            try:
                reply = inbox.get(timeout=timeout)
            except queue.Empty:
                raise ExtensionUnavailable(f"Runner {method} timeout") from None
            if "error" in reply:
                error = reply["error"]
                if not isinstance(error, dict):
                    raise ExtensionProtocolError("Runner error response is invalid")
                error_type = ExtensionProtocolError if isinstance(error, dict) and error.get("code") in {-32700, -32600, -32601, -32602} else ExtensionUnavailable
                raise error_type(self._redact(error.get("message", "Runner request failed")))
            return reply.get("result")
        finally:
            with self._lock:
                self._control.pop(request_id, None)

    def _read(self, process: subprocess.Popen, token: int) -> None:
        try:
            while process.stdout is not None:
                line = process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    raise ValueError("oversized Runner message")
                if not line.endswith(b"\n"):
                    # EOF in a frame is a broken connection, e.g. a crash mid-write.
                    break
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError("invalid Runner message")
                if token != self._token:
                    return
                self._message(message)
        except (ValueError, TypeError, KeyError, AttributeError, queue.Full):
            if token == self._token and not self._stop.is_set():
                self._terminal_error = ExtensionProtocolError("Runner protocol failed")
                self._state("error", "Runner protocol failed")
                if process.poll() is None:
                    process.kill()
        except OSError:
            pass
        finally:
            if token == self._token:
                self._fail_pending("Runner connection closed")

    def _stderr(self, process: subprocess.Popen, token: int) -> None:
        # Do not retain arbitrary package logs or credentials. Drain to prevent deadlock.
        if process.stderr:
            while process.stderr.read(4096):
                pass

    def _message(self, message: dict[str, Any]) -> None:
        self._last_traffic = time.monotonic()
        request_id = message.get("id")
        if request_id is not None and "method" in message:
            # No Host service is granted in this experiment.
            raw = (json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Host method not available"}}) + "\n").encode()
            with self._write_lock:
                if self._process and self._process.stdin:
                    self._process.stdin.write(raw)
                    self._process.stdin.flush()
            return
        if request_id is not None:
            with self._lock:
                control = self._control.get(request_id)
                pending = self._pending.get(request_id)
                if pending is not None:
                    pending.responded = True
                self._cancelled.pop(request_id, None)
            if control:
                control.put_nowait(message)
            elif pending:
                if "error" in message:
                    pending.error = "TTS contribution failed"
                else:
                    result = message.get("result")
                    duration = result.get("duration_ms") if isinstance(result, dict) else None
                    if (not isinstance(result, dict) or pending.seq == 0 or result.get("chunks") != pending.seq or
                            type(duration) not in {int, float} or not math.isfinite(duration) or
                            abs(duration - pending.duration_ms) > max(1.0, pending.duration_ms * 0.01)):
                        pending.error = "Runner returned invalid TTS completion"
                pending.finished.set()
            return
        method, params = message.get("method"), message.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("invalid Runner parameters")
        if method == "ready":
            self._ready.set()
        elif method == "contribution.health":
            contribution_id = params.get("id")
            if params.get("kind") == "voice_backend" and contribution_id in self.health and params.get("health") in {"ready", "degraded", "error"}:
                with self._lock:
                    self.health[contribution_id] = (params["health"], self._redact(str(params.get("detail", ""))))
                self.on_state()
        elif method == "tts.chunk":
            with self._lock:
                pending = self._pending.get(params.get("request_id"))
            if pending is None or pending.finished.is_set():
                return
            try:
                sample_rate = params["sample_rate"]
                encoded = params["pcm_s16le"]
                if not isinstance(encoded, str) or len(encoded) > (MAX_CHUNK + 2) // 3 * 4:
                    raise ValueError()
                pcm = base64.b64decode(encoded, validate=True)
                if (type(sample_rate) is not int or not 8000 <= sample_rate <= 48000 or
                        type(params["seq"]) is not int or params["seq"] != pending.seq or
                        not 0 < len(pcm) <= MAX_CHUNK or len(pcm) % 2 or
                        pending.sample_rate not in {None, sample_rate}):
                    raise ValueError()
                pending.sample_rate = sample_rate
                pending.seq += 1
                pending.duration_ms += len(pcm) / 2 / sample_rate * 1000
                if pending.duration_ms > 300000:
                    raise ValueError()
                # The pipe reader supplies backpressure to a fast valid producer.
                # Cancellation marks finished, unblocking the reader to drain the ACK.
                while not pending.finished.is_set():
                    try:
                        pending.inbox.put((sample_rate, pcm), timeout=.1)
                        break
                    except queue.Full:
                        continue
            except (ValueError, TypeError, KeyError):
                pending.error = "Runner audio violated the bounded TTS contract"
                pending.must_cancel = True
                pending.finished.set()

    def stream(self, contribution_id: str, *, text: str, language: str, speed: float, deadline_ms: int | None = None) -> Iterator[tuple[int, bytes]]:
        request_id = uuid.uuid4().hex
        pending = Pending(contribution_id=contribution_id)
        with self._lock:
            health, _ = self.health.get(contribution_id, ("error", ""))
            if self.state != "active" or not self._ready.is_set() or not self._registration_checked or health not in {"ready", "degraded"}:
                raise ExtensionUnavailable(self.detail or "extension voice is unavailable")
            if len(self._pending.keys() | self._cancelled.keys()) >= 2:
                raise ExtensionUnavailable("extension voice concurrency limit reached; cancellation cleanup may still be pending")
            self._pending[request_id] = pending
        timeout = deadline_ms / 1000 if deadline_ms is not None else REQUEST_TIMEOUT
        expires = time.monotonic() + timeout
        try:
            with self._lock:
                if pending.finished.is_set():
                    raise ExtensionUnavailable("extension utterance cancelled before dispatch")
                self._send("tts.synthesize", {"contribution_id": contribution_id, "utterance_id": request_id, "text": text,
                           "language": language, "speed": speed, "deadline_ms": max(1, int(timeout * 1000))}, request_id)
                pending.dispatched = True
            while True:
                if pending.error:
                    raise ExtensionUnavailable(pending.error)
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise ExtensionUnavailable("extension voice deadline exceeded")
                try:
                    audio = pending.inbox.get(timeout=min(remaining, 0.1))
                    if pending.error:
                        raise ExtensionUnavailable(pending.error)
                    if time.monotonic() >= expires:
                        raise ExtensionUnavailable("extension voice deadline exceeded")
                    yield audio
                except queue.Empty:
                    if pending.finished.is_set():
                        if pending.error:
                            raise ExtensionUnavailable(pending.error)
                        break
        finally:
            with self._lock:
                if pending.dispatched and not pending.responded:
                    self._cancel(request_id)
                pending.finished.set()
                self._pending.pop(request_id, None)

    def _cancel(self, request_id: str) -> None:
        try:
            with self._lock:
                pending = self._pending.get(request_id)
                if pending is None or pending.responded or request_id in self._cancelled:
                    return
                pending.finished.set()
                self._cancelled[request_id] = time.monotonic()
            self._send("$/cancel", {"id": request_id})
        except ExtensionUnavailable:
            pass

    def cancel_inflight(self, *, contribution_id: str | None = None) -> None:
        """The native TTS interrupt owner closes every admitted package request."""
        with self._lock:
            pending_requests = [(key, pending) for key, pending in self._pending.items()
                                if not pending.finished.is_set() and contribution_id in {None, pending.contribution_id}]
            for _, pending in pending_requests:
                pending.error = "extension utterance cancelled"
                pending.finished.set()
        for request_id, pending in pending_requests:
            if pending.dispatched:
                self._cancel(request_id)

    def _fail_pending(self, detail: str) -> None:
        with self._lock:
            for pending in self._pending.values():
                pending.error = detail
                pending.finished.set()
            for inbox in self._control.values():
                try:
                    inbox.put_nowait({"error": {"message": detail}})
                except queue.Full:
                    pass

    def _dispose(self, detail: str) -> None:
        self._ready.clear()
        self._fail_pending(detail)
        with self._lock:
            tree, process = self._tree, self._process
            self._tree, self._process = None, None
            self._cancelled.clear()
        if tree:
            tree.terminate()
        if process:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._closing.set()
        self._stop.set()
        self._state("inactive", "Runner stopped")
        self._ready.clear()
        self._fail_pending("Runner stopped")
        try:
            self._rpc("shutdown", {}, timeout=SHUTDOWN_TIMEOUT)
        except ExtensionUnavailable:
            pass
        self._dispose("Runner stopped")
        self._shutdown_complete.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=SHUTDOWN_TIMEOUT + 2)
