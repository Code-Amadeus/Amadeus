"""Qualify a cold Linux Voice install and an independently installed AEC wheel."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vendor" / "aec-audio-processing"
ABSEIL_EVIDENCE = "AEC build: forced bundled Abseil 20240722.0"


def source_snapshot(source: Path) -> dict[str, str]:
    """Include ignored build/install output and empty directories, but not bytecode."""
    result = {}
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        key = path.relative_to(source).as_posix()
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<dir>"
    return result


def assert_source_unchanged(before: dict[str, str], source: Path) -> None:
    after = source_snapshot(source)
    changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
    if changed:
        raise RuntimeError("AEC build changed source paths: " + ", ".join(changed[:20]))


def run(command: list[str], *, cwd: Path, require_abseil: bool = False) -> None:
    environment = os.environ.copy()
    # An inherited Rust filter can suppress uv's verbose build evidence.
    environment.pop("RUST_LOG", None)
    completed = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                               env=environment)
    if completed.returncode:
        print(completed.stdout)
        raise RuntimeError(f"{command[0]} {command[1]} failed ({completed.returncode})")
    if require_abseil:
        if ABSEIL_EVIDENCE not in completed.stdout:
            raise RuntimeError("Missing actual Meson resolution evidence; a cold build is required")
        print(ABSEIL_EVIDENCE)


def check_wheel(wheel: Path, source: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for name in ("audio_processing_wrap.cpp", "audio_processing.py", "webrtc_audio_processing.py"):
            if archive.read(f"aec_audio_processing/{name}") != (source / "src" / name).read_bytes():
                raise RuntimeError(f"Wheel does not contain the compiled source/proxy pair: {name}")
        if not any(name.startswith("aec_audio_processing/_webrtc_audio_processing.")
                   and name.endswith(".so") for name in names):
            raise RuntimeError("Wheel is missing its Python native extension")
        if "aec_audio_processing/files/libwebrtc-audio-processing-2.so" not in names:
            raise RuntimeError("Wheel is missing the WebRTC runtime library")
        if "aec_audio_processing/files/THIRD_PARTY_NOTICES.txt" not in names:
            raise RuntimeError("Wheel is missing third-party notices")


NATIVE_SMOKE = """
from aec_audio_processing import AudioProcessor
from aec_audio_processing.audio_processing import AudioProcessor as PublicProxy
assert AudioProcessor is PublicProxy
processor = AudioProcessor(enable_aec=True, enable_ns=False, enable_agc=False, enable_vad=False)
processor.set_stream_format(48000, 1, 48000, 1)
processor.set_reverse_stream_format(48000, 1)
processor.set_stream_delay(0)
frame = bytes(480 * 2)
for _ in range(10):
    processor.process_reverse_stream(frame)
    assert processor.process_stream(frame) == frame
print("Installed wheel public AudioProcessor native smoke: PASS")
"""


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("AEC qualification requires Python 3.12; rerun with python3.12.")
    if sys.platform != "linux":
        raise SystemExit("This native source-build qualification requires Linux.")
    before = source_snapshot(SOURCE)
    # The CI job supplies a fresh UV_CACHE_DIR. Require build evidence so a
    # cached wheel cannot turn this qualification into an install-only check.
    run(["uv", "sync", "--locked", "--extra", "voice", "--extra", "dev",
         "--python", sys.executable, "--verbose"],
        cwd=ROOT, require_abseil=True)
    assert_source_unchanged(before, SOURCE)
    with tempfile.TemporaryDirectory(prefix="amadeus-aec-qualification-") as directory:
        output = Path(directory)
        run(["uv", "build", "--sdist", str(SOURCE), "--out-dir", str(output),
             "--python", sys.executable], cwd=ROOT)
        sdist, = output.glob("*.tar.gz")
        with tarfile.open(sdist) as archive:
            if not any(name.endswith("/build_backend.py") for name in archive.getnames()):
                raise RuntimeError("Source release is missing the isolated build backend")
        run(["uv", "build", "--wheel", str(sdist), "--out-dir", str(output),
             "--python", sys.executable, "--verbose"],
            cwd=output, require_abseil=True)
        wheel, = output.glob("*.whl")
        check_wheel(wheel, SOURCE)
        environment = output / "venv"
        run(["uv", "venv", str(environment), "--python", sys.executable], cwd=output)
        python = environment / "bin" / "python"
        run(["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)], cwd=output)
        # -I and an external cwd keep the checkout and user site out of imports.
        subprocess.run([str(python), "-I", "-c", NATIVE_SMOKE], cwd=output, check=True,
                       env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
    assert_source_unchanged(before, SOURCE)
    print("Cold Voice install, sdist/wheel, native smoke and source cleanliness: PASS")


if __name__ == "__main__":
    main()
