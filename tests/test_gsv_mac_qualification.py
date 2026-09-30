from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from tools.probes import gsv_mac_qualification as qualification


HASH = "1" * 64
SECRET = "C:\\Users\\PrivateVolunteer\\reference.wav private reference transcript private-host"


def args_for(tmp_path, *extra):
    return qualification.parse_args([
        "--checkpoint", str(tmp_path / "gpt.ckpt"),
        "--sovits", str(tmp_path / "selected.pth"),
        "--reference-audio", str(tmp_path / "reference.wav"),
        "--reference-text", "private reference transcript",
        "--output", str(tmp_path / "result"), *extra])


def profiles():
    return {case[0]: {
        "top_k": 7, "top_p": 0.9, "temperature": 0.73,
        "sample_steps": (4, 16, 32, 4, 16)[index], "speed": 1.1 if case[2] else 1,
        "pause_second": 0.05, "how_to_cut": "不切", "max_sec_override": 3.5 if case[2] else None,
    } for index, case in enumerate(qualification.CASES)}


def environment(device="cpu"):
    return {"os": "Darwin" if device == "metal" else "Windows", "os_release": "1.0",
            "macos_version": "26.0" if device == "metal" else None,
            "architecture": "arm64" if device == "metal" else "AMD64",
            "chip": "Apple M4" if device == "metal" else None,
            "chip_tier": "base" if device == "metal" else "unknown",
            "system_memory_bytes": 16 * 1024**3, "python": "3.11.9"}


def binding_for(args):
    source = {"base_sha": "a" * 40, "candidate_sha": "b" * 40,
              "working_tree_dirty": False, "code_sha256": HASH, "runner_sha256": HASH,
              "inference_source_sha256": HASH}
    sources = {}
    for variant, (backend, _, revision) in qualification.VARIANT_SPECS.items():
        sources[variant] = {"mlx_revision": revision, "model_source_commit": "c" * 40 if revision in {"audit", "unpadded"}
                            else source["base_sha" if backend == "torch" else "candidate_sha"],
                            "source_kind": "frozen_snapshot" if revision in {"audit", "unpadded"} else "working_tree",
                            "source_sha256": {"model": HASH, "generation": HASH}}
    protocol = {key: getattr(args, key) for key in (
        "device", "smoke", "history_steps", "stage_runs", "fixed_blocks", "fixed_runs",
        "natural_blocks", "natural_runs", "soak_runs", "warmup", "seed", "chunk_seconds",
        "include_listening_wavs", "blind_seed", "timeout")}
    return {"schema": qualification.SCHEMA, "source": source,
            "assets": {**{key: HASH for key in qualification.ASSET_FIELDS}, "pretrained_sovits_sha256": HASH},
            "frontend_assets": {}, "variant_sources": sources,
            "reference_text_sha256": hashlib.sha256(args.reference_text.encode()).hexdigest(),
            "case_text_sha256": {name: hashlib.sha256(text.encode()).hexdigest()
                                 for name, text, _ in qualification.CASES},
            "dependencies": {name: {"status": "available", "version": "1.0"} for name in qualification.DEPENDENCIES},
            "installed_dependencies_sha256": HASH, "configuration_sha256": HASH,
            "machine": environment(args.device), "protocol": protocol, "profiles": profiles(),
            "smoke_protocol": {"qualifying": False} if args.smoke else None}


def options_of(options):
    flags = {"--fixed-only", "--characterize", "--probe-mlx-cpu", "--stage-profile", "--soak-memory"}
    result, index = {}, 0
    while index < len(options):
        key = str(options[index])
        result[key] = True if key in flags else str(options[index + 1])
        index += 1 if key in flags else 2
    return result


def fixed_distribution(values):
    return {variant: {"runs": 20, "fixed_ar_work_ms": qualification._stats([values[variant]] * 20)}
            for variant in qualification.VARIANTS}


