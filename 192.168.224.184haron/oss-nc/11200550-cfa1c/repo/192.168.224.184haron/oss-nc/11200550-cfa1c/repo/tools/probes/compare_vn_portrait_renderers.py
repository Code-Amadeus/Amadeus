"""Visible, sequential original-Tk/Lite-avatar measurements; never launch a game/model."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import psutil


ROOT = Path(__file__).resolve().parents[2]


def run(command, cwd, output, port):
    rows = []
    with (output / "process.log").open("wb") as log:
        proc = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=log,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        parent = psutil.Process(proc.pid)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("Overlay exited; see process.log")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.3) as response:
                        if json.load(response)["ok"]:
                            break
                except Exception:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Overlay startup timeout")
            for stage in ("idle", "speaking", "returned"):
                if stage != "idle":
                    payload = {"source": "vn_playback", "sentence_id": "bench", "emotion": "thinking",
                               "display_text": "同一段语音，同一构图；比较完整悬浮窗的资源开销。",
                               "speaking": stage == "speaking"}
                    with urlopen(Request(f"http://127.0.0.1:{port}/reaction", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}), timeout=2) as response:
                        response.read()
                time.sleep(0.8)
                for _ in range(6):
                    processes = [parent, *parent.children(recursive=True)]
                    records = []
                    for process in processes:
                        try:
                            records.append({"pid": process.pid, "rss": process.memory_info().rss,
                                            "cpu_seconds": sum(process.cpu_times()[:2])})
                        except psutil.NoSuchProcess:
                            pass
                    rows.append({"stage": stage, "time": time.monotonic(), "processes": records})
                    time.sleep(0.3)
        finally:
            # Only this probe's captured process tree is terminated.
            try:
                processes = [*parent.children(recursive=True), parent]
            except psutil.NoSuchProcess:
                processes = []
            for process in processes:
                try:
                    process.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(processes, timeout=4)
            for process in alive:
                process.kill()
    (output / "samples.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    summary = {}
    for stage in ("idle", "speaking", "returned"):
        selected = [row for row in rows if row["stage"] == stage]
        rss = [sum(p["rss"] for p in row["processes"]) / 1024**2 for row in selected]
        first, last = selected[0], selected[-1]
        cpu = (sum(p["cpu_seconds"] for p in last["processes"]) - sum(p["cpu_seconds"] for p in first["processes"]))
        summary[stage] = {"process_tree_rss_mib": [round(min(rss), 1), round(max(rss), 1)],
                          "cpu_one_core_percent": round(100 * cpu / (last["time"] - first["time"]), 2)}
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result = {"method": "Sum of process-tree RSS, includes shared pages; sequential visible windows, no game/model; six samples per stage. CPU is percent of one logical core."}
    for mode, port in (("legacy_tk", 18789), ("lite_avatar_tk", 18790)):
        out = args.output / mode
        out.mkdir()
        if mode == "legacy_tk":
            command = [sys.executable, str(args.legacy_root / "vn_portrait_overlay_tk.py"), "--port", str(port)]
            cwd = args.legacy_root
        else:
            command = [sys.executable, str(ROOT / "tools/vn_portrait_overlay_lite.py"),
                       "--lite-dir", str(ROOT / "assets/companion/kurisu"), "--port", str(port)]
            cwd = ROOT
        result[mode] = run(command, cwd, out, port)
    (args.output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
