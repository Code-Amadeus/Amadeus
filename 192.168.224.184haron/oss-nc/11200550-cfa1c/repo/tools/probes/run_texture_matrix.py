"""Serial hardware runs with independent device-memory samples; no model launch."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--electron", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--variants", nargs="+", choices=["baseline", "sampled", "cached"], default=["baseline", "sampled", "cached"])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TEXTURE_PROBE_PYTHON=str(args.python))
    env.pop("ELECTRON_RUN_AS_NODE", None)
    status = {"running": True, "runs": []}
    state = args.output / "matrix-status.json"
    for variant in args.variants:
        directory = args.output / f"{variant}-{args.seconds}"
        if directory.exists():
            raise FileExistsError(directory)
        command = [str(args.electron), "tests/fpsTextures.probe.mjs",
                   "baseline" if variant == "baseline" else "sampled", "60", "--profile", "standard",
                   "--duration-seconds", str(args.seconds), "--fixed-route", "--high-performance-gpu",
                   "--expect-gpu", "RTX 4070 Laptop", "--companion", "--context-loss", "--texture-disposal",
                   "--output", str(directory)]
        if variant == "cached":
            if not args.cache:
                raise ValueError("--cache required")
            command += ["--bc7-cache", str(args.cache)]
        run = {"variant": variant, "command": command, "startedUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        status["runs"].append(run)
        state.write_text(json.dumps(status, indent=2), encoding="utf-8")
        with (args.output / f"{variant}-{args.seconds}.stdout.log").open("w", encoding="utf-8") as stdout, \
             (args.output / f"{variant}-{args.seconds}.stderr.log").open("w", encoding="utf-8") as stderr, \
             (args.output / f"{variant}-{args.seconds}.gpu.ndjson").open("w", encoding="utf-8") as gpu:
            process = subprocess.Popen(command, cwd=args.root / "electron", env=env, stdout=stdout, stderr=stderr,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            run["pid"] = process.pid
            state.write_text(json.dumps(status, indent=2), encoding="utf-8")
            while process.poll() is None:
                sample = {"timeUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                try:
                    result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw",
                                             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4,
                                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
                    sample["csv"] = result.stdout.strip()
                except (OSError, subprocess.SubprocessError) as error:
                    sample["error"] = type(error).__name__
                gpu.write(json.dumps(sample) + "\n")
                gpu.flush()
                time.sleep(2)
            run["exitCode"] = process.returncode
        summary_path = directory / "summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        run["complete"] = summary.get("complete", False)
        run["error"] = summary.get("error")
        run["finishedUtc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state.write_text(json.dumps(status, indent=2), encoding="utf-8")
        print(json.dumps(run), flush=True)
        if process.returncode or not run["complete"]:
            status["running"] = False
            state.write_text(json.dumps(status, indent=2), encoding="utf-8")
            raise RuntimeError("Probe failed; subsequent variants were not started")
    status["running"] = False
    state.write_text(json.dumps(status, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