class RecordingRunner:
    """Only plan evidence; no model or subprocess is executed."""

    def __init__(self, args):
        self.args, self.root = args, Path(args.output)
        self.reports, self.steps = {}, []
        self.fixture, self.cohort = None, None

    def step(self, name, kind, options, spec, artifacts=()):
        self.steps.append((name, kind, options_of(options), spec, artifacts))
        if kind == "inputs":
            result = {"fixed_decode_steps": self.args.history_steps, "fixture_sha256": HASH}
        elif kind == "bench":
            # Audit is fastest, and Torch half captures less than 80% of its saving.
            cost = {"torch_fp32": 100, "torch_fp16": 80, "mlx_audit_fp32": 25,
                    "mlx_unpadded_fp32": 60, "mlx_unpadded_fp16": 45,
                    "mlx_padded_fp32": 50, "mlx_padded_fp16": 35}[spec["variant"]]
            result = {"backend": qualification.VARIANT_SPECS[spec["variant"]][0], "raw_fixed_runs": [
                {"seed": spec["seed"] + index, "fixed_ar_work_ms": cost, "prefill_ms": 1}
                for index in range(spec["runs"])]}
        else:
            result = {}
        self.reports[name] = result
        return result


def test_formal_plan_measures_all_seven_and_real_sampling_before_stage_and_selected_soak(tmp_path):
    args = args_for(tmp_path)
    runner = RecordingRunner(args)
    qualification._measure(runner, profiles())
    fixed = [step for step in runner.steps if step[1] == "bench"]
    assert [step[3]["variant"] for step in fixed] == [*qualification.VARIANTS, *reversed(qualification.VARIANTS)]
    assert all(sum(step[3]["runs"] for step in fixed if step[3]["variant"] == variant) >= 20
               for variant in qualification.VARIANTS)
    assert all(step[2]["--fixed-only"] is True and step[3]["sampling"]["temperature"] == 0.73
               and step[3]["sampling"]["top_k"] == 7 and step[3]["sampling"]["top_p"] == 0.9 for step in fixed)
    validates = [step for step in runner.steps if step[1] == "validate"]
    assert [step[3]["variant"] for step in validates] == list(qualification.MLX_VARIANTS)
    assert all(bool(step[2].get("--characterize")) == (step[3]["dtype"] == "float16") for step in validates)
    stages = [step for step in runner.steps if step[3].get("stage")]
    assert [int(step[2]["--sample-steps"]) for step in stages] == [4, 16, 32]
    assert all(step[3]["runs"] >= 10 and step[3]["variant"] == "torch_fp32" for step in stages)
    assert max(runner.steps.index(step) for step in fixed) < min(runner.steps.index(step) for step in stages)
    assert runner.cohort["candidate"] == "mlx_audit_fp32"
    natural = [step for step in runner.steps if step[0].startswith("natural_")]
    expected = runner.cohort["e2e_variants"]
    assert [step[3]["variant"] for step in natural] == [*expected, *reversed(expected)]
    assert all(sum(step[3]["runs"] for step in natural if step[3]["variant"] == variant) >= 20 for variant in expected)
    assert {variant for pair in qualification.PADDING_PAIRS.values() for variant in pair} <= set(expected)
    soak = [step for step in runner.steps if step[3].get("soak")]
    assert len(soak) == 1 and soak[0][3]["variant"] == "mlx_audit_fp32" and soak[0][3]["runs"] >= 100


def test_smoke_plan_traverses_every_phase_with_short_audio_and_deterministic_candidate(tmp_path):
    runner = RecordingRunner(args_for(tmp_path, "--device", "cpu", "--smoke"))
    qualification._measure(runner, profiles())
    assert runner.fixture["fixed_decode_steps"] == 2
    assert {step[3]["variant"] for step in runner.steps if step[1] == "bench"} == set(qualification.VARIANTS)
    assert runner.cohort["candidate"] == "mlx_padded_fp32"  # Ignore the fake fast audit times.
    assert runner.cohort["selection_basis"] == "deterministic_smoke_coverage"
    assert runner.cohort["torch_fp16_fraction_of_best_mlx_work_saved"] is None
    audio = [step for step in runner.steps if step[1] == "audio"]
    assert all(step[2]["--text"] == qualification.SMOKE_TEXT
               and float(step[2]["--max-sec-override"]) == qualification.SMOKE_MAX_SEC for step in audio)
    assert [int(step[2]["--sample-steps"]) for step in audio if step[3]["stage"]] == [4, 16, 32]
    assert any(step[3]["soak"] and step[3]["runs"] == 2 for step in audio)


