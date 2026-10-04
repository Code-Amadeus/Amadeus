"""Sanitized summaries preserve measured units and incomplete evidence."""
import json

import pytest

from tools.probes.summarize_texture_runs import aggregate, compact_run, cpu_window, main, summarize_run


def test_cpu_intervals_keep_missing_counters_and_restarts_unavailable():
    rows = [{"elapsedMs": i * 1000, "rendererProcesses": [{"pid": pid, "cpuSeconds": cpu}]}
            for i, (pid, cpu) in enumerate([(1, 10), (1, 10.5), (2, 1), (2, 1.25),
                                           (2, None), (2, 2), (2, 1), (2, 1.5)])]
    cpu = cpu_window(rows)
    assert cpu["renderer"] == {"cpuSeconds": 1.25, "measuredWallSeconds": 3,
                               "measuredIntervals": 3, "missingIntervals": 4,
                               "meanSingleCorePercent": 1.25 / 3 * 100}
    assert cpu["gpu"]["cpuSeconds"] is None
    assert cpu["gpu"]["missingIntervals"] == 7
    assert "pid" not in json.dumps(cpu)


def test_compact_evidence_retains_adverse_soak_extremes_and_full_totals():
    run = {"phaseTotals": {"misses": 20}, "recording": {"complete": False}, "phases": [
        {"phase": "startup", "misses": 1, "tickerGaps": {"maxMs": 30}},
        {"phase": "soak-speech-1", "misses": 2, "tickerGaps": {"maxMs": 31}},
        {"phase": "soak-idle-2", "misses": 17, "tickerGaps": {"maxMs": 500}},
        {"phase": "context-loss", "misses": 0}]}
    compact = compact_run(run)
    assert [row["phase"] for row in compact["phases"]] == ["startup", "soak-idle-2", "context-loss"]
    assert compact["phaseTotals"] == {"misses": 20}
    assert compact["recording"]["complete"] is False
    assert compact["phaseSelection"]["recordedCount"] == 4


def write_run(directory, *, samples=(), metadata=None, summary=None, events=None):
    directory.mkdir(parents=True)
    (directory / "metadata.json").write_text(json.dumps(metadata or {
        "schema": "amadeus.texture-probe.v2", "revision": "a" * 40,
        "options": {"fps": 30, "sampling": False, "durationSeconds": 60},
        "versions": {"electron": "44.0.0", "chrome": "152.0.7977.54", "node": "24.18.1"},
        "offscreen": True, "offscreenFrameRate": 60,
        "host": {"environment": {"python": "3.12.10"}}}), encoding="utf-8")
    (directory / "summary.json").write_text(json.dumps(summary if summary is not None else {
        "complete": True, "error": None, "droppedEvents": 0, "sourceChangedDuringRun": [],
        "phases": [], "phaseStatus": []}), encoding="utf-8")
    (directory / "samples.ndjson").write_text("".join(json.dumps(row) + "\n" for row in samples), encoding="utf-8")
    if events is not None:
        (directory / "events.ndjson").write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
    return directory


def sample(elapsed, renderer=100, gpu_memory=200, host=300, **render):
    return {"elapsedMs": elapsed, "rendererPid": 9911,
            "rendererProcesses": [{"pid": 9911, "type": "Tab", "privateBytes": renderer, "rssBytes": renderer}],
            "gpuProcesses": [{"pid": 9922, "type": "GPU", "privateBytes": gpu_memory, "rssBytes": gpu_memory}],
            "host": {"pid": 9933, "privateBytes": host, "rssBytes": host}, "render": render}


def test_memory_groups_keep_bytes_separate_and_tail_is_descriptive(tmp_path):
    directory = write_run(tmp_path / "run", samples=[sample(0), sample(20_000, 300, 400, 500),
                         sample(40_000, 600, 700, 800, activeLoads=1)])
    run = summarize_run("candidate", directory)
    renderer = run["memory"]["renderer"]["privateBytes"]
    assert renderer["initial"] == 100
    assert renderer["peak"] == renderer["final"] == 600
    assert renderer["tail"] == {"min": 300, "max": 600, "mean": 450.0, "sampleCount": 2}
    assert run["memory"]["gpu"]["privateBytes"]["final"] == 700
    assert run["memory"]["host"]["privateBytes"]["final"] == 800
    assert run["tailWindow"]["observedDurationMs"] == 20_000
    assert run["tailWindow"]["requestedDurationMs"] == 30_000
    assert run["tailWindow"]["settlement"] == "unverified"
    assert run["tailWindow"]["loadingObserved"] is True


