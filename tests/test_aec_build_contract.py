"""The vendored build must not write back or mix SWIG generations."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import runpy
import shutil
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest
import setuptools

from tools.verify_aec_source_build import assert_source_unchanged, check_wheel, source_snapshot


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "aec-audio-processing"


def load_backend():
    spec = importlib.util.spec_from_file_location("aec_build_backend", VENDOR / "build_backend.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_setup(monkeypatch):
    configuration = {}
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: configuration.update(kwargs))
    namespace = runpy.run_path(str(VENDOR / "setup.py"))
    return namespace, configuration


def test_old_installation_mismatch_fails_before_any_native_build(tmp_path, monkeypatch):
    # Reproduce #247's upgrade state: old builds regenerated only the wrapper
    # and named proxy, leaving the public audio_processing.py at SWIG 4.3.1.
    source = tmp_path / "src"
    source.mkdir()
    for name in ("audio_processing_wrap.cpp", "audio_processing.py", "webrtc_audio_processing.py"):
        shutil.copyfile(VENDOR / "src" / name, source / name)
    wrapper = source / "audio_processing_wrap.cpp"
    wrapper.write_text(wrapper.read_text().replace("0x040301", "0x040501"))
    proxy = source / "webrtc_audio_processing.py"
    proxy.write_text(proxy.read_text().replace("# Version 4.3.1", "# Version 4.5.1"))
    before = source_snapshot(source)
    namespace, _ = load_setup(monkeypatch)
    command_type = namespace["build_ext"]
    monkeypatch.setitem(command_type.run.__globals__, "__file__", str(tmp_path / "setup.py"))

    def native_work():
        pytest.fail("mismatched bindings reached native library lookup/build")

    monkeypatch.setitem(command_type.run.__globals__, "get_webrtc_library_path", native_work)
    command = command_type(setuptools.Distribution())
    with pytest.raises(RuntimeError, match="SWIG binding") as error:
        command.run()
    assert "4.5.1" in str(error.value) and "4.3.1" in str(error.value)
    assert "git restore" in str(error.value)
    assert_source_unchanged(before, source)


@pytest.mark.parametrize("hook", ["get_requires_for_build_wheel", "get_requires_for_build_sdist",
                                  "prepare_metadata_for_build_wheel", "build_wheel", "build_sdist"])
def test_backend_contains_all_build_writes_and_keeps_output_paths(tmp_path, monkeypatch, hook):
    backend = load_backend()
    source = tmp_path / "original"
    source.mkdir()
    (source / "input.txt").write_text("committed")
    (source / "build").mkdir()
    (source / "build" / "stale").touch()
    (source / "webrtc-audio-processing" / "install").mkdir(parents=True)
    monkeypatch.setattr(backend, "SOURCE", source)
    monkeypatch.chdir(tmp_path)
    before = source_snapshot(source)
    stages = []

    def delegate(*args):
        stage = Path.cwd()
        stages.append(stage)
        assert stage != source
        assert (stage / "input.txt").read_text() == "committed"
        assert not (stage / "build").exists()
        assert not (stage / "webrtc-audio-processing" / "install").exists()
        (stage / "input.txt").write_text("regenerated")
        (stage / "install").mkdir()
        if hook in {"build_wheel", "build_sdist", "prepare_metadata_for_build_wheel"}:
            assert Path(args[0]) == tmp_path / "output"
            Path(args[0]).mkdir()
            (Path(args[0]) / "artifact").write_text("built")
            if hook == "build_wheel":
                assert args[2] == str(tmp_path / "metadata")
        return "result"

    monkeypatch.setattr(backend, "build_meta", SimpleNamespace(**{hook: delegate}))
    arguments = ["output"] if hook in {"build_wheel", "build_sdist", "prepare_metadata_for_build_wheel"} else []
    keywords = {"metadata_directory": "metadata"} if hook == "build_wheel" else {}
    assert getattr(backend, hook)(*arguments, **keywords) == "result"
    assert Path.cwd() == tmp_path
    assert all(not path.exists() for path in stages)
    assert_source_unchanged(before, source)
    if arguments:
        assert (tmp_path / "output" / "artifact").read_text() == "built"


def test_backend_restores_cwd_and_removes_stage_after_build_failure(tmp_path, monkeypatch):
    backend = load_backend()
    monkeypatch.setattr(backend, "SOURCE", tmp_path)
    previous = Path.cwd()
    stages = []

    def fail(*args):
        stages.append(Path.cwd())
        raise RuntimeError("compiler failed")

    monkeypatch.setattr(backend, "build_meta", SimpleNamespace(build_wheel=fail))
    with pytest.raises(RuntimeError, match="compiler failed"):
        backend.build_wheel(tmp_path / "output")
    assert Path.cwd() == previous
    assert all(not path.exists() for path in stages)


def test_compiler_uses_the_committed_wrapper_and_matching_public_proxies(monkeypatch):
    namespace, configuration = load_setup(monkeypatch)
    extension, = configuration["ext_modules"]
    assert "src/audio_processing_wrap.cpp" in extension.sources
    assert not any(path.endswith(".i") for path in extension.sources)
    assert namespace["validate_swig_bindings"](VENDOR / "src") == (4, 3, 1)
    public = (VENDOR / "src/audio_processing.py").read_text()
    generated = (VENDOR / "src/webrtc_audio_processing.py").read_text()
    assert public == generated


@pytest.mark.parametrize("versions", [
    (0x040301, "4.3.1", "4.3.1"), (0x040501, "4.5.1", "4.5.1"),
    (0x040310, "4.3.10", "4.3.10"), (0x010311, "1.3.11", "1.3.11"),
    (0x041001, "4.10.1", "4.10.1"),
])
def test_binding_guard_accepts_matching_generations_with_either_line_ending(tmp_path, monkeypatch, versions):
    namespace, _ = load_setup(monkeypatch)
    (tmp_path / "audio_processing_wrap.cpp").write_bytes(
        f"#define SWIG_VERSION 0x{versions[0]:06x}\r\n".encode())
    (tmp_path / "audio_processing.py").write_bytes(f"# Version {versions[1]}\r\n".encode())
    (tmp_path / "webrtc_audio_processing.py").write_bytes(f"# Version {versions[2]}\n".encode())
    assert namespace["validate_swig_bindings"](tmp_path) == tuple(map(int, versions[1].split(".")))


@pytest.mark.parametrize("name", ["audio_processing_wrap.cpp", "audio_processing.py",
                                  "webrtc_audio_processing.py"])
@pytest.mark.parametrize("damage", ["different_version", "missing_header", "missing_file"])
def test_binding_guard_rejects_each_inconsistent_or_unreadable_input(tmp_path, monkeypatch, name, damage):
    namespace, _ = load_setup(monkeypatch)
    for filename in ("audio_processing_wrap.cpp", "audio_processing.py", "webrtc_audio_processing.py"):
        shutil.copyfile(VENDOR / "src" / filename, tmp_path / filename)
    damaged = tmp_path / name
    if damage == "missing_file":
        damaged.unlink()
    elif damage == "missing_header":
        damaged.write_text("not a generated binding")
    else:
        damaged.write_text(damaged.read_text().replace("0x040301", "0x040501")
                           .replace("# Version 4.3.1", "# Version 4.5.1"))
    before = source_snapshot(tmp_path)
    with pytest.raises(RuntimeError, match="SWIG binding") as error:
        namespace["validate_swig_bindings"](tmp_path)
    assert name in str(error.value)
    assert "git restore --source=HEAD -- vendor/aec-audio-processing/src" in str(error.value)
    assert_source_unchanged(before, tmp_path)


@pytest.mark.parametrize("version", [(3, 11, 9), (3, 13, 1), (3, 14, 0)])
def test_qualification_rejects_wrong_python_before_any_work(monkeypatch, version):
    from tools import verify_aec_source_build as verifier

    monkeypatch.setattr(verifier.sys, "version_info", version)
    monkeypatch.setattr(verifier, "source_snapshot", lambda path: pytest.fail("work started"))
    with pytest.raises(SystemExit, match="requires Python 3.12"):
        verifier.main()


def test_qualification_removes_inherited_rust_log_only_from_child_environment(tmp_path, monkeypatch):
    from tools import verify_aec_source_build as verifier

    monkeypatch.setenv("RUST_LOG", "warn")
    monkeypatch.setenv("UV_CACHE_DIR", "synthetic-cache")
    script = (
        "import os; assert 'RUST_LOG' not in os.environ; "
        "assert os.environ['UV_CACHE_DIR'] == 'synthetic-cache'; "
        f"print({verifier.ABSEIL_EVIDENCE!r})"
    )
    verifier.run([sys.executable, "-c", script], cwd=tmp_path, require_abseil=True)
    assert os.environ["RUST_LOG"] == "warn"
    assert os.environ["UV_CACHE_DIR"] == "synthetic-cache"


def test_qualification_still_rejects_missing_build_evidence(tmp_path, monkeypatch):
    from tools import verify_aec_source_build as verifier

    monkeypatch.setenv("RUST_LOG", "warn")
    with pytest.raises(RuntimeError, match="Missing actual Meson resolution evidence"):
        verifier.run([sys.executable, "-c", "print('no build occurred')"],
                     cwd=tmp_path, require_abseil=True)


def test_qualification_pins_both_builds_and_environments_to_the_running_python(tmp_path, monkeypatch):
    from tools import verify_aec_source_build as verifier

    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(verifier, "SOURCE", source)
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(verifier.sys, "version_info", (3, 12, 10))
    monkeypatch.setattr(verifier.sys, "platform", "linux")
    executable = str(tmp_path / "python3.12")
    monkeypatch.setattr(verifier.sys, "executable", executable)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "--sdist" in command:
            output = Path(command[command.index("--out-dir") + 1])
            with tarfile.open(output / "aec.tar.gz", "w:gz") as archive:
                archive.addfile(tarfile.TarInfo("aec/build_backend.py"))
        if "--wheel" in command:
            Path(command[command.index("--out-dir") + 1], "aec.whl").touch()

    monkeypatch.setattr(verifier, "run", run)
    monkeypatch.setattr(verifier, "check_wheel", lambda wheel, source: None)
    monkeypatch.setattr(verifier.subprocess, "run", lambda command, **kwargs: None)
    verifier.main()
    selected = [command for command in commands if command[1] in {"sync", "build", "venv"}]
    assert len(selected) == 4
    for command in selected:
        assert command[command.index("--python") + 1] == executable


def test_cleanliness_check_detects_hidden_artifacts_and_tracked_mutation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    tracked = source / "wrapper.cpp"
    tracked.write_bytes(b"original\r\n")
    before = source_snapshot(source)
    tracked.write_bytes(b"original\n")
    with pytest.raises(RuntimeError, match="wrapper.cpp"):
        assert_source_unchanged(before, source)
    tracked.write_bytes(b"original\r\n")
    (source / "build").mkdir()
    with pytest.raises(RuntimeError, match="build"):
        assert_source_unchanged(before, source)


@pytest.mark.parametrize("mismatch", [None, "audio_processing_wrap.cpp", "audio_processing.py",
                                     "webrtc_audio_processing.py"])
def test_wheel_qualification_rejects_a_stale_compiled_source_or_public_proxy(tmp_path, mismatch):
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in ("audio_processing_wrap.cpp", "audio_processing.py", "webrtc_audio_processing.py"):
            content = (VENDOR / "src" / name).read_bytes()
            archive.writestr(f"aec_audio_processing/{name}", b"stale" if name == mismatch else content)
        archive.writestr("aec_audio_processing/_webrtc_audio_processing.cpython-312-x86_64-linux-gnu.so", b"")
        archive.writestr("aec_audio_processing/files/libwebrtc-audio-processing-2.so", b"")
        archive.writestr("aec_audio_processing/files/THIRD_PARTY_NOTICES.txt", b"")
    if mismatch:
        with pytest.raises(RuntimeError, match="compiled source/proxy pair"):
            check_wheel(wheel, VENDOR)
    else:
        check_wheel(wheel, VENDOR)
