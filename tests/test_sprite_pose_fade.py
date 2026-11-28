"""Pose-release cross-fade contracts: an emotion switch without authored
transition frames dissolves the outgoing frame instead of hard-cutting."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_sprite_pose_fade_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for renderer contracts")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [node, "--test", str(root / "tests/sprite_pose_fade.test.cjs")],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
