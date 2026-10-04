"""Aggregate local texture-probe v2 evidence without copying machine identities.

Example: python tools/probes/summarize_texture_runs.py --run baseline=RUN_DIR
    --run candidate=RUN_DIR --output comparison.json

Memory values are bytes. Electron memoryKiB values are converted explicitly;
v2 *Bytes values are already bytes. Process groups are kept separate from the
store's retained CPU + potential GPU accounting. The final 30-second window is
descriptive, never evidence by itself that preload or memory has settled.
Journey-only comparisons require explicit sample stages and exclude optional
companion, scenario and context stages. Missing stage evidence is unavailable.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import math
from pathlib import Path
import re
from statistics import fmean


TAIL_MS = 30_000
GAP_KEYS = ("count", "p50Ms", "p95Ms", "p99Ms", "maxMs", "over50Ms")
PHASE_KEYS = ("startedAtMs", "endedAtMs", "durationMs", "misses", "changes", "cycles", "holds",
              "loadsStarted", "loadsCompleted", "loadFailures", "refetches", "decisions", "loadedPerSecond")
LEGACY_LOAD_KEYS = ("loadsStarted", "loadsCompleted", "loadFailures", "refetches", "loadedPerSecond")
STORE_KEYS = ("residentCpuBytes", "residentGpuBytes", "residentBytes", "budgetBytes", "pinnedBytes",
              "pinnedOverageBytes", "residentFrames", "transientBytes", "maxInFlight", "entries", "queued",
              "inFlight", "loads", "refetches", "evictions", "failures", "pressure", "displayMiss",
              "fetchAttempts", "fetchCompleted", "fetchedPayloadBytes", "transcodeAttempts",
              "transcodesCompleted", "transcodeMs", "textureUploads", "textureUploadMs")
LEGACY_KEYS = ("legacyFrames", "cpuBufferBytes", "glTextures", "queue", "activeLoads")
ERROR_KINDS = ("INVALID_ENUM", "INVALID_VALUE", "INVALID_OPERATION", "INVALID_FRAMEBUFFER_OPERATION")
EVIDENCE_TYPES = {"metadata.json": "metadata", "summary.json": "phase-summary",
                  "samples.ndjson": "process-and-texture-samples", "events.ndjson": "browser-events",
                  "renderer.log": "renderer-log", "frame.png": "final-frame"}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) else None


def relative_source(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:render|electron|tools|wallpaper|config)/[A-Za-z0-9_./-]+", value):
        return None
    return value if ".." not in value.split("/") else None


def hash_value(value, length=64):
    return value if isinstance(value, str) and re.fullmatch(rf"[a-fA-F0-9]{{{length}}}", value) else None


def numeric_fields(mapping, keys):
    return {key: mapping[key] for key in keys if key in mapping and (number(mapping[key]) or mapping[key] is None)}


def read_json(path, issues):
    if not path.is_file():
        issues.append({"file": path.name, "issue": "missing"})
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, UnicodeError, ValueError):
        pass
    issues.append({"file": path.name, "issue": "invalid-json-object"})
    return {}


def read_ndjson(path, issues, required=False):
    if not path.is_file():
        if required:
            issues.append({"file": path.name, "issue": "missing"})
        return
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError
                    yield value
                except ValueError:
                    issues.append({"file": path.name, "issue": "invalid-record", "line": line_number})
    except (OSError, UnicodeError):
        issues.append({"file": path.name, "issue": "unreadable"})


def process_bytes(process, metric):
    value = process.get(metric)
    if number(value) and value >= 0:
        return value
    kib = process.get("memoryKiB") or {}
    value = kib.get("privateBytes" if metric == "privateBytes" else "workingSetSize")
    return value * 1024 if number(value) and value >= 0 else None


def group_memory(sample, group, metric):
    if group == "host":
        return process_bytes(sample.get("host") or {}, metric)
    key = "rendererProcesses" if group == "renderer" else "gpuProcesses"
    processes = sample.get(key)
    if processes is None:
        # Older recordings may retain Electron's memoryKiB process records.
        all_processes = sample.get("processes") or []
        if group == "renderer":
            processes = [row for row in all_processes if row.get("pid") == sample.get("rendererPid")
                         or (sample.get("rendererPid") is None and row.get("type", "").lower() in ("tab", "renderer"))]
        else:
            processes = [row for row in all_processes if "gpu" in row.get("type", "").lower()]
    if not processes:
        return None
    values = [process_bytes(process, metric) for process in processes]
    return sum(values) if all(value is not None for value in values) else None


def cpu_window(samples):
    """Only count intervals with complete, monotonic counters for identical PIDs.

    Missing counters or a process restart make that interval unavailable, never
    zero. PIDs are used locally for matching and are not published.
    """
    result = {}
    for group, key in (("renderer", "rendererProcesses"), ("gpu", "gpuProcesses"),
                       ("other", "otherProcesses"), ("host", "host")):
        seconds = duration = 0
        measured = missing = 0
        previous = None
        for sample in samples:
            rows = [sample.get(key) or {}] if group == "host" else sample.get(key) or []
            current = {row.get("pid"): row.get("cpuSeconds") for row in rows}
            elapsed = sample.get("elapsedMs")
            valid = bool(current) and None not in current and all(number(v) and v >= 0 for v in current.values())
            if previous is not None:
                before, start, was_valid = previous
                if (valid and was_valid and current.keys() == before.keys() and number(elapsed)
                        and number(start) and elapsed > start and all(current[p] >= before[p] for p in current)):
                    seconds += sum(current[p] - before[p] for p in current)
                    duration += (elapsed - start) / 1000
                    measured += 1
                else:
                    missing += 1
            previous = current, elapsed, valid
        result[group] = {"cpuSeconds": seconds if measured else None,
                         "measuredWallSeconds": duration, "measuredIntervals": measured, "missingIntervals": missing,
                         "meanSingleCorePercent": seconds / duration * 100 if duration else None}
    return result


def describe_series(values, tail_values):
    measured = [value for value in values if number(value)]
    tail = [value for value in tail_values if number(value)]
    return {"initial": values[0] if values else None, "peak": max(measured, default=None),
            "final": values[-1] if values else None, "sampleCount": len(measured),
            "missingSamples": len(values) - len(measured),
            "tail": {"min": min(tail, default=None), "max": max(tail, default=None),
                     "mean": fmean(tail) if tail else None, "sampleCount": len(tail)}}


def gap_summary(values):
    ordered = sorted(value for value in values if number(value) and value >= 0)
    def percentile(fraction):
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None
    return {"count": len(ordered), "p50Ms": percentile(.5), "p95Ms": percentile(.95),
            "p99Ms": percentile(.99), "maxMs": ordered[-1] if ordered else None,
            "over50Ms": sum(value > 50 for value in ordered)}


def phases_from_events(events):
    phases = OrderedDict()
    for event in events:
        name = identifier(event.get("phase")) or "unlabelled"
        row = phases.setdefault(name, {"name": name, "startedAtMs": None, "endedAtMs": None,
            "misses": 0, "changes": 0, "cycles": 0, "holds": 0, "loadsStarted": 0,
            "loadsCompleted": 0, "loadFailures": 0, "refetches": 0, "decisions": 0, "raf": [], "ticks": []})
        kind = event.get("kind")
        if kind in ("phase-start", "phase-end"):
            row["startedAtMs" if kind == "phase-start" else "endedAtMs"] = event.get("t")
        elif kind in ("raf", "tick"):
            row["raf" if kind == "raf" else "ticks"].append(event.get("dt"))
        elif kind in ("miss", "change", "cycle", "hold", "decision"):
            row[{"miss": "misses", "change": "changes", "cycle": "cycles", "hold": "holds", "decision": "decisions"}[kind]] += 1
        elif kind == "load-start":
            row["loadsStarted"] += 1
            row["refetches"] += int(number(event.get("attempt")) and event["attempt"] > 1)
        elif kind == "load-end":
            row["loadsCompleted" if event.get("ok") is True else "loadFailures"] += 1
    for row in phases.values():
        start, end = row["startedAtMs"], row["endedAtMs"]
        row["durationMs"] = max(0, end - start) if number(start) and number(end) else None
        row["rafGaps"] = gap_summary(row.pop("raf"))
        row["tickerGaps"] = gap_summary(row.pop("ticks"))
        row["loadedPerSecond"] = row["loadsCompleted"] * 1000 / row["durationMs"] if row["durationMs"] else None
    return list(phases.values())


def sanitize_phase(row):
    result = {"phase": identifier(row.get("name")) or "unlabelled", **numeric_fields(row, PHASE_KEYS)}
    for key in ("rafGaps", "tickerGaps"):
        result[key] = numeric_fields(row.get(key) or {}, GAP_KEYS)
    return result


def phase_status(row):
    result = {"phase": identifier(row.get("phase")) or "unlabelled"}
    for key, allowed in (("status", ("exercised", "failed", "requested", "unavailable", "unverified", "not-requested")),
                         ("activation", ("observed", "unverified"))):
        if row.get(key) in allowed:
            result[key] = row[key]
    for key in ("suppressed", "restored", "pixelMatch"):
        if isinstance(row.get(key), bool):
            result[key] = row[key]
    result.update(numeric_fields(row, ("requestedSeconds", "actualSeconds")))
    if row.get("reason"):
        result["reasonRecorded"] = True  # Free-form errors stay in local raw evidence.
    if row.get("phase") == "context-loss":
        result["visualValidation"] = "recorded" if isinstance(row.get("pixelMatch"), bool) else "unverified"
        for key in ("beforePixels", "afterPixels"):
            pixels = row.get(key)
            if not isinstance(pixels, dict):
                continue
            clean = numeric_fields(pixels, ("width", "height", "rgbSum", "alphaSum"))
            value = pixels.get("hash")
            if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{1,8}", value):
                clean["hash"] = value
            if isinstance(pixels.get("glErrors"), list):
                clean["glErrors"] = [value for value in pixels["glErrors"] if number(value)]
            result[key] = clean
    return result


def observed_values(samples, accessor):
    values = [accessor(sample) for sample in samples]
    return sorted({value for value in values if number(value) or isinstance(value, bool)}, key=lambda value: (str(type(value)), value))


def clean_gpu(value):
    if not isinstance(value, str) or re.search(r"(?:[A-Za-z]:[\\/]|https?://|file://|\\\\)", value):
        return None
    return re.sub(r"\s*\(0x[a-fA-F0-9]+\)|0x[a-fA-F0-9]+", "", value).strip()


def metadata_facts(metadata, samples):
    host = metadata.get("host") or {}
    options, settings = metadata.get("options") or {}, host.get("settings") or {}
    versions = {key: value for key, value in (metadata.get("versions") or {}).items()
                if key in ("electron", "chrome", "node") and identifier(value)}
    python = (host.get("environment") or {}).get("python")
    if identifier(python):
        versions["python"] = python
    source_hashes = {key: value for key, value in (metadata.get("sourceSha256") or {}).items()
                     if relative_source(key) and hash_value(value)}
    pack = host.get("pack") or {}
    clean_pack = {key: pack[key] for key in ("id", "version") if identifier(pack.get(key))}
    manifest = (pack.get("sourceSha256") or {}).get("runtime_manifest.json")
    if hash_value(manifest):
        clean_pack["manifestSha256"] = manifest
    if isinstance(pack.get("available"), bool):
        clean_pack["available"] = pack["available"]
    result = {"revision": hash_value(metadata.get("revision"), 40), "sourceSha256": source_hashes,
              "versions": versions, "pack": clean_pack,
              "options": numeric_fields(options, ("fps", "durationSeconds", "sampleSeconds", "seed")),
              "settings": numeric_fields(settings, ("renderMaxFps", "renderMaxResolution")),
              "offscreenFrameRate": metadata.get("offscreenFrameRate") if number(metadata.get("offscreenFrameRate")) else None,
              "platform": {key: value for key, value in (metadata.get("platform") or {}).items()
                           if key in ("os", "release", "architecture") and identifier(value)},
              "instrumentation": {key: value for key, value in (metadata.get("instrumentation") or {}).items()
                                  if key in ("legacyMisses", "legacyLoads", "textureStats") and isinstance(value, bool)}}
    for key in ("sampling", "scenario", "companion", "contextLoss"):
        if isinstance(options.get(key), bool):
            result["options"][key] = options[key]
    if isinstance(settings.get("renderTextureSampling"), bool):
        result["settings"]["renderTextureSampling"] = settings["renderTextureSampling"]
    if settings.get("graphicsProfile") in ("standard", "power_saving", "custom"):
        result["settings"]["graphicsProfile"] = settings["graphicsProfile"]
    if isinstance(metadata.get("offscreen"), bool):
        result["offscreen"] = metadata["offscreen"]
    if isinstance(metadata.get("cpuProfiling"), bool):
        result["cpuProfiling"] = metadata["cpuProfiling"]
    result["observed"] = {key: observed_values(samples, lambda row, key=key: (row.get("render") or {}).get(key))
                          for key in ("resolution", "dpr", "rawTickerMaxFps", "samplingEnabled", "sampleFps")}
    result["observed"]["configuredMaxFps"] = observed_values(samples, lambda row: row.get("configuredMaxFps"))
    result["observed"]["offscreenFrameRate"] = observed_values(samples, lambda row: row.get("offscreenFrameRate"))
    result["gpuModels"] = sorted({clean for row in samples if (clean := clean_gpu((row.get("render") or {}).get("gpu")))})
    return result


def sampled_counters(samples, key, fields):
    rows = [(sample.get("elapsedMs"), numeric_fields((sample.get("render") or {}).get(key) or {}, fields))
            for sample in samples] if key else [(sample.get("elapsedMs"), numeric_fields(sample.get("render") or {}, fields)) for sample in samples]
    rows = [(elapsed, values) for elapsed, values in rows if values]
    return {"sampleCount": len(rows), "lastObservedElapsedMs": rows[-1][0] if rows else None,
            "last": rows[-1][1] if rows else None,
            "peak": {field: max(values[field] for _, values in rows if number(values.get(field)))
                     for field in fields if any(number(values.get(field)) for _, values in rows)}}


def webgl_log(path, issues):
    counts = {kind: 0 for kind in ERROR_KINDS}
    if not path.is_file():
        return {"available": False, "errorLines": None, "byKind": {key: None for key in counts}}
    total = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if "webgl" not in line.lower():
                    continue
                found = {kind for kind in ERROR_KINDS if re.search(rf"\b{kind}\b", line)}
                total += bool(found)
                for kind in found:
                    counts[kind] += 1
    except OSError:
        issues.append({"file": path.name, "issue": "unreadable"})
        return {"available": False, "errorLines": None, "byKind": {key: None for key in counts}}
    return {"available": True, "errorLines": total, "byKind": counts}


def memory_window(samples, scope):
    elapsed = [row["elapsedMs"] for row in samples if number(row.get("elapsedMs"))]
    end = max(elapsed, default=None)
    tail = [row for row in samples if number(row.get("elapsedMs")) and end - TAIL_MS <= row["elapsedMs"] <= end] if end is not None else []
    tail_elapsed = [row["elapsedMs"] for row in tail]
    loading = []
    for sample in tail:
        render = sample.get("render") or {}
        store = render.get("textureStats") or {}
        signals = [render.get("activeLoads"), render.get("queue"), store.get("inFlight"), store.get("queued")]
        loading.extend(value > 0 for value in signals if number(value))
    memory = {group: {metric: describe_series([group_memory(row, group, metric) for row in samples],
                                             [group_memory(row, group, metric) for row in tail])
                      for metric in ("privateBytes", "rssBytes")} for group in ("renderer", "gpu", "host")}
    window = {"requestedDurationMs": TAIL_MS, "scope": scope,
              "startElapsedMs": min(tail_elapsed, default=None), "endElapsedMs": max(tail_elapsed, default=None),
              "observedDurationMs": max(tail_elapsed) - min(tail_elapsed) if tail_elapsed else None,
              "sampleCount": len(tail), "settlement": "unverified",
              "loadingObserved": any(loading) if loading else None}
    return memory, window


def journey_memory(samples):
    scope = "journey and journey-complete samples only"
    if not samples:
        reason = "no-samples"
    elif any(not isinstance(row.get("stage"), str) or not row["stage"] for row in samples):
        reason = "missing-stage-labels"
    else:
        selected = [row for row in samples if row["stage"] in ("journey", "journey-complete")]
        if selected:
            memory, window = memory_window(selected, scope)
            return memory, {"available": True, **window}
        reason = "no-journey-samples"
    return None, {"available": False, "scope": scope, "reason": reason}


def journey_gaps(metadata, events_path, issues):
    scope = "raw RAF and ticker events in metadata.journey action phases only"
    actions = metadata.get("journey")
    phases = {name for action in actions if isinstance(action, dict)
              and (name := identifier(action.get("phase")))} if isinstance(actions, list) else set()
    if not phases:
        return {"available": False, "scope": scope, "reason": "missing-journey-phase-labels"}
    if not events_path.is_file():
        return {"available": False, "scope": scope, "reason": "missing-events"}
    raf, ticks = [], []
    for event in read_ndjson(events_path, issues):
        if identifier(event.get("phase")) not in phases:
            continue
        value = event.get("dt")
        if not number(value) or value < 0:
            continue
        if event.get("kind") == "raf":
            raf.append(value)
        elif event.get("kind") == "tick":
            ticks.append(value)
    if not raf and not ticks:
        return {"available": False, "scope": scope, "reason": "no-valid-journey-gaps"}
    return {"available": True, "scope": scope, "rafGaps": gap_summary(raf), "tickerGaps": gap_summary(ticks)}


def summarize_run(label, directory):
    directory = Path(directory)
    issues = []
    metadata = read_json(directory / "metadata.json", issues)
    summary = read_json(directory / "summary.json", issues)
    samples = list(read_ndjson(directory / "samples.ndjson", issues, required=True))
    samples.sort(key=lambda row: row.get("elapsedMs") if number(row.get("elapsedMs")) else -1)
    elapsed = [row["elapsedMs"] for row in samples if number(row.get("elapsedMs"))]
    memory, tail_window = memory_window(samples, "all recorded samples")
    journey, journey_window = journey_memory(samples)
    raw_phases = summary.get("phases")
    phase_source = "summary.json"
    if not isinstance(raw_phases, list):
        phase_source = "events.ndjson" if (directory / "events.ndjson").is_file() else "unavailable"
        raw_phases = phases_from_events(read_ndjson(directory / "events.ndjson", issues))
    phases = [sanitize_phase(row) for row in raw_phases if isinstance(row, dict)]
    if (metadata.get("instrumentation") or {}).get("legacyLoads") is False:
        for phase in phases:
            phase.update(dict.fromkeys(LEGACY_LOAD_KEYS, None))
    changed = summary.get("sourceChangedDuringRun")
    return {"label": label, "metadata": metadata_facts(metadata, samples),
            "recording": {"complete": summary.get("complete") if isinstance(summary.get("complete"), bool) else None,
                          "errorRecorded": bool(summary.get("error")),
                          "droppedEvents": summary.get("droppedEvents") if number(summary.get("droppedEvents")) else None,
                          "sourceChangedDuringRun": [name for name in changed if relative_source(name)] if isinstance(changed, list) else None,
                          "sourceChangedCount": len(changed) if isinstance(changed, list) else None,
                          "sampleCount": len(samples), "sampledDurationMs": max(elapsed) - min(elapsed) if elapsed else None},
            "memory": memory,
            "tailWindow": tail_window,
            "journeyMemory": journey, "journeyTailWindow": journey_window,
            "journeyCpu": cpu_window([row for row in samples if row.get("stage") in ("journey", "journey-complete")]),
            "journeyGaps": journey_gaps(metadata, directory / "events.ndjson", issues),
            "phaseMetricsSource": phase_source, "phases": phases,
            "phaseTotals": {key: sum(row[key] for row in phases if number(row.get(key)))
                            if any(number(row.get(key)) for row in phases) else None
                            for key in ("misses", "changes", "cycles", "holds", "loadsStarted", "loadsCompleted", "loadFailures", "refetches")},
            "phaseStatus": [phase_status(row) for row in (summary.get("phaseStatus") or []) if isinstance(row, dict)],
            "store": sampled_counters(samples, "textureStats", STORE_KEYS),
            "legacyTextureCounters": sampled_counters(samples, None, LEGACY_KEYS),
            "webglLog": webgl_log(directory / "renderer.log", issues),
            "evidenceFiles": [{"filename": filename, "type": kind} for filename, kind in EVIDENCE_TYPES.items()
                              if (directory / filename).is_file()], "readIssues": issues}


def aggregate(runs):
    return {"schema": "amadeus.texture-probe-summary.v1", "units": "bytes and milliseconds",
            "limitations": ["Process memory groups are separate; GPU process memory is not texture allocation or VRAM.",
                            "Store bytes count retained CPU payload plus potential GPU payload, independently of process memory.",
                            "Tail windows describe samples; settlement is unverified, including while loading.",
                            "Journey gap percentiles aggregate raw events; per-phase spikes remain separately reported.",
                            "Misses count recorded display attempts, changes count observed texture identities, holds count hold-state observations.",
                            "Uninstrumented legacy phase load counters are unavailable; store counters are reported separately.",
                            "Context restoration events alone do not validate displayed pixels; offscreen capture is not physical presentation."],
            "runs": [summarize_run(label, directory) for label, directory in sorted(runs)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, metavar="LABEL=DIRECTORY")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    runs = []
    for value in args.run:
        label, separator, directory = value.partition("=")
        if not separator or not identifier(label) or not directory:
            parser.error("Each --run requires a neutral identifier and directory: LABEL=DIRECTORY")
        if any(old_label == label for old_label, _ in runs):
            parser.error("Run labels must be unique")
        runs.append((label, directory))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(aggregate(runs), ensure_ascii=False, indent=2, sort_keys=True,
                                     allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