@pytest.mark.parametrize(("torch_half", "included"), [(50, True), (55, False), (100, False)])
def test_candidate_selection_includes_audit_and_competitive_torch_half(torch_half, included):
    costs = dict(zip(qualification.VARIANTS, (100, torch_half, 40, 60, 50, 55, 45)))
    cohort = qualification.select_cohort(fixed_distribution(costs), qualifying=True)
    assert cohort["candidate"] == "mlx_audit_fp32"
    assert ("torch_fp16" in cohort["e2e_variants"]) is included
    assert {variant for pair in qualification.PADDING_PAIRS.values() for variant in pair} <= set(cohort["e2e_variants"])


@pytest.mark.parametrize(("decode", "first", "recommendation"), [(90, 90, "retain"), (100, 90, "remove"),
                                                                  (90, 100, "remove"), (110, 120, "remove")])
def test_padding_requires_both_matched_dtype_improvements(decode, first, recommendation):
    values = {variant: 100 for variant in qualification.VARIANTS}
    values["mlx_padded_fp32"] = decode
    values["mlx_unpadded_fp16"], values["mlx_padded_fp16"] = 30, 40
    natural = {variant: {"first_emitted_chunk_ms": qualification._stats([100])} for variant in qualification.VARIANTS}
    natural["mlx_padded_fp32"]["first_emitted_chunk_ms"] = qualification._stats([first])
    result = qualification.padding_assessment(fixed_distribution(values), natural, qualifying=True)
    assert result["by_dtype"]["float32"]["recommendation"] == recommendation
    assert result["by_dtype"]["float16"]["recommendation"] == "remove"
    assert result["recommendation"] == "pending_deployment_dtype"
    assert result["other_optimizations"] == "retained" and result["source_mutation_performed"] is False
    assert qualification.padding_assessment({}, {}, qualifying=False)["by_dtype"] == {}


