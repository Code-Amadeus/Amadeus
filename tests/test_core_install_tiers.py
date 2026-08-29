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
    def find_module(self, name, path=None):
        if name.split(".")[0] in _BLOCKED:
            return self

    def load_module(self, name):
        raise ImportError(f"{{name}} blocked: not part of the T1 core install")

sys.meta_path.insert(0, _Blocker())
for mod in list(sys.modules):
    if mod.split(".")[0] in _BLOCKED:
        del sys.modules[mod]

import server.app  # noqa: F401
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


def test_secret_settings_strip_accidental_quotes_and_whitespace() -> None:
    probe = (
        "import os;"
        "os.environ['MIMO_TTS_API_KEY']='  \"sk-test123\"  ';"
        "from config import settings;"
        "assert settings.MIMO_TTS_API_KEY == 'sk-test123', repr(settings.MIMO_TTS_API_KEY);"
        "print('SECRET_OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    assert "SECRET_OK" in result.stdout


def test_venv_python_uses_platform_layout() -> None:
    import os
    from pathlib import Path

    from config.environment import venv_python

    path = venv_python(Path("/repo"), ".venv_asr")
    if os.name == "nt":
        assert path == Path("/repo/.venv_asr/Scripts/python.exe")
    else:
        assert path == Path("/repo/.venv_asr/bin/python3")


def test_qwen_asr_python_probe_uses_platform_layout() -> None:
    from asr.backends.qwen3_asr import _VENV_ASR_PYTHON

    import os
    if os.name != "nt":
        assert str(_VENV_ASR_PYTHON).endswith(".venv_asr/bin/python3")
