"""The vendored build must not write back or mix SWIG generations."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import runpy
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
    configuration = {}
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: configuration.update(kwargs))
    runpy.run_path(str(VENDOR / "setup.py"))
    extension, = configuration["ext_modules"]
    assert "src/audio_processing_wrap.cpp" in extension.sources
    assert not any(path.endswith(".i") for path in extension.sources)
    wrapper = (VENDOR / "src/audio_processing_wrap.cpp").read_text()
    match = re.search(r"#define SWIG_VERSION 0x([0-9a-fA-F]{6})", wrapper)
    version = ".".join(str(int(match[1][index:index + 2], 16)) for index in (0, 2, 4))
    for name in ("audio_processing.py", "webrtc_audio_processing.py"):
        assert f"# Version {version}" in (VENDOR / "src" / name).read_text()
    public = (VENDOR / "src/audio_processing.py").read_text()
    generated = (VENDOR / "src/webrtc_audio_processing.py").read_text()
    assert public == generated


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