def test_journey_memory_excludes_optional_context_drop_and_uses_its_own_tail(tmp_path):
    samples = [{**sample(0), "stage": "journey"},
               {**sample(20_000, 300, 400, 500), "stage": "journey"},
               {**sample(40_000, 600, 700, 800, activeLoads=1), "stage": "journey-complete"},
               {**sample(45_000, 1000, 1100, 1200), "stage": "companion-suppressed"},
               {**sample(50_000, 800, 900, 1000), "stage": "scenario"},
               {**sample(55_000, 5, 6, 7), "stage": "context-loss"}]
    directory = write_run(tmp_path / "run", samples=samples)
    run = summarize_run("scoped", directory)
    assert run["memory"]["renderer"]["privateBytes"]["peak"] == 1000
    assert run["memory"]["renderer"]["privateBytes"]["final"] == 5
    assert run["tailWindow"]["endElapsedMs"] == 55_000
    journey = run["journeyMemory"]["renderer"]["privateBytes"]
    assert journey["initial"] == 100
    assert journey["peak"] == journey["final"] == 600
    assert journey["sampleCount"] == 3
    assert journey["tail"] == {"min": 300, "max": 600, "mean": 450.0, "sampleCount": 2}
    assert run["journeyMemory"]["gpu"]["privateBytes"]["final"] == 700
    assert run["journeyMemory"]["host"]["privateBytes"]["final"] == 800
    assert run["journeyTailWindow"]["available"] is True
    assert run["journeyTailWindow"]["scope"] == "journey and journey-complete samples only"
    assert run["journeyTailWindow"]["endElapsedMs"] == 40_000
    assert run["journeyTailWindow"]["settlement"] == "unverified"
    assert run["journeyTailWindow"]["loadingObserved"] is True


@pytest.mark.parametrize("stages", [(None, None), ("journey", None)])
def test_missing_sample_stages_do_not_infer_a_journey_from_phase_names(tmp_path, stages):
    rows = []
    for elapsed, stage in zip((0, 1000), stages):
        row = {**sample(elapsed), "phase": "startup" if elapsed == 0 else "first-speech"}
        if stage is not None:
            row["stage"] = stage
        rows.append(row)
    directory = write_run(tmp_path / "run", samples=rows)
    run = summarize_run("unstaged", directory)
    assert run["journeyMemory"] is None
    assert run["journeyTailWindow"] == {"available": False,
        "scope": "journey and journey-complete samples only", "reason": "missing-stage-labels"}
    assert run["memory"]["renderer"]["privateBytes"]["sampleCount"] == 2
    assert run["tailWindow"]["scope"] == "all recorded samples"


def test_optional_only_stage_evidence_has_no_journey_memory(tmp_path):
    directory = write_run(tmp_path / "run", samples=[{**sample(0), "stage": "context-loss"}])
    run = summarize_run("optional-only", directory)
    assert run["journeyMemory"] is None
    assert run["journeyTailWindow"]["available"] is False
    assert run["journeyTailWindow"]["reason"] == "no-journey-samples"


