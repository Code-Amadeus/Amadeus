from __future__ import annotations

import plistlib
import platform
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_macos_wallpaper_app.sh"


def test_launchagent_writer_preserves_app_path_with_spaces(tmp_path: Path) -> None:
    output = tmp_path / "com.amadeus.wallpaper.plist"
    program = tmp_path / "Amadeus Wallpaper.app" / "Contents" / "MacOS" / "Amadeus Wallpaper"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "write_macos_wallpaper_plist.py"),
            "--label",
            "com.amadeus.wallpaper",
            "--program",
            str(program),
            "--stdout",
            str(tmp_path / "stdout.log"),
            "--stderr",
            str(tmp_path / "stderr.log"),
            "--output",
            str(output),
        ],
        check=True,
    )

    with output.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["ProgramArguments"] == [str(program)]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is False


@pytest.mark.skipif(platform.system() == "Windows", reason="Windows cannot directly execute POSIX shell scripts")
def test_force_rejects_unrelated_directory_without_removing_contents(tmp_path: Path) -> None:
    unrelated = tmp_path / "Applications"
    unrelated.mkdir()
    sentinel = unrelated / "keep-me.txt"
    sentinel.write_text("unrelated", encoding="utf-8")

    result = subprocess.run(
        [str(BUILD_SCRIPT), "--output", str(unrelated), "--force"],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "Refusing output path" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "unrelated"


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS app bundle build")
def test_force_rejects_unrelated_same_named_bundle_without_removing_contents(tmp_path: Path) -> None:
    unrelated = tmp_path / "Amadeus Wallpaper.app"
    unrelated.mkdir()
    sentinel = unrelated / "keep-me.txt"
    sentinel.write_text("unrelated", encoding="utf-8")

    result = subprocess.run(
        [str(BUILD_SCRIPT), "--output", str(unrelated), "--force"],
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "not a generated Amadeus Wallpaper.app bundle" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "unrelated"


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS app bundle build")
def test_force_rebuilds_generated_app_bundle(tmp_path: Path) -> None:
    output = tmp_path / "Amadeus Wallpaper.app"
    command = [str(BUILD_SCRIPT), "--output", str(output), "--force"]

    subprocess.run(command, check=True)
    subprocess.run(command, check=True)

    with (output / "Contents" / "Info.plist").open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["CFBundleIdentifier"] == "com.amadeus.wallpaper"
    assert (output / "Contents" / "MacOS" / "Amadeus Wallpaper").is_file()
    assert (output / "Contents" / "Resources" / "project-root").is_file()
