"""Reversible real-Lively lifecycle probe; Windows only, no model/API calls.

Uses the actual Amadeus bridge and checks live RPC state before/after normal
exit, parent-pipe loss, and helper termination followed by journal recovery.
The finally block always attempts recovery before stopping the local bridge.
"""
from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HELPER = ROOT / "build/windows-wallpaper/host/Amadeus.Wallpaper.exe"


def inspect() -> dict:
    result = subprocess.run([str(HELPER), "inspect"], capture_output=True, text=True, check=True)
    return json.loads(result.stdout.splitlines()[-1])


def wait_status(process: subprocess.Popen, expected: str, timeout: int = 75) -> None:
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
        event = json.loads(line)
        if event.get("status") == "error":
            raise RuntimeError(event)
        if event.get("status") == expected:
            return
    raise TimeoutError(f"No {expected} status")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", type=int, default=0, help="Seconds to inspect the real mounted scene visually")
    options = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Windows-only probe")
    from wallpaper.wallpaper_engine_bridge import WallpaperEngineBridgeHost
    from render.spriteforge_animator import SpriteForgeAnimator

    baseline = inspect()
    if baseline["recoveryPending"]:
        raise SystemExit("Resolve the existing session before running the probe")
    bridge = WallpaperEngineBridgeHost(asset_port=17880, bridge_port=17897)
    animator = SpriteForgeAnimator(bridge)
    report = {"before": baseline, "experiments": []}
    output = ROOT / "build/windows-wallpaper/lifecycle-experiment.json"
    process = None
    bridge.start()
    animator.start()
    try:
        for scenario in ("normal-exit", "parent-pipe-loss", "helper-crash-recovery"):
            previous_clients = {id(client) for client in bridge._state.clients}
            process = subprocess.Popen(
                [str(HELPER), "run", bridge.lively_url], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            wait_status(process, "mounted")
            mounted = inspect()
            assert any(Path(item["Path"]).name == "amadeus-managed" for item in mounted["wallpapers"])
            deadline = time.monotonic() + 30
            while not any(id(client) not in previous_clients for client in bridge._state.clients) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert any(id(client) not in previous_clients for client in bridge._state.clients), "Lively mounted but the Amadeus renderer did not connect"
            print(f"MOUNTED {scenario}; live renderer connected", flush=True)
            if options.hold and scenario == "normal-exit":
                time.sleep(options.hold)
            if scenario == "helper-crash-recovery":
                process.kill()  # Deliberately kill ONLY this probe's helper.
                process.wait(timeout=10)
                recovered = subprocess.run([str(HELPER), "recover"], capture_output=True, text=True, timeout=75)
                assert recovered.returncode == 0, recovered.stdout + recovered.stderr
            else:
                if scenario == "normal-exit":
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                else:
                    process.stdin.close()  # Exact EOF received when the owning Electron process exits.
                assert process.wait(timeout=75) == 0, process.stderr.read()
            after = inspect()
            assert after["wallpapers"] == baseline["wallpapers"], (baseline, after)
            assert after["options"] == baseline["options"], (baseline, after)
            assert after["running"] == baseline["running"], (baseline, after)
            assert not after["recoveryPending"]
            report["experiments"].append({"scenario": scenario, "renderer_connected": True, "after": after, "passed": True})
            print(f"PASS {scenario}: original wallpaper and host restored", flush=True)
        # Unlike closing stdin alone, real parent death also closes the stdout
        # reader. Verify that a broken diagnostic pipe cannot abort cleanup.
        parent_script = '''
import json, os, subprocess, sys
child = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
for line in child.stdout:
    event = json.loads(line)
    if event.get('status') == 'error':
        print(line, flush=True)
        sys.exit(1)
    if event.get('status') == 'mounted':
        print(child.pid, flush=True)
        os._exit(0)
sys.exit(1)
'''
        parent = subprocess.run([sys.executable, "-c", parent_script, str(HELPER), "run", bridge.lively_url],
                                capture_output=True, text=True, timeout=75)
        assert parent.returncode == 0, parent.stdout + parent.stderr
        deadline = time.monotonic() + 45
        while inspect()["recoveryPending"] and time.monotonic() < deadline:
            time.sleep(0.2)
        after = inspect()
        assert not after["recoveryPending"] and after["wallpapers"] == baseline["wallpapers"], after
        assert after["options"] == baseline["options"] and after["running"] == baseline["running"], after
        report["experiments"].append({"scenario": "actual-parent-crash", "after": after, "passed": True})
        print("PASS actual-parent-crash: restored after stdin and stdout readers vanished", flush=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(output, flush=True)
        return 0
    finally:
        if process is not None and process.poll() is None:
            process.stdin.close()
            process.wait(timeout=75)
        recovery = subprocess.run([str(HELPER), "recover"], capture_output=True, text=True, timeout=75)
        if recovery.returncode:
            print("RECOVERY FAILED: " + recovery.stdout + recovery.stderr, file=sys.stderr)
        animator.stop()
        bridge.stop()


if __name__ == "__main__":
    raise SystemExit(main())
