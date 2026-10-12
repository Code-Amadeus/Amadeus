"""Independent synthetic backend for the optional desktop theme lifecycle probe."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.extensions.runtime import ExtensionRuntime  # noqa: E402
from tts.backend import TTSSynthesisRequest  # noqa: E402


def reply(value: dict) -> None:
    print(json.dumps(value), flush=True)


def main() -> None:
    runtime = ExtensionRuntime(json.loads(sys.stdin.readline()))
    runtime.start()
    reply({
        "pid": os.getpid(),
        "accepted": {row["id"]: row["revision"] for row in runtime.statuses() if "revision" in row},
    })
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("stop"):
                break
            # Snapshot acceptance precedes optional Runner readiness, just as in the Host.
            deadline = time.monotonic() + 15
            while any(client.state == "starting" for client in runtime.clients.values()) and time.monotonic() < deadline:
                time.sleep(0.05)
            try:
                chunk = runtime.backends[request["speak"]].synthesize(
                    TTSSynthesisRequest(text="Lifecycle test.", language="en"))
                reply({"samples": len(chunk.audio), "rate": chunk.sample_rate, "pid": os.getpid()})
            except Exception as error:
                reply({"error": f"{type(error).__name__}: {error}"})
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