def test_journey_gaps_aggregate_weighted_raw_events_and_exclude_optional_phases(tmp_path):
    metadata = {"revision": "a" * 40, "cpuProfiling": True,
                "journey": [{"phase": "short", "at": 0}, {"phase": "long", "at": 1}]}
    summary = {"complete": True, "phases": [
        {"name": "short", "tickerGaps": {"count": 1, "p99Ms": 100}, "rafGaps": {"count": 1, "p99Ms": 50}},
        {"name": "long", "tickerGaps": {"count": 100, "p99Ms": 10}, "rafGaps": {"count": 100, "p99Ms": 16}},
        {"name": "context-loss", "tickerGaps": {"count": 1, "p99Ms": 3000}},
    ]}
    events = [{"phase": "short", "kind": "tick", "dt": 100}, {"phase": "short", "kind": "raf", "dt": 50}]
    events.extend({"phase": "long", "kind": "tick", "dt": 10} for _ in range(100))
    events.extend({"phase": "long", "kind": "raf", "dt": 16} for _ in range(100))
    for phase, gap in (("companion", 1000), ("scenario", 2000), ("context-loss", 3000)):
        events.extend({"phase": phase, "kind": kind, "dt": gap} for kind in ("raf", "tick"))
    events.extend({"phase": "long", "kind": "tick", "dt": value} for value in (None, "120", True, -1))
    directory = write_run(tmp_path / "run", metadata=metadata, summary=summary, events=events)
    run = summarize_run("raw-gaps", directory)
    gaps = run["journeyGaps"]
    assert gaps["available"] is True
    assert gaps["scope"] == "raw RAF and ticker events in metadata.journey action phases only"
    assert gaps["tickerGaps"] == {"count": 101, "p50Ms": 10, "p95Ms": 10, "p99Ms": 10, "maxMs": 100, "over50Ms": 1}
    assert gaps["rafGaps"] == {"count": 101, "p50Ms": 16, "p95Ms": 16, "p99Ms": 16, "maxMs": 50, "over50Ms": 0}
    assert gaps["tickerGaps"]["p99Ms"] != (100 + 10) / 2, 'phase percentiles are never averaged'
    assert run["phases"][0]["tickerGaps"]["p99Ms"] == 100, 'the short phase spike remains visible'
    assert run["phases"][2]["tickerGaps"]["p99Ms"] == 3000, 'optional phase evidence remains separately reported'
    assert run["metadata"]["cpuProfiling"] is True


@pytest.mark.parametrize("missing", ["metadata", "journey", "events", "empty-events"])
def test_journey_gaps_require_explicit_labels_and_raw_events(tmp_path, missing):
    directory = write_run(tmp_path / "run", metadata={"revision": "a" * 40, "journey": [{"phase": "startup"}]},
                          events=[{"phase": "startup", "kind": "tick", "dt": 33}])
    if missing == "metadata":
        (directory / "metadata.json").unlink()
    elif missing == "journey":
        (directory / "metadata.json").write_text(json.dumps({"revision": "a" * 40}), encoding="utf-8")
    elif missing == "events":
        (directory / "events.ndjson").unlink()
    else:
        (directory / "events.ndjson").write_text("", encoding="utf-8")
    gaps = summarize_run("missing-gap-evidence", directory)["journeyGaps"]
    assert gaps["available"] is False
    assert gaps["reason"] == {"metadata": "missing-journey-phase-labels", "journey": "missing-journey-phase-labels",
                              "events": "missing-events", "empty-events": "no-valid-journey-gaps"}[missing]
    assert gaps["scope"] == "raw RAF and ticker events in metadata.journey action phases only"
    assert "rafGaps" not in gaps and "tickerGaps" not in gaps


def test_electron_memory_kib_is_converted_once_and_gpu_processes_sum(tmp_path):
    older = {"elapsedMs": 0, "rendererPid": 111,
             "processes": [{"pid": 111, "type": "Tab", "memoryKiB": {"privateBytes": 2, "workingSetSize": 3}},
                           {"pid": 222, "type": "GPU", "memoryKiB": {"privateBytes": 5, "workingSetSize": 6}},
                           {"pid": 333, "type": "GPU", "memoryKiB": {"privateBytes": 7, "workingSetSize": 8}},
                           {"pid": 444, "type": "Browser", "memoryKiB": {"privateBytes": 999}}],
             "host": {"privateBytes": 99, "rssBytes": 100}}
    directory = write_run(tmp_path / "run", samples=[older, sample(1000, 4096, 8192, 123)])
    run = summarize_run("units", directory)
    assert run["memory"]["renderer"]["privateBytes"]["initial"] == 2048
    assert run["memory"]["renderer"]["rssBytes"]["initial"] == 3072
    assert run["memory"]["gpu"]["privateBytes"]["initial"] == 12 * 1024
    assert run["memory"]["gpu"]["rssBytes"]["initial"] == 14 * 1024
    assert run["memory"]["host"]["privateBytes"]["initial"] == 99
    assert run["memory"]["renderer"]["privateBytes"]["final"] == 4096


