from __future__ import annotations

import sys
import asyncio
import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from tools.smoke_electron_model_less import model_less_backend_environment, _wait_desktop_value


def test_model_less_smoke_uses_isolated_disabled_voice_profile(tmp_path: Path) -> None:
    env = model_less_backend_environment(
        {
            "AMADEUS_UI_THEME": "wallpaper-slice",
            "MIMO_TTS_API_KEY": "synthetic-parent-secret",
            "UNRELATED_SECRET": "synthetic-parent-secret",
            "AMADEUS_SESSION_DIR": str(tmp_path / "sessions"),
            "AMADEUS_WORK_LEDGER_PATH": str(tmp_path / "work.sqlite3"),
            "AMADEUS_ELECTRON_USER_DATA_DIR": str(tmp_path / "electron"),
        },
        python_executable=sys.executable,
    )

    assert "AMADEUS_UI_THEME" not in env
    assert "MIMO_TTS_API_KEY" not in env
    assert "UNRELATED_SECRET" not in env
    assert env["AMADEUS_PYTHON"] == sys.executable
    assert env["AMADEUS_E2E_NO_TTS"] == "1"
    assert env["TTS_BACKEND"] == "disabled"
    assert env["TTS_DEVICE"] == "cpu"
    assert env["WAKE_ENABLED"] == "0"
    assert env["VTS_ENABLED"] == "0"
    assert env["AEC_REALTIME_ENABLED"] == "0"
    assert env["AMADEUS_WALLPAPER"] == "0"
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


@pytest.mark.parametrize('saved', [None, 'wallpaper-slice'])
def test_recovery_wait_rejects_unpersisted_values(saved):
    class Page:
        async def evaluate(self, expression):
            return {'values': {'AMADEUS_UI_THEME': saved}}
    with pytest.raises(TimeoutError):
        asyncio.run(_wait_desktop_value(Page(), 'AMADEUS_UI_THEME', 'classic', timeout=0))


def test_recovery_wait_observes_eventual_persisted_value():
    class Page:
        calls = 0
        async def evaluate(self, expression):
            self.calls += 1
            return {'values': {'AMADEUS_UI_THEME': 'classic' if self.calls > 1 else 'wallpaper-slice'}}
    page = Page()
    asyncio.run(_wait_desktop_value(page, 'AMADEUS_UI_THEME', 'classic', timeout=2))
    assert page.calls >= 2



def test_recovery_wait_bounds_an_ipc_call_that_never_returns():
    class Page:
        async def evaluate(self, expression):
            return await asyncio.Future()
    with pytest.raises(TimeoutError):
        asyncio.run(_wait_desktop_value(Page(), 'AMADEUS_UI_THEME', 'classic', timeout=.02))



def test_source_archive_identity_checks_actual_files_without_parent_git(tmp_path):
    import hashlib
    import json
    from tools.smoke_electron_model_less import launcher_code_identity

    source = tmp_path / "unpacked"
    source.mkdir()
    (source / "main.py").write_bytes(b"print('release')")
    manifest = {"schema":"amadeus.source-release-manifest.v1", "release_ready":True,
        "commit":"a"*40, "selected_file_count":1, "selected_files":[{
            "path":"main.py", "size":len(b"print('release')"),
            "sha256":hashlib.sha256(b"print('release')").hexdigest()}]}
    (source / "SOURCE_MANIFEST.json").write_text(json.dumps(manifest))
    identity = launcher_code_identity(source)
    assert identity["commit_sha"] == "a"*40
    assert identity["identity_kind"] == "source-release-manifest"
    assert not identity["workspace_dirty"]
    (source / "main.py").write_bytes(b"print('changed')")
    with pytest.raises(ValueError, match="differs"):
        launcher_code_identity(source)
    (source / "main.py").unlink()
    with pytest.raises(ValueError, match="missing"):
        launcher_code_identity(source)
    manifest["selected_files"][0]["path"] = "../outside.py"
    (source / "SOURCE_MANIFEST.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="file path"):
        launcher_code_identity(source)


@pytest.mark.parametrize("other", [
    ["--extension-fixture", "legacy-fixture"],
    ["--extension-failures"],
    ["--native-confirmations"],
])
def test_publication_journey_rejects_mixed_profile_modes_before_launch(other):
    from tools.smoke_electron_model_less import _parser, run_smoke
    args = _parser().parse_args(["--extension-publication", "demo.json", *other])
    with pytest.raises(ValueError, match="separate complete journey"):
        asyncio.run(run_smoke(args))
