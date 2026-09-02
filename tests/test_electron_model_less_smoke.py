from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from tools.smoke_electron_model_less import model_less_backend_environment


def test_model_less_smoke_uses_isolated_disabled_voice_profile(tmp_path: Path) -> None:
    env = model_less_backend_environment(
        {
            "AMADEUS_SESSION_DIR": str(tmp_path / "sessions"),
            "AMADEUS_WORK_LEDGER_PATH": str(tmp_path / "work.sqlite3"),
            "AMADEUS_ELECTRON_USER_DATA_DIR": str(tmp_path / "electron"),
        },
        python_executable=sys.executable,
    )

    assert env["AMADEUS_PYTHON"] == sys.executable
    assert env["AMADEUS_E2E_NO_TTS"] == "1"
    assert env["TTS_BACKEND"] == "disabled"
    assert env["TTS_DEVICE"] == "cpu"
    assert env["WAKE_ENABLED"] == "0"
    assert env["VTS_ENABLED"] == "0"
    assert env["AEC_REALTIME_ENABLED"] == "0"
    assert env["LLM_PROVIDER"] == "deepseek"
    assert env["DEEPSEEK_API_KEY"] == ""
    assert env["OPENAI_API_KEY"] == ""
    assert env["AUIP_APPSESSION_ROLE_BRANCH_MODE"] == "b2"
    assert env["AUIP_CONTROL_DECISION_ENABLED"] == "1"
    assert env["AUIP_ACTION_PROVIDER"] == ""
    assert env["AUIP_ACTION_MODEL"] == ""
    assert Path(env["AMADEUS_SESSION_DIR"]).is_relative_to(tmp_path)
    assert Path(env["AMADEUS_WORK_LEDGER_PATH"]).is_relative_to(tmp_path)
    assert Path(env["AMADEUS_ELECTRON_USER_DATA_DIR"]).is_relative_to(tmp_path)


def test_python_windows_ci_runs_the_real_electron_smoke() -> None:
    workflow = (ROOT / ".github" / "workflows" / "python-windows.yml").read_text(
        encoding="utf-8"
    )

    assert "actions/setup-node@v7" in workflow
    assert "npm run build" in workflow
    assert "tools\\smoke_electron_model_less.py" in workflow
    assert "build\\electron-smoke" in workflow
    assert "AUIP_APPSESSION_ROLE_BRANCH_MODE" not in workflow
