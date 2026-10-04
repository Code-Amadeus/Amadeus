"""Run the renderer and real FrameStore ownership boundary with a fake backend."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_sprite_texture_lifecycle_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for renderer contracts")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [node, "--test", str(root / "tests/sprite_texture_lifecycle.test.cjs")],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