def test_missing_group_or_partial_process_memory_is_unavailable_not_zero(tmp_path):
    row = sample(0)
    row["gpuProcesses"].append({"pid": 42, "type": "GPU", "privateBytes": None})
    row["rendererProcesses"] = []
    row["host"] = {}
    directory = write_run(tmp_path / "run", samples=[row])
    run = summarize_run("missing", directory)
    for group in ("renderer", "gpu", "host"):
        metric = run["memory"][group]["privateBytes"]
        assert metric["initial"] is None and metric["peak"] is None and metric["final"] is None
        assert metric["missingSamples"] == 1


def test_phase_attempts_are_not_inflated_by_cumulative_store_snapshots(tmp_path):
    phases = [{"name": "first-speech", "durationMs": 5000, "misses": 3, "changes": 7, "holds": 5, "cycles": 2,
               "rafGaps": {"count": 300, "p99Ms": 17}, "tickerGaps": {"count": 150, "p99Ms": 51}}]
    summary = {"complete": True, "droppedEvents": 0, "sourceChangedDuringRun": [], "phases": phases}
    directory = write_run(tmp_path / "run", summary=summary, samples=[
        sample(0, textureStats={"residentBytes": 10, "budgetBytes": 20, "displayMiss": 99}),
        sample(5000, textureStats={"residentBytes": 15, "budgetBytes": 20, "displayMiss": 100})])
    run = summarize_run("attempts", directory)
    assert run["phaseTotals"]["misses"] == 3
    assert run["phaseTotals"]["changes"] == 7
    assert run["phaseTotals"]["cycles"] == 2
    assert run["phaseTotals"]["holds"] == 5
    assert run["phaseTotals"]["loadsStarted"] is None
    assert run["phases"][0]["tickerGaps"] == {"count": 150, "p99Ms": 51}
    assert run["store"]["last"]["displayMiss"] == 100
    assert run["store"]["peak"]["residentBytes"] == 15
    assert run["store"]["peak"]["budgetBytes"] == 20
    assert run["phaseMetricsSource"] == "summary.json"


@pytest.mark.parametrize("legacy_loads", [False, True, None, "absent"])
def test_uninstrumented_legacy_phase_loads_are_unavailable_while_store_counters_remain_measured(tmp_path, legacy_loads):
    fields = ("loadsStarted", "loadsCompleted", "loadFailures", "refetches", "loadedPerSecond")
    metadata = {"revision": "a" * 40}
    if legacy_loads != "absent":
        metadata["instrumentation"] = {"legacyLoads": legacy_loads, "textureStats": True}
    phase = {"name": "first-speech", "misses": 4, "changes": 7, **dict.fromkeys(fields, 0)}
    directory = write_run(tmp_path / "run", metadata=metadata,
        summary={"complete": True, "phases": [phase]},
        samples=[sample(5000, textureStats={"loads": 23, "failures": 1, "refetches": 5})])
    run = summarize_run("load-evidence", directory)
    expected = None if legacy_loads is False else 0
    assert {field: run["phases"][0][field] for field in fields} == dict.fromkeys(fields, expected)
    assert {field: run["phaseTotals"][field] for field in fields[:-1]} == dict.fromkeys(fields[:-1], expected)
    assert run["phaseTotals"]["misses"] == 4
    assert run["phaseTotals"]["changes"] == 7
    assert run["store"]["last"] == {"loads": 23, "failures": 1, "refetches": 5}
    assert run["store"]["peak"]["refetches"] == 5


