"""Run the wallpaper scene's aspect-preserving layout contracts with Node."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_wallpaper_cover_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for wallpaper scene contracts")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([node, "--test", str(root / "tests/wallpaper_cover.test.cjs")],
                   cwd=root, check=True, capture_output=True, text=True, encoding="utf-8")
