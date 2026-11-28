"""Run the Electron slice host's draggable-controls contracts with Node."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_electron_slice_host_draggable_controls():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Electron slice host contracts")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([node, "--test", str(root / "tests/electron_slice_host.test.cjs")],
                   cwd=root, check=True, capture_output=True, text=True, encoding="utf-8")
