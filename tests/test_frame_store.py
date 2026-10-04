"""Run the isolated texture ownership contracts with a fake Node backend."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_frame_store_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for renderer contracts")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([node, "--test", str(root / "tests/frame_store.test.cjs")],
                   cwd=root, check=True, capture_output=True, text=True, encoding="utf-8")