class FakeProbe:
    """Valid deterministic reports at the real Runner subprocess boundary."""

    def __init__(self, binding):
        self.binding, self.fixture, self.calls = binding, None, []
        self.fail_kind, self.mutate_report = None, None

    def __call__(self, command, **kwargs):
        kind, options = command[2], options_of(command[3:])
        self.calls.append((kind, options))
        assert kwargs["env"]["FIRST_SENTENCE_AUDIO_CACHE_ENABLED"] == "0"
        assert kwargs["env"]["TTS_RUNTIME_WARMUP"] == "0"
        path = Path(options["--output"])
        report = self.report(kind, options)
        if self.mutate_report:
            self.mutate_report(kind, report)
        path.write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=1 if kind == self.fail_kind else 0, stdout=SECRET, stderr="")

    def report(self, kind, options):
        binding = self.binding
        report = {"schema": "amadeus.gsv_mlx_probe.v1", "command": kind, "status": "passed",
                  **binding["source"], **{key: binding["assets"][key] for key in qualification.ASSET_FIELDS},
                  "environment": {**binding["machine"], "hostname": SECRET,
                                  **{key: "1.0" for key in ("torch", "torchaudio", "mlx", "mlx_metal", "mlx_cpu")}},
                  "reference_text": SECRET, "private_path": SECRET}
        device = binding["protocol"]["device"]
        if kind == "doctor":
            report.update(mlx_device=device, dtype="float32", torch_mps_available=device == "metal")
        elif kind == "export":
            report["converted_weights_sha256"] = HASH
        elif kind == "inputs":
            fixture = Path(options["--fixture-output"])
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_bytes(b"private derived fixture")
            self.fixture = {"fixture_kind": "frontend_private", "fixture_sha256": qualification._sha(fixture),
                            "fixed_decode_steps": int(options["--history-steps"]), "reference_tokens": 10, "phone_length": 8}
            report.update(self.fixture)
        else:
            backend = "mlx" if kind == "validate" else options["--backend"]
            dtype, revision = options["--inference-dtype"], options.get("--mlx-revision", "current")
            variant = next(name for name, spec in qualification.VARIANT_SPECS.items()
                           if spec == (backend, dtype, revision if backend == "mlx" else "not_applicable"))
            report.update(backend=backend, execution={**binding["variant_sources"][variant], "inference_dtype": dtype,
                                                      "sampler_dtype": "float32" if backend == "mlx" else dtype})
            if kind in {"validate", "bench"}:
                report.update(self.fixture, inputs_sha256=self.fixture["fixture_sha256"])
            if kind == "validate":
                rows = [{"step": index, "tensor": "logits", "finite": True, "passed": True, "max_abs": 0,
                         "rmse": 0, "atol": 0.001, "rtol": 0.001, "top1_agrees": True, "top5_overlap": 1,
                         "private_transcript": SECRET} for index in range(self.fixture["fixed_decode_steps"] + 1)]
                report.update(status="recorded" if dtype == "float16" else "passed", mlx_device=device,
                              inference_dtype=dtype, rows=rows, comparisons=len(rows), logit_steps=len(rows),
                              top1_agreements=len(rows), max_logit_abs=0, failed_comparisons=[], strict_tolerance_status="passed")
            elif kind == "bench":
                n, seed, count = self.fixture["fixed_decode_steps"], int(options["--seed"]), int(options["--runs"])
                report["execution"]["fixed_only"] = True
                rows = [{"seed": seed + index, "prefill_ms": 3, "fixed_ar_work_ms": n * 2,
                         "fixed_step_ms": [2] * n, "final_drain_ms": 0, "sampled_ids_sha256": HASH,
                         "decode_calls": n, "sampler_calls": n, "host_stop_reads": n,
                         "final_audio_length": self.fixture["reference_tokens"] + n, "private_path": SECRET}
                        for index in range(count)]
                report.update(torch_device="cpu" if device == "cpu" else "mps",
                              mlx_device=device if backend == "mlx" else "not_applicable",
                              raw_fixed_runs=rows, per_position_ms=[qualification._stats([2] * count)] * n,
                              free_generation_outputs=[], warmup_runs_excluded=int(options["--warmup"]), measured_seed_start=seed,
                              sampling_parameters={"top_k": int(options["--top-k"]), "top_p": float(options["--top-p"]),
                                                   "temperature": float(options["--temperature"]),
                                                   "repetition_penalty": float(options["--repetition-penalty"]),
                                                   "early_stop_num": int(options["--budget"])})
            elif kind == "audio":
                stage, soak = bool(options.get("--stage-profile")), bool(options.get("--soak-memory"))
                seed, count, warmup = int(options["--seed"]), int(options["--runs"]), int(options["--warmup"])
                def audio_row(row_seed):
                    memory = {key: 1000 if key == "rss_bytes" or device == "metal" else None
                              for key in qualification.MEMORY_FIELDS}
                    timings = [{"stage": name, "elapsed_ms": 1} for name in sorted(qualification.STAGES)] if stage else []
                    return {"seed": row_seed, "first_emitted_chunk_ms": 100, "total_synthesis_ms": 120,
                            "audio_seconds": 0.5, "rtf": 0.24, "sample_rate": 24000, "chunks": 1,
                            "semantic_tokens": 26, "semantic_attempts": 1, "semantic_segments": 1,
                            "semantic_budget_boundary_segments": 1, "semantic_total_ms": 10,
                            "bridge_in_ms": 0, "bridge_out_ms": 0, "peak_abs_sample": 0.5, "rms": 0.1,
                            "clipping_fraction": 0, "rss_bytes": 1000, "first_chunk_semantic_ms": 10 if stage else None,
                            "stage_timings": timings, "first_chunk_stage_timings": timings,
                            "memory": memory if soak else None, "wav_path": SECRET, "semantic_text": SECRET}
                rows, warmups = [audio_row(seed + index) for index in range(count)], [audio_row(seed + 1_000_000 + index) for index in range(warmup)]
                parameters = {key: float(options["--" + key.replace("_", "-")]) for key in (
                    "top_k", "top_p", "temperature", "speed", "pause_second", "sample_steps")}
                parameters.update(how_to_cut=options["--how-to-cut"],
                                  max_sec_override=float(options["--max-sec-override"]) if "--max-sec-override" in options else None,
                                  chunk_seconds=float(options["--chunk-seconds"]) or None, semantic_guard=True)
                report.update(semantic={"semantic_backend": backend}, semantic_dtype=dtype, acoustic_dtype="float32",
                              acoustic_device="cpu" if device == "cpu" else "mps", sampler_dtype="float32" if backend == "mlx" else dtype,
                              text_sha256=hashlib.sha256(options["--text"].encode()).hexdigest(),
                              prompt_text_sha256=binding["reference_text_sha256"], parameters=parameters,
                              controlled_acoustic_rng=False, stage_profile=stage, soak_memory=soak, worker_thread=soak,
                              runs=rows, warmup_rows=warmups, cold_run=warmups[0],
                              warmup_runs_excluded=warmup, measured_seed_start=seed)
                if "--wav" in options:
                    wav = Path(options["--wav"])
                    wav.parent.mkdir(parents=True, exist_ok=True)
                    for index in range(count):
                        wav.with_name(f"{wav.stem}-{index + 1}.wav").write_bytes(b"RIFF synthetic sample")
        return report


