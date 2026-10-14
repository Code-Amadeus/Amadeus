"""Extension contracts using synthetic local packages; no network/credentials."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
from server.extensions.package import inspect_wheel, validate_config, validate_manifest
from server.extensions.runner import ExtensionUnavailable, Pending, RunnerClient
from server.extensions.runtime import ExtensionRuntime
from server.extensions.snapshot import parse_snapshot, pop_snapshot_environment
from server.extensions.tts import ExtensionTTSBackend
from tts.backend import TTSBackendError, TTSSynthesisRequest, TTSRuntimeAdapter


def manifest():
    return {
        "schema": "amadeus.extension/v0", "id": "com.example.synthetic", "version": "0.1.0",
        "display_name": "Synthetic voice", "description": "Local extension test fixture", "author": {"name": "Test"},
        "engines": {"amadeus_host_api": ">=0.1,<0.2"},
        "runtime": {"kind": "python", "requires_python": ">=3.11", "entry": "synthetic_voice:extension"},
        "contributes": {"voice_backend": [{"id": "tone", "role": "tts", "contract": "amadeus.voice.tts/v0", "deployment": "local", "languages": ["en"], "streaming": True}]},
        "config": {"api_key": {"type": "string", "title": "Key", "secret": True, "required": True},
                   "delay": {"type": "number", "title": "Delay", "min": 0, "max": 2, "default": 0}},
    }


def wait_until(function, timeout=5):
    expires = time.monotonic() + timeout
    while time.monotonic() < expires:
        if function():
            return
        time.sleep(0.01)
    raise AssertionError("local runtime did not reach expected state")


@pytest.fixture(scope="module")
def private_environment(tmp_path_factory):
    directory = tmp_path_factory.mktemp("extension-env")
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(directory)], check=True, capture_output=True)
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    info = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], check=True, capture_output=True, text=True)
    site = Path(info.stdout.strip())
    from server.extensions.sdk import sdk_spec
    with zipfile.ZipFile(ROOT / "vendor/extension-sdk" / sdk_spec()["filename"]) as sdk:
        sdk.extractall(site)
    fixture = site / "synthetic_voice"
    fixture.mkdir()
    fixture.joinpath("__init__.py").write_text('''
import json, os, subprocess, sys, time
from pathlib import Path
from amadeus_extension import Extension, AudioChunk
extension = Extension()
data_directory = None
@extension.initialize
def initialize(context):
    global data_directory
    data_directory = Path(context.paths["data"])
    if context.config.get("mode") == "initialization_failure":
        raise RuntimeError("synthetic initialization failure")
    time.sleep(context.config.get("delay", 0))
    if context.config.get("mode") == "mismatch":
        extension._tts["wrong"] = extension._tts.pop("tone")
    if context.config.get("mode") == "never_ready":
        time.sleep(120)
    print("package stdout is redirected 日本語")
    Path(context.paths["data"], "init.json").write_text(json.dumps({
        "env": dict(os.environ), "secrets": dict(context.secrets)}))
@extension.shutdown
def shutdown():
    if data_directory:
        with (data_directory / "shutdown.txt").open("a") as log:
            log.write("closed\\n")
@extension.tts("tone")
def speak(request, context):
    data = Path(context.paths["data"])
    with (data / "requests.txt").open("a") as log:
        log.write(request.text + "\\n")
    if request.text == "crash":
        os._exit(7)
    if request.text == "slow_before_chunk":
        (data / "blocking").write_text("started")
        time.sleep(3)
    if request.text == "ignore_cancel":
        yield AudioChunk(24000, b"\\x00\\x40" * 100)
        time.sleep(120)
    if request.text == "spawn":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        (data / "child.pid").write_text(str(child.pid))
    if request.text == "deadline":
        context.on_cancel(lambda: (data / "cancelled").write_text("closed"))
        while not context.cancelled():
            time.sleep(.01)
        context.check_cancelled()
    yield AudioChunk(24000, b"\\x00\\x40" * 100)
    if request.text == "burst":
        for index in range(128):
            yield AudioChunk(24000, b"\\x00\\xc0" * 100)
        return
    if request.text == "cancel":
        context.on_cancel(lambda: (data / "cancelled").write_text("closed"))
        while not context.cancelled():
            time.sleep(.01)
        context.check_cancelled()
    else:
        yield AudioChunk(24000, b"\\x00\\xc0" * 100)
''', encoding="utf-8")
    return python, fixture


@pytest.fixture
def record(private_environment, tmp_path):
    python, package = private_environment
    definition = manifest()
    package.joinpath("amadeus-extension.json").write_text(json.dumps(definition), encoding="utf-8")
    return {"id": definition["id"], "version": definition["version"], "digest": "a" * 64,
            "manifest": definition, "python_path": str(python), "package_path": str(package), "data_path": str(tmp_path / "data"),
            "enabled": True, "config": {}, "secrets": {"api_key": "synthetic-package-secret"}, "providers": []}


@pytest.fixture
def fast_limits(monkeypatch):
    import server.extensions.runner as client
    monkeypatch.setattr(client, "RESTART_BACKOFF", (.05, .05, .05))
    monkeypatch.setattr(client, "READY_TIMEOUT", 2)
    monkeypatch.setattr(client, "REQUEST_TIMEOUT", 2)
    monkeypatch.setattr(client, "SHUTDOWN_TIMEOUT", 1)


def test_sdk_failed_initialization_runs_shutdown_once(record):
    client = RunnerClient(record)
    params = client._initialize()
    from importlib.metadata import version
    assert params["host"]["version"] == version("amadeus")
    params["config"] = {"mode": "initialization_failure"}
    reply = subprocess.run([record["python_path"], "-I", "-m", "amadeus_extension.runner",
        "--entry", record["manifest"]["runtime"]["entry"]],
        input=json.dumps({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": params}) + "\n",
        capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert reply.returncode == 0, reply.stderr
    messages = [json.loads(line) for line in reply.stdout.splitlines()]
    assert len(messages) == 1 and messages[0]["error"]["code"] == -32010
    assert Path(record["data_path"], "shutdown.txt").read_text() == "closed\n"


def test_manifest_subset_and_semantics():
    good = manifest()
    assert validate_manifest(good) is good
    bad = copy.deepcopy(good)
    bad["permissions"] = {"host": [{"capability": "conversation.current:read", "reason": "test"}]}
    with pytest.raises(ValueError, match="permissions.host"):
        validate_manifest(bad)

    bad = copy.deepcopy(good)
    bad["contributes"]["voice_backend"][0]["role"] = "asr"
    with pytest.raises(ValueError):
        validate_manifest(bad)
    bad = copy.deepcopy(good)
    bad["contributes"]["voice_backend"].append(copy.deepcopy(bad["contributes"]["voice_backend"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        validate_manifest(bad)
    bad = copy.deepcopy(good)
    bad["engines"]["amadeus_host_api"] = ">=0.2"
    with pytest.raises(ValueError, match="incompatible"):
        validate_manifest(bad)


def test_runtime_reports_only_validated_startup_revision(record):
    record = {**record, 'revision': 7, 'secrets': {}}
    runtime = ExtensionRuntime([record])
    assert runtime.statuses()[0]['revision'] == 7
    assert runtime.clients == {}
    assert runtime.statuses()[0]['startup'] == 'rejected'  # Configuration is required for admission.
    invalid = {**record, 'revision': -1}
    assert 'revision' not in ExtensionRuntime([invalid]).statuses()[0]
    blocked = {**record, 'startup_error': 'synthetic blocked installation'}
    assert ExtensionRuntime([blocked]).statuses()[0]['startup'] == 'rejected'
    assert ExtensionRuntime([blocked]).statuses()[0]['revision'] == 7


def test_config_types_defaults_required_and_secret_errors():
    resolved, missing = validate_config(manifest(), {}, allow_incomplete=True)
    assert resolved == {"delay": 0} and missing == ["api_key"]
    for values in ({"api_key": "secret", "delay": True}, {"api_key": "secret", "delay": 3}, {"api_key": "secret", "other": "secret"}):
        with pytest.raises(ValueError) as error:
            validate_config(manifest(), values)
        assert "secret" not in str(error.value)
    with pytest.raises(ValueError, match="missing"):
        validate_config(manifest(), {})


def test_wheel_static_inspection_and_unsafe_archive(tmp_path):
    wheel = tmp_path / "synthetic-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("synthetic_voice/amadeus-extension.json", json.dumps(manifest()))
        archive.writestr("synthetic_voice/__init__.py", "raise AssertionError('must not execute')")
        archive.writestr("synthetic-0.1.0.dist-info/METADATA", "Name: synthetic\nVersion: 0.1.0\nRequires-Dist: dependency>=1\n")
        archive.writestr("synthetic-0.1.0.dist-info/entry_points.txt", "[amadeus.extensions]\ncom.example.synthetic = synthetic_voice\n")
    result = inspect_wheel(wheel)
    assert result["package_root"] == "synthetic_voice" and result["distribution"] == "synthetic"
    assert len(result["digest"]) == 64
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../outside.py", "")
    with pytest.raises(ValueError, match="unsafe"):
        inspect_wheel(wheel)


@pytest.mark.parametrize("unsafe", ["synthetic_voice/./other.py", "synthetic_voice//other.py", "synthetic_voice/trailing.", "startup.pth", "SYNTHETIC_VOICE/__INIT__.PY"])
def test_wheel_rejects_ambiguous_or_executable_startup_members(tmp_path, unsafe):
    wheel = tmp_path / "synthetic-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("synthetic_voice/amadeus-extension.json", json.dumps(manifest()))
        archive.writestr("synthetic_voice/__init__.py", "")
        archive.writestr("synthetic-0.1.0.dist-info/METADATA", "Name: synthetic\nVersion: 0.1.0\n")
        archive.writestr("synthetic-0.1.0.dist-info/entry_points.txt", "[amadeus.extensions]\ncom.example.synthetic = synthetic_voice\n")
        archive.writestr(unsafe, "")
    with pytest.raises(ValueError):
        inspect_wheel(wheel)


@pytest.mark.parametrize("entry_name,entry_root,runtime_entry", [
    ("com.example.wrong", "synthetic_voice", "synthetic_voice:extension"),
    ("com.example.synthetic", "foreign_package", "synthetic_voice:extension"),
    ("com.example.synthetic", "synthetic_voice", "foreign_package:extension"),
])
def test_wheel_binds_manifest_identity_and_runtime_to_artifact_root(tmp_path, entry_name, entry_root, runtime_entry):
    wheel = tmp_path / "synthetic-0.1.0-py3-none-any.whl"
    definition = manifest()
    definition["runtime"]["entry"] = runtime_entry
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("synthetic_voice/amadeus-extension.json", json.dumps(definition))
        archive.writestr("synthetic-0.1.0.dist-info/METADATA", "Name: synthetic\nVersion: 0.1.0\n")
        archive.writestr("synthetic-0.1.0.dist-info/entry_points.txt", f"[amadeus.extensions]\n{entry_name} = {entry_root}\n")
    with pytest.raises(ValueError, match="entry"):
        inspect_wheel(wheel)


def test_configuration_contract_for_url_enum_boolean_and_integer():
    definition = manifest()
    definition["config"].update({
        "endpoint": {"type": "url", "title": "Endpoint", "required": True},
        "enabled": {"type": "boolean", "title": "Enabled", "required": True},
        "count": {"type": "integer", "title": "Count", "min": 1, "max": 4},
        "voice": {"type": "enum", "title": "Voice", "options": ["a", {"value": "b"}], "default": "a"},
    })
    values = {"api_key": "secret", "endpoint": "http://localhost:1234/v1", "enabled": False, "count": 4, "voice": "b"}
    assert validate_config(definition, values)[0]["enabled"] is False
    for key, invalid in (("endpoint", "https://user:secret@example.test"), ("endpoint", "file:///tmp/model"), ("count", True), ("voice", "missing")):
        with pytest.raises(ValueError) as error:
            validate_config(definition, {**values, key: invalid})
        assert "secret" not in str(error.value)


def test_cli_redacts_secret_values_and_invalid_json():
    body = {"manifest": manifest(), "config": {"api_key": "private-secret", "delay": 0}}
    result = subprocess.run([sys.executable, "-I", str(ROOT / "tools/extension_package.py"), "validate-config"], input=json.dumps(body), capture_output=True, text=True)
    assert result.returncode == 0 and "private-secret" not in result.stdout
    assert json.loads(result.stdout)["config"] == {"delay": 0}
    invalid = subprocess.run([sys.executable, "-I", str(ROOT / "tools/extension_package.py"), "validate-config"], input='{"private-secret":', capture_output=True, text=True)
    assert invalid.returncode == 1 and "private-secret" not in invalid.stdout + invalid.stderr


def test_snapshot_removed_before_child_environment(monkeypatch):
    monkeypatch.setenv("AMADEUS_EXTENSIONS", '[{"secret":"private"}]')
    assert parse_snapshot(pop_snapshot_environment()) == [{"secret": "private"}]
    assert "AMADEUS_EXTENSIONS" not in os.environ
    with pytest.raises(ValueError):
        parse_snapshot("{}")


def test_runtime_needs_config_disabled_and_unavailable(record):
    missing = copy.deepcopy(record)
    missing["secrets"] = {}
    runtime = ExtensionRuntime([missing])
    assert runtime.statuses()[0]["state"] == "needs_config" and runtime.activation_records == []
    disabled = copy.deepcopy(record)
    disabled["enabled"] = False
    assert ExtensionRuntime([disabled]).statuses()[0]["state"] == "inactive"
    unavailable = copy.deepcopy(record)
    unavailable["python_path"] = str(Path(record["python_path"]).parent / "missing-python.exe")
    runtime = ExtensionRuntime([unavailable])
    assert runtime.statuses()[0]["state"] == "error" and runtime.statuses()[0]["enabled"]
    assert runtime.activation_records == [] and len(runtime.statuses()[0]["contributions"]) == 1
    duplicate = ExtensionRuntime([record, record])
    assert duplicate.activation_records == [] and duplicate.clients == {}


def test_mcp_health_reads_registered_native_projection_and_preserves_targets(record):
    record["manifest"]["runtime"] = {"kind": "none"}
    record["manifest"]["contributes"] = {"mcp_server": [{"id": "notes", "contract": "amadeus.mcp-server/v0", "transport": "http", "url": "https://example.test/mcp"}]}
    record["manifest"]["config"] = {}
    record["secrets"] = {}
    Path(record["package_path"], "amadeus-extension.json").write_text(json.dumps(record["manifest"]), encoding="utf-8")
    definitions = {
        "good": SimpleNamespace(capabilities=SimpleNamespace(capability_projections=("mcp_connection",))),
        "incompatible": SimpleNamespace(capabilities=SimpleNamespace(capability_projections=("agent_skill",))),
    }
    record["providers"] = ["incompatible", "missing"]
    runtime = ExtensionRuntime([record], provider_manifest_lookup=definitions.get)
    status = runtime.statuses()[0]
    assert status["state"] == "error" and status["contributions"][0]["health"] == "error"
    assert status["contributions"][0]["provider_targets"] == [{"id": "incompatible", "state": "incompatible"}, {"id": "missing", "state": "unavailable"}]
    assert runtime.activation_records[0]["providers"] == record["providers"]
    record["providers"] = ["good", "incompatible"]
    runtime = ExtensionRuntime([record], provider_manifest_lookup=definitions.get)
    assert runtime.statuses()[0]["contributions"][0]["health"] == "degraded"
    record["providers"] = ["good"]
    runtime = ExtensionRuntime([record], provider_manifest_lookup=definitions.get)
    assert runtime.statuses()[0]["contributions"][0]["health"] == "ready"


def test_runtime_nonblocking_registration_and_stable_proxy(record, fast_limits):
    record["config"] = {"delay": .4}
    runtime = ExtensionRuntime([record])
    try:
        started = time.monotonic()
        runtime.start()
        from tts.registry import create_tts_runtime, tts_backend_statuses
        adapter = create_tts_runtime("com.example.synthetic/tone")
        assert time.monotonic() - started < .3
        assert any(item["id"] == adapter.backend_id and item["state"] == "starting" for item in tts_backend_statuses())
        before = adapter.backend
        wait_until(lambda: runtime.statuses()[0]["state"] == "active")
        result = before.synthesize(TTSSynthesisRequest("normal", language="en"))
        assert result.sample_rate == 24000 and result.audio.dtype == np.float32
        assert result.audio.size == 200 and np.all(result.audio[:100] == .5)
        assert adapter.backend is before
    finally:
        runtime.close()


def test_process_gets_only_own_secrets_and_no_host_environment(record, fast_limits, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-host-secret")
    monkeypatch.setenv("AMADEUS_EXTENSIONS", "all-package-secrets")
    client = RunnerClient(record)
    try:
        client.start()
        wait_until(lambda: client.state == "active")
        captured = json.loads(Path(record["data_path"], "init.json").read_text())
        assert captured["secrets"] == {"api_key": "synthetic-package-secret"}
        assert "OPENAI_API_KEY" not in captured["env"] and "AMADEUS_EXTENSIONS" not in captured["env"]
        assert "synthetic-package-secret" not in json.dumps(captured["env"])
    finally:
        client.close()


def test_generator_close_cancels_resources_and_discards_late_audio(record, fast_limits):
    client = RunnerClient(record)
    try:
        client.start()
        wait_until(lambda: client.state == "active")
        stream = client.stream("tone", text="cancel", language="en", speed=1)
        assert next(stream)[0] == 24000
        stream.close()
        wait_until(lambda: Path(record["data_path"], "cancelled").exists())
        wait_until(lambda: not client._cancelled)
        assert client._pending == {} and client.state == "active"
        assert len(list(client.stream("tone", text="normal", language="en", speed=1))) == 2
    finally:
        client.close()


def test_crash_recovers_only_for_new_utterance_without_replay(record, fast_limits):
    client = RunnerClient(record)
    try:
        client.start()
        wait_until(lambda: client.state == "active")
        proxy = ExtensionTTSBackend(record["id"], record["manifest"]["contributes"]["voice_backend"][0], client)
        with pytest.raises(TTSBackendError):
            proxy.synthesize(TTSSynthesisRequest("crash", language="en"))
        wait_until(lambda: client._token >= 2 and client.state == "active")
        assert proxy.synthesize(TTSSynthesisRequest("normal", language="en")).audio.size == 200
        lines = Path(record["data_path"], "requests.txt").read_text().splitlines()
        assert lines == ["crash", "normal"]
    finally:
        client.close()


def test_deadline_cancels_without_synthesis_retry(record, fast_limits):
    client = RunnerClient(record)
    try:
        client.start()
        wait_until(lambda: client.state == "active")
        with pytest.raises(ExtensionUnavailable, match="deadline|failed"):
            list(client.stream("tone", text="deadline", language="en", speed=1, deadline_ms=100))
        wait_until(lambda: not client._cancelled)
        assert Path(record["data_path"], "requests.txt").read_text().splitlines() == ["deadline"]
    finally:
        client.close()


def test_wire_rejects_invalid_audio_and_bounds_queue(record):
    client = RunnerClient(record)
    for params in ({"seq": 1, "sample_rate": 24000, "pcm_s16le": "AAA="},
                   {"seq": 0, "sample_rate": 1000, "pcm_s16le": "AAA="},
                   {"seq": 0, "sample_rate": 24000, "pcm_s16le": "AA=="}):
        pending = Pending()
        client._pending = {"request": pending}
        client._message({"method": "tts.chunk", "params": {"request_id": "request", **params}})
        assert pending.error and pending.must_cancel
    pending = Pending()
    client._pending = {"request": pending}
    for seq in range(8):
        client._message({"method": "tts.chunk", "params": {"request_id": "request", "seq": seq, "sample_rate": 24000, "pcm_s16le": "AAA="}})
    producer = threading.Thread(target=client._message, args=({"method": "tts.chunk", "params": {"request_id": "request", "seq": 8, "sample_rate": 24000, "pcm_s16le": "AAA="}},))
    producer.start()
    wait_until(lambda: pending.seq == 9)
    assert pending.inbox.qsize() == 8 and not pending.error and producer.is_alive()
    pending.finished.set()
    producer.join(timeout=.3)
    assert not producer.is_alive()
    client._pending.clear()
    client._message({"method": "tts.chunk", "params": {"request_id": "request", "seq": 9}})


def test_fast_valid_burst_backpressure_and_first_chunk_text(record, fast_limits):
    client = RunnerClient(record)
    try:
        client.start()
        wait_until(lambda: client.state == "active")
        proxy = ExtensionTTSBackend(record["id"], record["manifest"]["contributes"]["voice_backend"][0], client)
        chunks = list(proxy.synthesize_stream(TTSSynthesisRequest("burst", language="en")))
        assert len(chunks) == 129 and sum(chunk.audio.size for chunk in chunks) == 12900
        assert chunks[0].text == "burst" and all(not chunk.text for chunk in chunks[1:])
    finally:
        client.close()


def test_language_rejects_before_remote_request(record):
    client = RunnerClient(record)
    proxy = ExtensionTTSBackend(record["id"], record["manifest"]["contributes"]["voice_backend"][0], client)
    with pytest.raises(TTSBackendError, match="language"):
        proxy.synthesize(TTSSynthesisRequest("text", language="ja"))


def test_regional_language_matches_ui_and_native_pacing_is_not_sent(record):
    calls = []
    def stream(contribution, **kwargs):
        calls.append(kwargs)
        yield 24000, b'\x00\x00' * 16
    contribution = {**record['manifest']['contributes']['voice_backend'][0], 'languages': ['ja-JP', 'en-US']}
    proxy = ExtensionTTSBackend(record['id'], contribution, SimpleNamespace(stream=stream))
    for language, speed in [('ja', 1.067), ('en', .97)]:
        assert proxy.synthesize(TTSSynthesisRequest('text', language=language, speed=speed)).audio.size == 16
    assert [call['speed'] for call in calls] == [1.0, 1.0]
    assert [call['language'] for call in calls] == ['ja', 'en']


def test_slow_cancellation_keeps_runner_and_does_not_consume_crash_budget(record):
    client = RunnerClient(record)
    try:
        client.start()
        wait_until(lambda: client.state == 'active')
        process = client._process
        for _ in range(4):
            marker = Path(record['data_path'], 'blocking')
            marker.unlink(missing_ok=True)
            failures = []
            def consume():
                try:
                    list(client.stream('tone', text='slow_before_chunk', language='en', speed=1))
                except ExtensionUnavailable as error:
                    failures.append(str(error))
            task = threading.Thread(target=consume)
            task.start()
            wait_until(marker.exists)
            client.cancel_inflight()
            task.join(timeout=1)
            assert not task.is_alive() and failures
            wait_until(lambda: not client._cancelled, timeout=5)
            assert client._process is process and client.state == 'active'
        assert client._restarts == []
        assert len(list(client.stream('tone', text='normal', language='en', speed=1))) == 2
    finally:
        client.close()
    assert client._pending == {}


def test_uncooperative_cancellation_recovers_capacity_without_replaying_speech(record, monkeypatch):
    import server.extensions.runner as runner
    monkeypatch.setattr(runner, 'CANCEL_GRACE_TIMEOUT', 1.0)
    monkeypatch.setattr(runner, 'RESTART_BACKOFF', (.05,))
    client = RunnerClient(record)
    try:
        client.start()
        wait_until(lambda: client.state == 'active')
        old = client._process
        for _ in range(2):
            stream = client.stream('tone', text='ignore_cancel', language='en', speed=1)
            next(stream)
            stream.close()
        wait_until(lambda: client._process is not old and client.state == 'active', timeout=8)
        assert old.poll() is not None
        assert client._cancelled == {}
        assert len(client._restarts) == 1
        assert len(list(client.stream('tone', text='normal', language='en', speed=1))) == 2
        assert Path(record['data_path'], 'requests.txt').read_text().splitlines() == [
            'ignore_cancel', 'ignore_cancel', 'normal']
        # A pathological package cannot turn repeated hangs into an unbounded
        # process-spawn loop. Cancellation recovery uses the existing budget.
        stream = client.stream('tone', text='ignore_cancel', language='en', speed=1)
        next(stream)
        stream.close()
        wait_until(lambda: 'restart budget exhausted' in client.detail, timeout=8)
        assert client.state == 'error'
        with pytest.raises(ExtensionUnavailable):
            list(client.stream('tone', text='must not dispatch', language='en', speed=1))
        assert 'must not dispatch' not in Path(record['data_path'], 'requests.txt').read_text()
    finally:
        client.close()


def test_closing_old_stream_after_recovery_does_not_cancel_in_new_runner(record, monkeypatch):
    import server.extensions.runner as runner
    monkeypatch.setattr(runner, 'CANCEL_GRACE_TIMEOUT', .5)
    monkeypatch.setattr(runner, 'RESTART_BACKOFF', (.05, .05, .05))
    client = RunnerClient(record)
    stale = None
    try:
        client.start()
        wait_until(lambda: client.state == 'active')
        old = client._process
        abandoned = client.stream('tone', text='ignore_cancel', language='en', speed=1)
        next(abandoned)
        stale = client.stream('tone', text='cancel', language='en', speed=1)
        next(stale)
        abandoned.close()
        wait_until(lambda: client._process is not old and client.state == 'active', timeout=8)
        stale.close()
        assert client._cancelled == {}, 'A disposed process has no request left to cancel'
        assert len(list(client.stream('tone', text='normal', language='en', speed=1))) == 2
    finally:
        if stale is not None:
            stale.close()
        client.close()


def test_catalog_retains_unavailable_voice_without_secrets(record):
    runtime = ExtensionRuntime([record])
    packages = runtime.capability_packages()
    projection = packages[0].to_dict(include_disabled=True)
    assert projection["contributions"][0]["available"] is False
    assert projection["contributions"][0]["metadata"]["observed_state"] == "starting"
    assert "synthetic-package-secret" not in json.dumps(projection)


def test_missing_selected_extension_has_unavailable_status_without_fallback():
    from tts.registry import create_tts_runtime, tts_backend_statuses
    selected = "com.example.removed/tone"
    projected = next(item for item in tts_backend_statuses(selected) if item["id"] == selected)
    assert projected["selected"] and not projected["available"] and projected["state"] == "unavailable"
    with pytest.raises(ValueError, match="unknown TTS"):
        create_tts_runtime(selected)


def test_windows_job_stops_runner_descendants(record, fast_limits):
    if os.name != "nt":
        pytest.skip("Windows process tree containment")
    import psutil
    client = RunnerClient(record)
    client.start()
    try:
        wait_until(lambda: client.state == "active")
        list(client.stream("tone", text="spawn", language="en", speed=1))
        child_pid = int(Path(record["data_path"], "child.pid").read_text())
        assert psutil.pid_exists(child_pid)
        runner_pid = client._process.pid
    finally:
        client.close()
    wait_until(lambda: not psutil.pid_exists(child_pid))
    assert not psutil.pid_exists(runner_pid)


def test_registration_mismatch_and_ready_timeout_have_bounded_recovery(record, fast_limits, monkeypatch):
    import server.extensions.runner as module
    monkeypatch.setattr(module, "READY_TIMEOUT", 1)
    monkeypatch.setattr(module, "RESTART_BACKOFF", (.01,))
    for mode in ("mismatch", "never_ready"):
        changed = copy.deepcopy(record)
        changed["manifest"]["config"]["mode"] = {"type": "string", "title": "Mode"}
        changed["config"] = {"mode": mode}
        client = RunnerClient(changed)
        try:
            client.start()
            if mode == "mismatch":
                wait_until(lambda: client.state == "error" and client._process is None)
                assert client._token == 1 and client._restarts == []
                assert "mismatch" in client.detail
            else:
                wait_until(lambda: "restart budget exhausted" in client.detail)
                assert client.state == "error" and len(client._restarts) == 1 and "timeout" in client.detail
            wait_until(lambda: client._process is None)
        finally:
            client.close()


def test_two_request_limit_rejects_extra_work_without_dispatch(record, fast_limits):
    client = RunnerClient(record)
    first = second = None
    try:
        client.start()
        wait_until(lambda: client.state == "active")
        first = client.stream("tone", text="cancel", language="en", speed=1)
        second = client.stream("tone", text="cancel", language="en", speed=1)
        next(first)
        next(second)
        with pytest.raises(ExtensionUnavailable, match="concurrency"):
            list(client.stream("tone", text="unadmitted", language="en", speed=1))
        assert "unadmitted" not in Path(record["data_path"], "requests.txt").read_text()
    finally:
        if first:
            first.close()
        if second:
            second.close()
        client.close()


def test_close_during_startup_cannot_leave_runner(record, fast_limits):
    record["config"] = {"delay": 1}
    client = RunnerClient(record)
    client.start()
    client.close()
    assert client._process is None and not client._thread.is_alive()


def test_native_interrupt_cancels_first_chunk_wait_promptly(record, fast_limits):
    client = RunnerClient(record)
    errors = []
    def synthesize():
        try:
            list(client.stream("tone", text="deadline", language="en", speed=1, deadline_ms=60000))
        except ExtensionUnavailable as error:
            errors.append(str(error))
    try:
        client.start()
        wait_until(lambda: client.state == "active")
        worker = threading.Thread(target=synthesize)
        worker.start()
        wait_until(lambda: Path(record["data_path"], "requests.txt").exists())
        from server.handlers.tts_handler import TtsHandler
        stopped = []
        async def stop_playback():
            stopped.append(True)
        proxy = ExtensionTTSBackend(record["id"], record["manifest"]["contributes"]["voice_backend"][0], client)
        adapter = TTSRuntimeAdapter(proxy)
        async def interrupt():
            handler = TtsHandler()
            handler.configure(SimpleNamespace(interrupt=stop_playback), None, cancel_synthesis=adapter.cancel_pending)
            with patch("tts.pipeline.interrupt_pending_tts", return_value=2):
                await handler.handle("tts.interrupt", {"annotate_history": False})
                await handler.handle("tts.interrupt", {"annotate_history": False})
        asyncio.run(interrupt())
        worker.join(timeout=.5)
        assert not worker.is_alive() and errors
        wait_until(lambda: not client._cancelled)
        assert stopped == [True, True] and Path(record["data_path"], "cancelled").exists()
        assert client._pending == {}
        assert len(list(client.stream("tone", text="normal", language="en", speed=1))) == 2
    finally:
        client.close()


def test_windows_host_exit_kills_runner_and_descendants(record, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows abrupt Host exit containment")
    import psutil
    driver = tmp_path / "driver.py"
    record_file = tmp_path / "record.json"
    record_file.write_text(json.dumps(record), encoding="utf-8")
    driver.write_text('''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from server.extensions.runner import RunnerClient
record = json.loads(Path(sys.argv[2]).read_text())
client = RunnerClient(record)
client.start()
deadline = time.monotonic() + 10
while client.state != 'active':
    if time.monotonic() > deadline:
        raise RuntimeError('local runner startup failed')
    time.sleep(.01)
list(client.stream('tone', text='spawn', language='en', speed=1))
Path(record['data_path'], 'runner.pid').write_text(str(client._process.pid))
time.sleep(120)
''', encoding="utf-8")
    host = subprocess.Popen([sys.executable, "-I", str(driver), str(ROOT), str(record_file)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    runner_pid = child_pid = None
    try:
        wait_until(lambda: Path(record["data_path"], "runner.pid").exists(), timeout=10)
        runner_pid = int(Path(record["data_path"], "runner.pid").read_text())
        child_pid = int(Path(record["data_path"], "child.pid").read_text())
        host.kill()
        host.wait(timeout=5)
        wait_until(lambda: not psutil.pid_exists(runner_pid) and not psutil.pid_exists(child_pid))
    finally:
        if host.poll() is None:
            host.kill()
            host.wait(timeout=5)
        if host.stdout:
            host.stdout.close()
        if host.stderr:
            host.stderr.close()

@pytest.mark.parametrize('failure', ['installation', 'config', 'blocked'])
def test_backend_rejection_keeps_received_revision_without_activation(record, failure):
    record = {**record, 'revision': 7}
    if failure == 'installation':
        record['package_path'] = str(Path(record['package_path']) / 'missing')
    elif failure == 'config':
        record['config'] = {'delay': 'invalid'}
    else:
        record['startup_error'] = 'blocked by desktop'
    runtime = ExtensionRuntime([record])
    status = runtime.statuses()[0]
    assert status['revision'] == 7
    assert status['startup'] == 'rejected'
    assert runtime.activation_records == []
    assert runtime.clients == {}


def test_duplicate_identity_cannot_acknowledge_any_revision(record):
    runtime = ExtensionRuntime([{**record, 'revision': 7}, {**record, 'revision': 8}])
    assert all('revision' not in status and 'startup' not in status for status in runtime.statuses())


def test_runtime_health_errors_do_not_rewrite_successful_startup_receipt(record):
    runtime = ExtensionRuntime([{**record, 'revision': 7}])
    status = runtime.statuses()[0]
    assert status['startup'] == 'accepted'
    client = next(iter(runtime.clients.values()))
    client.state = 'error'
    assert runtime.statuses()[0]['startup'] == 'accepted'
    assert runtime.statuses()[0]['state'] == 'error'
