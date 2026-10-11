"""JSON-lines RPC Runner. Launch using the private environment's Python -I."""
from __future__ import annotations

import argparse
import base64
import importlib
import json
import math
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from amadeus_extension import AudioChunk, CancelledError, Extension, RequestContext, TTSRequest, __version__

MAX_LINE = 8 * 1024 * 1024
MAX_CHUNK = 1024 * 1024


class Runner:
    def __init__(self, entry: str) -> None:
        self.entry = entry
        self.output = sys.stdout
        # All package prints must leave the JSON wire intact, including imports.
        sys.stdout = sys.stderr
        self.extension: Extension | None = None
        self.snapshot: dict[str, Any] = {}
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._requests: dict[Any, RequestContext] = {}
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="extension-tts")
        self.stopping = False
        self._closed = False

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=True, allow_nan=False)
        if len(line.encode("utf-8")) > MAX_LINE:
            raise ValueError("protocol message exceeds limit")
        with self._write_lock:
            self.output.write(line + "\n")
            self.output.flush()

    def error(self, request_id: Any, code: int, message: str) -> None:
        for secret in self.snapshot.get("secrets", {}).values():
            if isinstance(secret, str) and secret:
                message = message.replace(secret, "[redacted]")
        self.send({"id": request_id, "error": {"code": code, "message": message[:240]}})

    def initialize(self, request_id: Any, params: dict[str, Any]) -> None:
        if self.extension is not None or params.get("protocol") != "amadeus.extension-rpc/v0":
            self.error(request_id, -32602, "invalid initialize")
            return
        self.snapshot = params
        try:
            module_name, attribute = self.entry.split(":", 1)
            extension = importlib.import_module(module_name)
            for part in attribute.split("."):
                extension = getattr(extension, part)
            if not isinstance(extension, Extension):
                raise TypeError("entry must be an Extension")
            context = self.context()
            if extension._initialize:
                extension._initialize(context)
            self.extension = extension
            self.send({"id": request_id, "result": {
                "sdk": {"name": "amadeus-extension-sdk", "version": __version__},
                "contributions": [{"kind": "voice_backend", "id": key, "contract": "amadeus.voice.tts/v0"} for key in extension._tts],
            }})
            self.send({"method": "ready", "params": {}})
            for key in extension._tts:
                self.send({"method": "contribution.health", "params": {"kind": "voice_backend", "id": key, "health": "ready", "detail": ""}})
        except Exception:
            self.error(request_id, -32010, "extension initialization failed")
            self.stopping = True

    def context(self) -> RequestContext:
        return RequestContext(config=self.snapshot.get("config"), secrets=self.snapshot.get("secrets"), paths=self.snapshot.get("paths"))

    def synthesize(self, request_id: Any, params: dict[str, Any]) -> None:
        if self.extension is None or self.stopping:
            self.error(request_id, -32002, "extension is not ready")
            return
        contribution_id = params.get("contribution_id")
        if contribution_id is None and len(self.extension._tts) == 1:
            contribution_id = next(iter(self.extension._tts))
        function = self.extension._tts.get(contribution_id)
        try:
            utterance_id, text, language = params["utterance_id"], params["text"], params["language"]
            speed = params.get("speed", 1.0)
            deadline = params.get("deadline_ms")
            if (function is None or not isinstance(utterance_id, str) or not 1 <= len(utterance_id) <= 128 or
                    not isinstance(text, str) or not 1 <= len(text) <= 2000 or not isinstance(language, str) or
                    type(speed) not in {int, float} or not math.isfinite(speed) or not 0.5 <= speed <= 2.0 or
                    (deadline is not None and (type(deadline) is not int or deadline <= 0))):
                raise ValueError()
            request = TTSRequest(utterance_id, text, language, float(speed), deadline)
        except (KeyError, TypeError, ValueError):
            self.error(request_id, -32602, "invalid TTS request")
            return
        context = self.context()
        with self._lock:
            if len(self._requests) >= 2 or request_id in self._requests:
                self.error(request_id, -32005, "TTS concurrency limit reached")
                return
            self._requests[request_id] = context
        self._pool.submit(self._produce, request_id, function, request, context)

    def _produce(self, request_id: Any, function, request: TTSRequest, context: RequestContext) -> None:
        stream = None
        timer = threading.Timer(request.deadline_ms / 1000, context._cancel) if request.deadline_ms else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            context.check_cancelled()
            stream = iter(function(request, context))
            chunks = 0
            duration_ms = 0.0
            sample_rate = None
            for chunk in stream:
                context.check_cancelled()
                if (not isinstance(chunk, AudioChunk) or type(chunk.sample_rate) is not int or
                        not 8000 <= chunk.sample_rate <= 48000 or not isinstance(chunk.pcm_s16le, bytes) or
                        not 0 < len(chunk.pcm_s16le) <= MAX_CHUNK or len(chunk.pcm_s16le) % 2 or
                        (sample_rate is not None and sample_rate != chunk.sample_rate)):
                    raise ValueError("invalid audio chunk")
                sample_rate = chunk.sample_rate
                self.send({"method": "tts.chunk", "params": {"request_id": request_id, "seq": chunks,
                           "sample_rate": sample_rate, "pcm_s16le": base64.b64encode(chunk.pcm_s16le).decode("ascii")}})
                chunks += 1
                duration_ms += len(chunk.pcm_s16le) / 2 / sample_rate * 1000
            context.check_cancelled()
            if not chunks:
                raise ValueError("extension produced no audio")
            self.send({"id": request_id, "result": {"chunks": chunks, "duration_ms": duration_ms}})
        except CancelledError:
            self.error(request_id, -32003, "request cancelled")
        except Exception:
            self.error(request_id, -32010, "TTS contribution failed")
        finally:
            if timer:
                timer.cancel()
            if stream is not None:
                close = getattr(stream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            with self._lock:
                self._requests.pop(request_id, None)

    def cancel(self, request_id: Any) -> None:
        with self._lock:
            context = self._requests.get(request_id)
        if context:
            # Cleanup may block in a third-party client; keep the wire reader responsive.
            threading.Thread(target=context._cancel, daemon=True, name="extension-cancel").start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stopping = True
        with self._lock:
            pending = list(self._requests)
        for request_id in pending:
            self.cancel(request_id)
        if self.extension and self.extension._shutdown:
            self.extension._shutdown()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def run(self) -> None:
        try:
            while not self.stopping:
                line = sys.stdin.buffer.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    break
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise ValueError()
                    request_id = message.get("id")
                    method, params = message.get("method"), message.get("params", {})
                    if method == "initialize" and request_id is not None:
                        self.initialize(request_id, params)
                    elif method == "$/cancel":
                        self.cancel(params.get("id"))
                    elif method == "$/ping" and request_id is not None:
                        self.send({"id": request_id, "result": {}})
                    elif method == "shutdown" and request_id is not None:
                        self.close()
                        self.send({"id": request_id, "result": {}})
                    elif method == "tts.synthesize" and request_id is not None:
                        self.synthesize(request_id, params)
                    elif request_id is not None:
                        self.error(request_id, -32601, "method not found")
                except (ValueError, TypeError, KeyError):
                    self.error(None, -32700, "invalid protocol message")
        finally:
            self.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", required=True)
    args = parser.parse_args()
    Runner(args.entry).run()


if __name__ == "__main__":
    main()