def install_fake_run(monkeypatch, args):
    binding = binding_for(args)
    probe = FakeProbe(binding)
    monkeypatch.setattr(qualification, "_profiles", profiles)
    monkeypatch.setattr(qualification, "_machine_environment", lambda: environment(args.device))
    monkeypatch.setattr(qualification, "_binding", lambda *_: deepcopy(binding))
    monkeypatch.setattr(qualification, "_run_child", probe)
    return probe, binding


def test_full_smoke_packages_only_sanitized_evidence_and_resume_reuses_every_child(tmp_path, monkeypatch):
    args = args_for(tmp_path, "--device", "cpu", "--smoke")
    probe, _ = install_fake_run(monkeypatch, args)
    result = qualification.run(args)
    assert result["status"] == "recorded_nonqualifying"
    assert result["suggested_numeric_gates"]["candidates"] == {}
    assert result["padding_assessment"]["recommendation"] == "not_assessed"
    assert result["runtime_target_met"] is None
    assert result["human_listening"] == result["asr"] == "pending"
    assert len(result["completed_steps"]) == 32
    assert all(result["fixed_model"][variant]["runs"] == 2 for variant in qualification.VARIANTS)
    assert result["natural_first_chunk"]["mlx_unpadded_fp32"]["runs"] == 2
    archive = Path(args.output) / "share.zip"
    with zipfile.ZipFile(archive) as bundle:
        assert all(name.endswith(".json") and not name.startswith("private/") for name in bundle.namelist())
        payload = b"\n".join(bundle.read(name) for name in bundle.namelist()).decode()
        assert SECRET not in payload and args.reference_text not in payload
        assert str(tmp_path) not in payload and "private_path" not in payload and "hostname" not in payload
    count = len(probe.calls)
    args.resume = True
    resumed = qualification.run(args)
    assert len(probe.calls) == count
    assert resumed["completed_steps"] == result["completed_steps"]
    assert len(resumed["sessions"]) == 2


@pytest.mark.parametrize("section", ["source", "assets", "protocol", "dependencies", "installed_dependencies_sha256"])
def test_resume_rejects_changed_binding_before_launching_a_child(tmp_path, monkeypatch, section):
    args = args_for(tmp_path, "--device", "cpu", "--smoke")
    probe, binding = install_fake_run(monkeypatch, args)
    qualification.run(args)
    count = len(probe.calls)
    args.resume = True
    changed = deepcopy(binding)
    changed[section] = "changed"
    monkeypatch.setattr(qualification, "_binding", lambda *_: changed)
    with pytest.raises(ValueError, match="resume_binding_mismatch"):
        qualification.run(args)
    assert len(probe.calls) == count


@pytest.mark.parametrize("artifact", ["report", "fixture"])
def test_resume_rejects_tampered_completed_evidence(tmp_path, monkeypatch, artifact):
    args = args_for(tmp_path, "--device", "cpu", "--smoke")
    probe, _ = install_fake_run(monkeypatch, args)
    qualification.run(args)
    count = len(probe.calls)
    path = Path(args.output) / "private" / ("runs/doctor.json" if artifact == "report" else "inputs.npz")
    path.write_bytes(b"tampered")
    args.resume = True
    with pytest.raises(ValueError, match="qualification_step_failed|resume_.*tampered"):
        qualification.run(args)
    assert len(probe.calls) == count


