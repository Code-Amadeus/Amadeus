"""Regenerate the supported Windows/Python 3.12 dependency locks.

Uses `uv pip compile --python-platform windows`, so locks can be regenerated
from any OS (pip-tools required running on Windows).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / "requirements" / "locks"
PROFILES = {
    # T1 core: conversation kernel, no audio, no local models.
    "cpu": {
        "extras": (),
        "output": LOCK_DIR / "windows-py312-cpu.txt",
    },
    # T1 + dev tooling for the CI/test environment.
    "ci": {
        "extras": ("dev",),
        "output": LOCK_DIR / "windows-py312-ci.txt",
    },
}


def _compile(profile: str) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required to generate locks; see https://docs.astral.sh/uv/")
    config = PROFILES[profile]
    command = [
        uv,
        "pip",
        "compile",
        "pyproject.toml",
        "--python-platform",
        "windows",
        "--python-version",
        "3.12",
        f"--output-file={config['output']}",
    ]
    for extra in config["extras"]:
        command.extend(("--extra", extra))
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile",
        nargs="?",
        choices=("all", *PROFILES),
        default="all",
    )
    args = parser.parse_args()
    profiles = list(PROFILES) if args.profile == "all" else [args.profile]
    for profile in profiles:
        print(f"[locks] compiling {profile} -> {PROFILES[profile]['output'].name}")
        _compile(profile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
