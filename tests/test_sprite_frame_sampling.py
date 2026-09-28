"""Run the renderer sampling contracts with the repository's Node VM harness."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_sprite_frame_sampling_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for renderer contracts")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([node, "--test", str(root / "tests/sprite_frame_sampling.test.cjs")],
                   cwd=root, check=True, capture_output=True, text=True, encoding="utf-8")
