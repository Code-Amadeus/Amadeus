"""API key parsing tolerates accidental quotes/whitespace from .env edits."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    assert "SECRET_OK" in result.stdout
