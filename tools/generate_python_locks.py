"""Regenerate the supported Windows/Python 3.12 dependency locks."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / "requirements" / "locks"
PROFILES = {
    "cpu": {
        "extra": None,
        "index": "https://download.pytorch.org/whl/cpu",
        "output": LOCK_DIR / "windows-py312-cpu.txt",
    },
    "ci": {
        "extra": "dev",
        "index": "https://download.pytorch.org/whl/cpu",
        "output": LOCK_DIR / "windows-py312-ci.txt",
    },
}


def _require_python_312() -> None:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit(
            "locks must be generated with CPython 3.12; "
            f"current interpreter is {sys.version.split()[0]}"
        )


def _compile(profile: str) -> None:
    config = PROFILES[profile]
    command = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
        "pyproject.toml",
        "--resolver=backtracking",
        "--strip-extras",
        "--index-url=https://pypi.org/simple",
        f"--extra-index-url={config['index']}",
        f"--output-file={config['output']}",
    ]
    if config["extra"]:
        command.extend(("--extra", str(config["extra"])))
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile",
        nargs="?",
        choices=("all", *PROFILES),
        default="all",
        help="lock profile to regenerate (default: all release locks)",
    )
    args = parser.parse_args()

    _require_python_312()
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    profiles = tuple(PROFILES) if args.profile == "all" else (args.profile,)
    for profile in profiles:
        print(f"generating {profile} lock", flush=True)
        _compile(profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
