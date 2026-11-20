"""Run the frame-video runtime and provider contracts in the normal Python CI suites."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("suite", [
    "sprite_video_source.test.cjs",
    "frame_source_configuration.test.cjs",
    "hybrid_fixture.test.mjs",
    "hybrid_frame_provider.test.mjs",
    "hybrid_lifecycle.test.mjs",
    "frame_video_probe.test.mjs",
    "frame_video_shutdown_control.test.mjs",
])
def test_frame_video_contracts(suite):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for renderer contracts")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [node, "--test", str(root / "tests" / suite)],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
