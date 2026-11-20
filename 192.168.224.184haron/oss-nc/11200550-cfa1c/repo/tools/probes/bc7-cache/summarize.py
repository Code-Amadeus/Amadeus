"""Additional fixed-route/cache/steady-window evidence over the common probe summary."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from summarize_texture_runs import cpu_window, summarize_run  # noqa: E402


def rows(file):
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]


def counters(samples, key, fields):
    first, last = samples[0]["render"].get(key) or {}, samples[-1]["render"].get(key) or {}
    return {field: {"initial": first.get(field), "final": last.get(field),
                    "delta": last[field] - first[field] if field in first and field in last else None}
            for field in fields}


def summarize(directory):
    common = summarize_run(directory.name, directory)
    samples = [row for row in rows(directory / "samples.ndjson") if row["stage"] in ("journey", "journey-complete")]
    result = {"recording": common["recording"], "metadata": common["metadata"],
              "journeyMemory": common["journeyMemory"], "journeyGaps": common["journeyGaps"],
              "phaseStatus": common["phaseStatus"], "webglLog": common["webglLog"], "phases": common["phases"]}
    windows = {"full": samples, "last300": [sample for sample in samples if sample["elapsedMs"] >= 300000]}
    for name, window in windows.items():
        if len(window) < 2:
            continue
        result[name] = {"startMs": window[0]["elapsedMs"], "endMs": window[-1]["elapsedMs"],
                        "cpu": cpu_window(window),
                        "transcodes": counters(window, "transcodes", ["completed", "failures", "elapsedMs"]),
                        "cache": counters(window, "bc7Cache", ["hits", "misses", "failures", "unsupported", "inflateMs", "validateMs"]),
                        "store": counters(window, "textureStats", ["loads", "refetches", "evictions", "failures", "displayMiss", "textureUploads", "textureUploadMs"])}
    events = rows(directory / "events.ndjson")
    route = rows(directory / "journey.ndjson")
    expected_phases = {row["phase"] for row in route}
    result["journeyMisses"] = sum(event.get("kind") == "miss" and event.get("phase") in expected_phases for event in events)
    result["route"] = {"actions": len(route), "maxActionDelayMs": max((row["actualAtSeconds"] - row["at"]) * 1000 for row in route),
                       "observedLabelsMatch": all(row.get("observed", {}).get("label") == row.get("label") for row in route),
                       "schedule": [{key: row[key] for key in ("at", "label", "speaking")} for row in route]}
    result["observedTextureFormats"] = sorted({row["render"]["displayedTextureFormat"] for row in samples if row["render"].get("displayedTextureFormat") is not None})
    metadata = json.loads((directory / "metadata.json").read_text())
    result["bc7Cache"] = metadata.get("bc7Cache")
    gpu_file = directory.parent / (directory.name + ".gpu.ndjson")
    if gpu_file.exists():
        start, end = [datetime.fromisoformat(row["timeUtc"].replace("Z", "+00:00")) for row in (samples[0], samples[-1])]
        gpu_rows = [row for row in rows(gpu_file) if "csv" in row and start <= datetime.fromisoformat(row["timeUtc"].replace("Z", "+00:00")) <= end]
        parsed = [row["csv"].split(", ") for row in gpu_rows]
        used = [float(row[1]) for row in parsed]
        result["deviceMemoryMiB"] = {"scope": "whole NVIDIA GPU, includes other applications", "samples": len(used),
                                     "peak": max(used, default=None), "mean": mean(used) if used else None,
                                     "final": used[-1] if used else None}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"schema": "amadeus.bc7-comparison.v1", "runs": {}}
    for variant in ["baseline", "sampled", "cached"]:
        directory = args.directory / (variant + "-600")
        if (directory / "summary.json").exists():
            result["runs"][variant] = summarize(directory)
    if result["runs"]:
        values = list(result["runs"].values())
        result["identicalSchedules"] = all(run["route"]["schedule"] == values[0]["route"]["schedule"] for run in values)
        result["identicalSourceHashes"] = all(run["metadata"]["sourceSha256"] == values[0]["metadata"]["sourceSha256"] for run in values)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"runs": list(result["runs"]), "output": str(args.output)}))
