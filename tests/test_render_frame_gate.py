"""Run real-Pixi presentation cadence and elapsed-time contracts."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_render_frame_gate_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for renderer contracts")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([node, "--test", str(root / "tests/render_frame_gate.test.cjs")],
                   cwd=root, check=True, capture_output=True, text=True, encoding="utf-8")
