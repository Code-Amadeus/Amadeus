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


@pytest.mark.parametrize("executable", [
    "electron.exe", "electron", "Electron.app/Contents/MacOS/Electron",
])
async def test_launcher_owns_the_installed_native_process(tmp_path, monkeypatch, executable):
    from tools import e2e_live_product_journey as journey

    package = tmp_path / "node_modules" / "electron"
    native = package / "dist" / executable
    native.parent.mkdir(parents=True)
    native.touch()
    (package / "path.txt").write_text(executable, encoding="utf-8")
    monkeypatch.setattr(journey, "ELECTRON_ROOT", tmp_path)
    monkeypatch.setattr(journey, "_port_is_open", lambda port: False)
    monkeypatch.setattr(journey, "_windows_launch_identity", lambda: None)
    monkeypatch.setattr(journey, "_require_windows_electron_profile", lambda identity: None)
    command = []

    class LaunchObserved(Exception):
        pass

    def popen(args, **kwargs):
        command.extend(args)
        raise LaunchObserved

    monkeypatch.setattr(journey.subprocess, "Popen", popen)
    product = journey.ElectronProduct(
        run_root=tmp_path, debug_port=9223, main_debug_port=9224,
        no_tts=True, identity={},
    )
    try:
        with pytest.raises(LaunchObserved):
            await product.start(startup_timeout=1)
    finally:
        if product.log_handle:
            product.log_handle.close()
    assert command[0] == str(native)
    assert "--inspect=127.0.0.1:9224" in command
    assert "." in command


async def test_startup_fixture_refuses_an_existing_inspector_before_launch(tmp_path, monkeypatch):
    from tools import e2e_live_product_journey as journey

    monkeypatch.setattr(journey, '_windows_launch_identity', lambda: None)
    monkeypatch.setattr(journey, '_require_windows_electron_profile', lambda identity: None)
    monkeypatch.setattr(journey, '_port_is_open', lambda port: port == 9224)
    product = journey.ElectronProduct(run_root=tmp_path, debug_port=9223, main_debug_port=9224,
                                     no_tts=True, identity={})
    with pytest.raises(RuntimeError, match='main inspector port 9224 is already in use'):
        await product.start(startup_timeout=1, main_startup_script='throw Error("must not execute")')
    assert product.process is None


async def test_packaged_launcher_does_not_require_an_npm_install(tmp_path, monkeypatch):
    from tools import e2e_live_product_journey as journey

    executable = tmp_path / "Amadeus"
    executable.touch()
    monkeypatch.setattr(journey, "ELECTRON_ROOT", tmp_path)
    monkeypatch.setattr(journey, "_port_is_open", lambda port: False)
    monkeypatch.setattr(journey, "_windows_launch_identity", lambda: None)
    monkeypatch.setattr(journey, "_require_windows_electron_profile", lambda identity: None)
    command = []

    class LaunchObserved(Exception):
        pass

    def popen(args, **kwargs):
        command.extend(args)
        raise LaunchObserved

    monkeypatch.setattr(journey.subprocess, "Popen", popen)
    product = journey.ElectronProduct(
        run_root=tmp_path, debug_port=9223, no_tts=True, identity={},
        packaged_executable=executable,
    )
    try:
        with pytest.raises(LaunchObserved):
            await product.start(startup_timeout=1)
    finally:
        if product.log_handle:
            product.log_handle.close()
    assert command[0] == str(executable)
    assert "." not in command


class StartupPage:
    """Small IPC/UI substitute; mismatched UI and persistence fail separately."""

    def __init__(self, platform, *, visible=False, pending=False, persist=True):
        self.platform = platform
        self.visible = visible
        self.pending = pending
        self.persist = persist
        self.selected = []
        self.value = None
        self.waited = False

    async def evaluate(self, expression):
        assert expression == "window.amadeus.getDesktopSettings()"
        return {
            "platform": self.platform,
            "values": {"AMADEUS_WINDOWS_STARTUP_MODE": self.value},
            "pendingKeys": ["AMADEUS_WINDOWS_STARTUP_MODE"] if self.pending else [],
        }

    def get_by_role(self, role, **kwargs):
        assert (role, kwargs) == ("combobox", {"name": "Startup mode", "exact": True})
        return self

    async def wait_for(self, **kwargs):
        assert self.visible
        self.waited = True

    async def select_option(self, value):
        assert self.visible
        self.selected.append(value)
        if self.persist:
            self.value = value

    async def to_have_count(self, count):
        assert int(self.visible) == count


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("visible", [False, True])
async def test_non_windows_checks_absence_instead_of_skipping_settings(monkeypatch, platform, visible):
    import playwright.async_api
    from tools.smoke_electron_model_less import _exercise_startup_settings

    monkeypatch.setattr(playwright.async_api, "expect", lambda locator: locator)
    page = StartupPage(platform, visible=visible)
    if visible:
        with pytest.raises(AssertionError):
            await _exercise_startup_settings(page, timeout=0)
    else:
        assert await _exercise_startup_settings(page, timeout=0) == {
            "windows_startup_mode_hidden": True,
        }
    assert not page.waited
    assert page.selected == []


@pytest.mark.parametrize("platform", [None, "", "unknown"])
async def test_startup_settings_rejects_unknown_platform(platform):
    from tools.smoke_electron_model_less import _exercise_startup_settings

    with pytest.raises(RuntimeError, match="Unknown desktop platform"):
        await _exercise_startup_settings(StartupPage(platform), timeout=0)


async def test_windows_still_saves_both_modes_and_checks_restart_semantics():
    from tools.smoke_electron_model_less import _exercise_startup_settings

    page = StartupPage("win32", visible=True)
    assert await _exercise_startup_settings(page, timeout=0) == {"startup_mode_saved_in_gui": True}
    assert page.selected == ["window", "wallpaper"]
    with pytest.raises(RuntimeError, match="backend restart"):
        await _exercise_startup_settings(StartupPage("win32", visible=True, pending=True), timeout=0)
    with pytest.raises(TimeoutError, match="did not persist"):
        await _exercise_startup_settings(StartupPage("win32", visible=True, persist=False), timeout=0)