def test_optional_events_can_rebuild_phase_gaps_and_real_attempt_counts(tmp_path):
    events = [{"phase": "startup", "kind": "phase-start", "t": 0},
              {"phase": "startup", "kind": "tick", "dt": None},
              *({"phase": "startup", "kind": "tick", "dt": value} for value in (10, 20, 100)),
              {"phase": "startup", "kind": "raf", "dt": 16.7},
              {"phase": "startup", "kind": "miss", "label": "private-name", "asset": "file:///private"},
              {"phase": "startup", "kind": "miss"}, {"phase": "startup", "kind": "change"},
              {"phase": "startup", "kind": "hold"}, {"phase": "startup", "kind": "cycle"},
              {"phase": "startup", "kind": "load-start", "attempt": 2},
              {"phase": "startup", "kind": "load-end", "ok": False},
              {"phase": "startup", "kind": "phase-end", "t": 1000}]
    directory = write_run(tmp_path / "run", summary={"complete": False}, events=events)
    run = summarize_run("events", directory)
    row = run["phases"][0]
    assert run["phaseMetricsSource"] == "events.ndjson"
    assert row["tickerGaps"] == {"count": 3, "p50Ms": 20, "p95Ms": 100, "p99Ms": 100, "maxMs": 100, "over50Ms": 1}
    assert row["rafGaps"]["count"] == 1
    assert row["misses"] == 2 and row["changes"] == 1
    assert row["holds"] == row["cycles"] == row["refetches"] == row["loadFailures"] == 1
    assert "private-name" not in json.dumps(run)


def test_metadata_is_allowlisted_and_pci_ids_paths_names_and_process_ids_do_not_escape(tmp_path):
    metadata = {"revision": "a" * 40, "cpuProfiling": True, "name": "Fixture Person", "owner": "Fixture Owner", "pid": 999111,
                "source": "C:\\Users\\<user>\\private", "sourceSha256": {
                    "render/web/renderer.js": "b" * 64, "C:/Users/<user>/private.py": "c" * 64},
                "options": {"fps": 30, "sampling": False, "output": "/home/<user>/private"},
                "versions": {"electron": "44.0.0", "chrome": "152.0.7977.54", "node": "24.18.1", "privateName": "Fixture Person"},
                "host": {"pid": 991122, "url": "http://127.0.0.1:51234/private", "deviceName": "private-host",
                         "environment": {"python": "3.12.10", "hostname": "private-host"},
                         "pack": {"id": "kurisu", "version": "2026.08.29-mouth1",
                                  "sourceSha256": {"runtime_manifest.json": "d" * 64}}}}
    directory = write_run(tmp_path / "run", metadata=metadata, samples=[sample(0,
        gpu="ANGLE (AMD, AMD Radeon 780M Graphics (0x00001900) Direct3D11)")])
    run = summarize_run("safe", directory)
    encoded = json.dumps(run)
    for private in ("Fixture Person", "Fixture Owner", "<user>", "private-host", "127.0.0.1", "51234", "0x00001900", "9911", "9922", "9933", "999111", str(tmp_path)):
        assert private not in encoded
    assert run["metadata"]["gpuModels"] == ["ANGLE (AMD, AMD Radeon 780M Graphics Direct3D11)"]
    assert run["metadata"]["versions"]["python"] == "3.12.10"
    assert run["metadata"]["cpuProfiling"] is True
    assert run["metadata"]["sourceSha256"] == {"render/web/renderer.js": "b" * 64}
    assert run["metadata"]["pack"]["manifestSha256"] == "d" * 64
    assert all(set(row) == {"filename", "type"} for row in run["evidenceFiles"])


def test_failed_runs_keep_failure_and_dropped_or_changed_evidence_without_raw_errors(tmp_path):
    directory = write_run(tmp_path / "run", summary={"complete": False,
        "error": "Failed C:/Users/<user>/private at http://127.0.0.1:51234", "droppedEvents": 8,
        "sourceChangedDuringRun": ["render/web/renderer.js", "C:/Users/<user>/private.py"],
        "phases": [], "phaseStatus": None})
    run = summarize_run("failed", directory)
    assert run["recording"]["complete"] is False
    assert run["recording"]["errorRecorded"] is True
    assert run["recording"]["droppedEvents"] == 8
    assert run["recording"]["sourceChangedCount"] == 2
    assert run["recording"]["sourceChangedDuringRun"] == ["render/web/renderer.js"]
    assert run["phaseTotals"]["misses"] is None
    assert run["tailWindow"]["sampleCount"] == 0
    assert "<user>" not in json.dumps(run) and "127.0.0.1" not in json.dumps(run)