def test_failed_child_stops_then_resumes_without_accepting_its_report(tmp_path, monkeypatch):
    args = args_for(tmp_path, "--device", "cpu", "--smoke")
    probe, _ = install_fake_run(monkeypatch, args)
    probe.fail_kind = "bench"
    with pytest.raises(ValueError, match="qualification_step_failed"):
        qualification.run(args)
    assert probe.calls[-1][0] == "bench"
    partial = json.loads((Path(args.output) / "share/qualification.json").read_text())
    assert partial["status"] == "failed" and partial["selection"]["candidate"] is None
    assert partial["suggested_numeric_gates"]["candidates"] == {}
    assert partial["padding_assessment"]["by_dtype"] == {}
    failed_report = Path(args.output) / "private/runs/fixed_b0_torch_fp32.json"
    failed_report.write_text("old successful report is not reusable")
    probe.fail_kind = None
    before = len(probe.calls)
    args.resume = True
    assert qualification.run(args)["status"] == "recorded_nonqualifying"
    assert probe.calls[before][0] == "bench"  # Every earlier complete phase was reused.


def test_incomplete_metal_run_has_no_selection_or_padding_speed_conclusion(tmp_path, monkeypatch):
    args = args_for(tmp_path)
    probe, _ = install_fake_run(monkeypatch, args)
    probe.fail_kind = "audio"  # All fixed measurements have completed.
    with pytest.raises(ValueError, match="qualification_step_failed"):
        qualification.run(args)
    result = json.loads((Path(args.output) / "share/qualification.json").read_text())
    assert all(result["fixed_model"][variant]["runs"] == 20 for variant in qualification.VARIANTS)
    assert result["selection"]["candidate"] is None
    assert "best_fixed_mlx_variant" not in result["suggested_numeric_gates"]
    assert result["padding_assessment"]["recommendation"] == "not_assessed"


@pytest.mark.parametrize("broken", ["revision", "count", "fp32_top1"])
def test_rejects_variant_count_and_numeric_gate_failures(tmp_path, monkeypatch, broken):
    args = args_for(tmp_path, "--device", "cpu", "--smoke")
    probe, _ = install_fake_run(monkeypatch, args)
    def mutate(kind, report):
        if broken == "revision" and kind == "bench":
            report["execution"]["source_sha256"]["model"] = "2" * 64
        elif broken == "count" and kind == "bench":
            report["raw_fixed_runs"] = []
        elif broken == "fp32_top1" and kind == "validate" and report["inference_dtype"] == "float32":
            report["rows"][0]["top1_agrees"] = False
            report["top1_agreements"] -= 1
    probe.mutate_report = mutate
    with pytest.raises(ValueError, match="qualification_step_failed"):
        qualification.run(args)
    assert probe.calls[-1][0] == ("validate" if broken == "fp32_top1" else "bench")


def test_cpu_formal_and_under_counted_metal_protocols_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="cpu_requires_smoke"):
        qualification.run(args_for(tmp_path, "--device", "cpu"))
    with pytest.raises(ValueError, match="formal_protocol_requires"):
        qualification.run(args_for(tmp_path, "--fixed-runs", "9"))
    with pytest.raises(ValueError, match="forward_reverse_blocks_must_be_paired"):
        qualification.run(args_for(tmp_path, "--fixed-blocks", "3"))


def test_optional_listening_export_is_blind_and_never_includes_reference_assets(tmp_path, monkeypatch):
    args = args_for(tmp_path, "--device", "cpu", "--smoke", "--include-listening-wavs")
    install_fake_run(monkeypatch, args)
    result = qualification.run(args)
    assert result["blind_kit"]["status"] == "human_listening_pending" and result["blind_kit"]["pairs"] == 5
    with zipfile.ZipFile(Path(args.output) / "share.zip") as bundle:
        wavs = [name for name in bundle.namelist() if name.endswith(".wav")]
        assert len(wavs) == 10 and all(name.startswith("listening/pair_") for name in wavs)
        assert not any("reference" in name or "answer-key" in name or "private/" in name for name in bundle.namelist())
        assert args.reference_text not in bundle.read("listening/review.csv").decode("utf-8-sig")
    assert (Path(args.output) / "private/listening-answer-key.json").is_file()


