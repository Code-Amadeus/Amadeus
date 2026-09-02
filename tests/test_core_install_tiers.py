"""T1 core install contract: headless tier must boot without voice/local stacks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Voice (T2) and local-model (T2b) packages that a T1 install does not have.
_NON_CORE_MODULES = (
    "torch",
    "torchaudio",
    "pyaudio",
    "onnxruntime",
    "scipy",
    "silero_vad",
    "aec_audio_processing",
    "soundfile",
    "av",
)

_BOOT_PROBE = f"""
import sys

_BLOCKED = {set(_NON_CORE_MODULES)!r}

class _Blocker:
    # Simulate a missing package for `import` statements: the finder raises
    # ModuleNotFoundError, which is what a real T1 install raises.
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED:
            # name= set so guards can distinguish "package absent" from
            # "package present but its own dependencies are broken".
            raise ModuleNotFoundError(
                f"{{name}} is not part of the T1 core install", name=name
            )
        return None

sys.meta_path.insert(0, _Blocker())
for mod in list(sys.modules):
    if mod.split(".")[0] in _BLOCKED:
        del sys.modules[mod]

import server.app  # noqa: F401
# asr manager / mic_input_service are reachable outside server.app's lazy
# import chain (tests, config-only journeys); they must stay importable on
# a T1 install even though asr.microphone pulls pyaudio at its module top.
import asr.mic_input_service  # noqa: F401, E402
import asr.manager  # noqa: F401, E402
print("T1_BOOT_OK")
"""


def test_server_app_boots_without_voice_or_local_stacks() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _BOOT_PROBE],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"T1 core import failed without voice/local stacks:\n{result.stderr[-2000:]}"
    )
    assert "T1_BOOT_OK" in result.stdout
