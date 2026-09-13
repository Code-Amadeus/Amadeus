from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