def test_optional_phase_statuses_and_context_pixels_preserve_verification_scope(tmp_path):
    status = [{"phase": "scenario", "status": "requested", "activation": "unverified", "reason": "private reason"},
              {"phase": "companion", "status": "exercised", "suppressed": True, "restored": True},
              {"phase": "context-loss", "status": "exercised", "restored": True}]
    directory = write_run(tmp_path / "run", summary={"complete": True, "phases": [], "phaseStatus": status})
    run = summarize_run("old-context", directory)
    assert run["phaseStatus"][0]["activation"] == "unverified"
    assert run["phaseStatus"][0]["status"] == "requested"
    assert run["phaseStatus"][2]["visualValidation"] == "unverified"
    assert "private reason" not in json.dumps(run)
    pixels = {"width": 960, "height": 1080, "hash": "a123bc45", "rgbSum": 400, "alphaSum": 500,
              "glErrors": [1280], "deviceId": "private-device", "pid": 9911}
    status[-1].update(status="failed", pixelMatch=False, beforePixels=pixels, afterPixels={**pixels, "rgbSum": 0})
    (directory / "summary.json").write_text(json.dumps({"complete": False, "phases": [], "phaseStatus": status}))
    checked = summarize_run("new-context", directory)["phaseStatus"][2]
    assert checked["visualValidation"] == "recorded"
    assert checked["pixelMatch"] is False
    assert checked["beforePixels"]["glErrors"] == [1280]
    assert checked["afterPixels"]["rgbSum"] == 0
    assert checked["beforePixels"]["hash"] == "a123bc45"
    assert "deviceId" not in checked["beforePixels"] and "pid" not in checked["beforePixels"]


def test_webgl_errors_are_counted_as_lines_without_copying_log_content(tmp_path):
    directory = write_run(tmp_path / "run")
    (directory / "renderer.log").write_text(
        'WebGL: INVALID_ENUM: C:/Users/<user>/private\n'
        'WebGL: INVALID_ENUM: INVALID_VALUE: private-device\n'
        'WebGL: INVALID_OPERATION\n'
        'WebGL: INVALID_FRAMEBUFFER_OPERATION\n'
        'Unrelated INVALID_ENUM\n'
        'WebGL: normal informational line\n', encoding="utf-8")
    run = summarize_run("log", directory)
    assert run["webglLog"]["errorLines"] == 4
    assert run["webglLog"]["byKind"] == {"INVALID_ENUM": 2, "INVALID_VALUE": 1,
                                         "INVALID_OPERATION": 1, "INVALID_FRAMEBUFFER_OPERATION": 1}
    assert "<user>" not in json.dumps(run) and "private-device" not in json.dumps(run)


def test_cli_is_deterministic_and_orders_neutral_labels_without_directory_names(tmp_path):
    first = write_run(tmp_path / "private-run-pid-1111", samples=[sample(0)])
    second = write_run(tmp_path / "private-run-pid-2222", samples=[sample(0)])
    output_a, output_b = tmp_path / "one.json", tmp_path / "two.json"
    main(["--run", f"candidate={second}", "--run", f"baseline={first}", "--output", str(output_a)])
    main(["--run", f"baseline={first}", "--run", f"candidate={second}", "--output", str(output_b)])
    assert output_a.read_bytes() == output_b.read_bytes()
    assert [row["label"] for row in json.loads(output_a.read_text())["runs"]] == ["baseline", "candidate"]
    assert "private-run-pid" not in output_a.read_text()
    assert aggregate([("candidate", second), ("baseline", first)]) == json.loads(output_a.read_text())
    with pytest.raises(SystemExit):
        main(["--run", f"same={first}", "--run", f"same={second}", "--output", str(output_a)])


def test_missing_or_malformed_recordings_report_sanitized_read_issues(tmp_path):
    directory = write_run(tmp_path / "run", summary={"complete": False})
    (directory / "metadata.json").unlink()
    (directory / "samples.ndjson").write_text('{"elapsedMs":0}\nnot-json\n', encoding="utf-8")
    run = summarize_run("partial", directory)
    assert run["readIssues"] == [{"file": "metadata.json", "issue": "missing"},
                                 {"file": "samples.ndjson", "issue": "invalid-record", "line": 2}]
    assert run["recording"]["sampleCount"] == 1
    assert run["phaseMetricsSource"] == "unavailable"
    assert run["webglLog"]["errorLines"] is None
    assert run["metadata"]["revision"] is None
    assert str(tmp_path) not in json.dumps(run)