def test_binding_includes_base_acoustic_asset_frontend_trees_and_full_dependency_digest(tmp_path, monkeypatch):
    args = args_for(tmp_path, "--device", "cpu", "--smoke", "--assets-root", str(tmp_path / "installation"))
    for path in (args.checkpoint, args.sovits, args.reference_audio):
        Path(path).write_text("private asset content")
    pretrained = Path(args.assets_root) / "assets/models/gpt-sovits/pretrained"
    pretrained.mkdir(parents=True)
    base = pretrained / "s2Gv3.pth"
    base.write_text("base acoustic dependency")
    for directory in qualification.FRONTEND_DIRS:
        folder = pretrained / directory
        folder.mkdir()
        (folder / "model.bin").write_text(directory)
    monkeypatch.setattr(qualification, "ROOT", tmp_path)
    monkeypatch.setattr(qualification, "_identity", lambda: {"candidate_sha": "a" * 40})
    monkeypatch.setattr(qualification, "_inference_source_sha", lambda: HASH)
    monkeypatch.setattr(qualification, "_variant_sources", lambda: {})
    monkeypatch.setattr(qualification, "_versions", lambda: {})
    installed = {"torch": "1.0", "indirect-frontend-package": "1.0"}
    monkeypatch.setattr(qualification, "_installed_versions", lambda: installed)
    first = qualification._binding(args, profiles(), environment())
    assert first["assets"]["pretrained_sovits_sha256"] == qualification._sha(base)
    base.write_text("changed base acoustic dependency")
    second = qualification._binding(args, profiles(), environment())
    assert first["assets"] != second["assets"]
    frontend = pretrained / qualification.FRONTEND_DIRS[0] / "model.bin"
    frontend.write_text("changed frontend weights")
    third = qualification._binding(args, profiles(), environment())
    assert second["frontend_assets"] != third["frontend_assets"]
    installed["indirect-frontend-package"] = "2.0"
    final = qualification._binding(args, profiles(), environment())
    assert third["installed_dependencies_sha256"] != final["installed_dependencies_sha256"]
    assert "indirect-frontend-package" not in json.dumps(final) and str(tmp_path) not in json.dumps(final)


@pytest.mark.parametrize("interruption", ["timeout", "interrupt"])
def test_timeout_and_interrupt_stop_only_the_owned_child_tree_then_drain_logs(monkeypatch, interruption):
    calls, creation = [], {}
    failure = (qualification.subprocess.TimeoutExpired(["python", "probe"], 1)
               if interruption == "timeout" else KeyboardInterrupt())
    class Child:
        pid, returncode, communicate_count = 4321, -9, 0

        def communicate(self, *, timeout):
            self.communicate_count += 1
            if self.communicate_count == 1:
                raise failure
            return SECRET, "private child error"

        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            return self.returncode

        def poll(self):
            return self.returncode

    child = Child()
    def popen(command, **kwargs):
        creation.update(kwargs)
        return child
    def taskkill(command, **kwargs):
        calls.append(("taskkill", command))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(qualification.subprocess, "Popen", popen)
    monkeypatch.setattr(qualification.subprocess, "run", taskkill)
    monkeypatch.setattr(qualification.os, "killpg", lambda pid, sig: calls.append(("killpg", pid, sig)), raising=False)
    with pytest.raises(type(failure)) as observed:
        qualification._run_child(["python", "probe"], timeout=1, env={})
    if qualification.os.name == "nt":
        assert calls[0] == ("taskkill", ["taskkill", "/PID", "4321", "/T", "/F"])
    else:
        assert calls[0] == ("killpg", 4321, qualification.signal.SIGKILL)
    assert calls[-1] == ("wait", 10)
    assert child.communicate_count == 2 and observed.value.stdout == SECRET
    assert creation["start_new_session"] is (qualification.os.name != "nt")
