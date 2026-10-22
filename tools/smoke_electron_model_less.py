"""Exercise the shipping Electron shell without models, credentials, or assets.

The smoke launches the built Electron application. Electron starts the normal
authenticated Python backend, and Playwright connects to Electron's own Chromium
over a loopback CDP port. No chat turn, Provider work, microphone, TTS, model, or
character package is required.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "amadeus.electron-model-less-smoke.v1"


def model_less_backend_environment(
    base: dict[str, str], *, python_executable: str
) -> dict[str, str]:
    # The native-key probe opens a temporary main inspector in this isolated
    # test process. Never forward arbitrary parent secrets into that process.
    platform_keys = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
                     'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES',
                     'PROGRAMFILES(X86)', 'COMMONPROGRAMFILES', 'HOMEDRIVE', 'HOMEPATH',
                     'HOME', 'LANG', 'LC_ALL', 'DISPLAY', 'XAUTHORITY'}
    test_keys = {'NODE_ENV', 'AMADEUS_SESSION_DIR', 'AMADEUS_WORK_LEDGER_PATH',
                'AMADEUS_PROVIDER_ACTIVITY_PATH', 'AMADEUS_SERVER_LOG_PATH', 'AMADEUS_DESKTOP_PATH',
                'AMADEUS_ELECTRON_USER_DATA_DIR', 'AMADEUS_ELECTRON_CACHE_DIR',
                'WORK_SCRATCH_ROOT', 'WORK_WORKTREE_ROOT', 'WORK_PROJECT_ALLOWLIST',
                'AMADEUS_PRE_TRANSLATION_ENABLED', 'PYTHONUTF8', 'PYTHONIOENCODING',
                'AMADEUS_CODE_SHA', 'AMADEUS_WORKSPACE_DIRTY', 'AMADEUS_WORKSPACE_FINGERPRINT',
                'AMADEUS_BACKEND_AUTH_MODE', 'AMADEUS_BACKEND_TOKEN', 'AMADEUS_BACKEND_INSTANCE_NONCE'}
    env = {key: value for key, value in base.items() if key.upper() in platform_keys or key in test_keys}
    env.update(
        {
            "AMADEUS_PYTHON": python_executable,
            "AMADEUS_E2E_NO_TTS": "1",
            "TTS_BACKEND": "disabled",
            "TTS_DEVICE": "cpu",
            "WAKE_ENABLED": "0",
            "VTS_ENABLED": "0",
            "AEC_REALTIME_ENABLED": "0",
            # This compatibility journey exercises Chat/Settings without a
            # wallpaper host or character pack, including on Windows.
            "AMADEUS_WALLPAPER": "0",
            # Exercise the shipping first-run boundary deterministically:
            # B2 remains selected, no remote credential can be inherited from
            # a developer desktop, and the backend must still expose Settings.
            "LLM_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "",
            "OPENAI_API_KEY": "",
            "AUIP_APPSESSION_ROLE_BRANCH_MODE": "b2",
            "AUIP_CONTROL_DECISION_ENABLED": "1",
            "AUIP_ACTION_PROVIDER": "",
            "AUIP_ACTION_MODEL": "",
            "AUIP_ACTION_REASONING_EFFORT": "none",
        }
    )
    return env


async def _wait_enabled(locator: Any, *, timeout: float) -> None:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if await locator.is_visible() and await locator.is_enabled():
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("Chat input did not become enabled after backend connection")


async def _wait_desktop_value(page: Any, key: str, expected: str, *, timeout: float) -> None:
    # wait_for_function polls Promise truthiness, not its asynchronously resolved
    # value. Await the IPC result in Python and compare the actual saved value.
    deadline = time.monotonic() + timeout
    while True:
        snapshot = await asyncio.wait_for(page.evaluate("window.amadeus.getDesktopSettings()"),
                                          timeout=max(0.001, deadline - time.monotonic()))
        if snapshot.get("values", {}).get(key) == expected:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Desktop setting {key} did not persist the expected value")
        await asyncio.sleep(0.1)


def _frontend_build_identity(directory: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    files = [directory] if directory.is_file() else sorted(file for file in directory.rglob('*') if file.is_file())
    if not files:
        raise FileNotFoundError('The frontend must be built before the smoke')
    for file in files:
        digest.update((file.name if directory.is_file() else file.relative_to(directory).as_posix()).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    return {"kind": "packaged-asar" if directory.is_file() else "source-dist", "sha256": digest.hexdigest(), "file_count": len(files)}


def launcher_code_identity(root: Path) -> dict[str, Any]:
    """Identify a Git checkout or verify the shipped source-manifest file set.

    A ZIP nested inside another checkout must not inherit that parent's Git HEAD.
    Manifest identity describes the release inputs; installed dependencies and built
    frontend bytes have their own lock/build evidence. It is not a signature.
    """
    from tools.semantic_journey_evidence import code_identity

    manifest_path = root / "SOURCE_MANIFEST.json"
    if (root / ".git").exists() or not manifest_path.is_file():
        return code_identity(root)
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    records = manifest.get("selected_files")
    commit = manifest.get("commit", "")
    if (manifest.get("schema") != "amadeus.source-release-manifest.v1"
            or manifest.get("release_ready") is not True
            or not re.fullmatch(r"[a-f0-9]{40}", commit)
            or not isinstance(records, list) or not records
            or manifest.get("selected_file_count") != len(records)):
        raise ValueError("Invalid source release identity")
    seen: set[str] = set()
    for record in records:
        name = record.get("path", "")
        if (not isinstance(name, str) or not name or "\\" in name
                or ":" in name or name.startswith("/")
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or name in seen):
            raise ValueError("Invalid source release file path")
        seen.add(name)
        file = root / name
        if not file.resolve().is_relative_to(root.resolve()) or not file.is_file():
            raise ValueError("Source release input is missing or outside the archive")
        data = file.read_bytes()
        if len(data) != record.get("size") or hashlib.sha256(data).hexdigest() != record.get("sha256"):
            raise ValueError("Source release input differs from its manifest: " + name)
    digest = hashlib.sha256(raw).hexdigest()
    return {"commit_sha": commit, "workspace_dirty": False,
            "workspace_fingerprint": digest, "identity_kind": "source-release-manifest",
            "verified_source_files": len(records)}


async def _press_native_theme_recovery(process_id: int, inspector_port: int) -> None:
    """Exercise the actual Electron input hook in this isolated owned process."""
    import websockets

    def target() -> str:
        connection = http.client.HTTPConnection("127.0.0.1", inspector_port, timeout=3)
        try:
            connection.request("GET", "/json/list")
            response = connection.getresponse()
            targets = json.loads(response.read())
            if response.status != 200 or len(targets) != 1:
                raise RuntimeError("The smoke's main inspector is unavailable or ambiguous")
            address = targets[0]["webSocketDebuggerUrl"]
            if not address.startswith(f"ws://127.0.0.1:{inspector_port}/"):
                raise RuntimeError("The smoke inspector must remain on its own loopback port")
            return address
        finally:
            connection.close()

    expression = """(() => {
      if (process.pid !== %d) throw new Error('Unexpected Electron process');
      const { BrowserWindow } = process.getBuiltinModule('module').createRequire(process.execPath)('electron');
      const windows = BrowserWindow.getAllWindows().filter(window => {
        try { return new URL(window.webContents.getURL()).searchParams.get('mainWindow') === '1' } catch { return false }
      });
      if (windows.length !== 1) throw new Error('Main window identity is ambiguous');
      const contents = windows[0].webContents;
      contents.sendInputEvent({type:'keyDown',keyCode:'F12',modifiers:['control','shift']});
      contents.sendInputEvent({type:'keyUp',keyCode:'F12',modifiers:['control','shift']});
      return true;
    })()""" % process_id
    async with websockets.connect(await asyncio.to_thread(target), open_timeout=3) as websocket:
        await websocket.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True}}))
        while True:
            reply = json.loads(await asyncio.wait_for(websocket.recv(), timeout=3))
            if reply.get('id') == 1:
                if reply.get('result', {}).get('result', {}).get('value') is not True:
                    raise RuntimeError('The owned Electron process did not accept the native recovery key')
                return


async def _exercise_startup_settings(page: Any, *, timeout: float) -> dict[str, bool]:
    from playwright.async_api import expect

    desktop = await page.evaluate("window.amadeus.getDesktopSettings()")
    platform = desktop.get("platform")
    startup_mode = page.get_by_role("combobox", name="Startup mode", exact=True)
    if platform in {"linux", "darwin"}:
        await expect(startup_mode).to_have_count(0)
        return {"windows_startup_mode_hidden": True}
    if platform != "win32":
        raise RuntimeError(f"Unknown desktop platform: {platform!r}")

    await startup_mode.wait_for(state="visible", timeout=max(1_000, int(timeout * 1_000)))
    for mode in ("window", "wallpaper"):
        await startup_mode.select_option(mode)
        await _wait_desktop_value(page, "AMADEUS_WINDOWS_STARTUP_MODE", mode, timeout=timeout)
    desktop = await page.evaluate("window.amadeus.getDesktopSettings()")
    if "AMADEUS_WINDOWS_STARTUP_MODE" in desktop.get("pendingKeys", []):
        raise RuntimeError("startup preference incorrectly requires a backend restart")
    return {"startup_mode_saved_in_gui": True}


async def _exercise_renderer(page: Any, *, timeout: float, process_id: int, inspector_port: int) -> dict[str, bool]:
    from playwright.async_api import expect

    timeout_ms = max(1_000, int(timeout * 1_000))
    checks: dict[str, bool] = {}

    chat_input = page.locator('textarea[placeholder*="Type a message"]')
    await chat_input.wait_for(state="visible", timeout=timeout_ms)
    await _wait_enabled(chat_input, timeout=timeout)
    checks["chat_connected"] = True

    # Backend status lives in Settings › Diagnostics.
    await page.get_by_role("button", name="Settings", exact=True).click()
    await page.get_by_role("button", name="Diagnostics", exact=True).click()
    await page.get_by_role("heading", name="Server log", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_text("Connected", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    checks["backend_diagnostics_connected"] = True

    # Plugins is a sidebar page whether the sidebar is expanded or collapsed; General still links to it.
    plugins = page.get_by_role('button', name='Plugins', exact=True)
    await expect(plugins).to_have_count(1)
    await page.get_by_role('button', name='Collapse navigation', exact=True).click()
    await expect(plugins).to_have_count(1)
    await page.get_by_role('button', name='Expand navigation', exact=True).click()
    await plugins.click()
    await page.get_by_role('heading', name='Plugins', exact=True).wait_for(state='visible', timeout=timeout_ms)
    await page.get_by_role('button', name='Install plugin…', exact=True).wait_for(state='visible', timeout=timeout_ms)
    checks['plugins_in_sidebar'] = True
    await page.get_by_role('button', name='Settings', exact=True).click()
    await page.get_by_role('button', name='General', exact=True).click()
    await page.get_by_role('button', name='Manage plugins', exact=True).click()
    await page.get_by_role('heading', name='Plugins', exact=True).wait_for(state='visible', timeout=timeout_ms)
    checks['settings_links_to_plugins'] = True

    # Establish an actual transition, checking IPC results before any native key.
    for theme in ('classic', 'wallpaper-slice'):
        update = await page.evaluate("theme => window.amadeus.updateDesktopSettings({values:{AMADEUS_UI_THEME:theme}})", theme)
        if not update.get('ok'):
            raise RuntimeError(f"Theme setup failed: {update.get('error')}")
        await expect(page.locator("html")).to_have_attribute("data-theme", theme, timeout=timeout_ms)
        await _wait_desktop_value(page, 'AMADEUS_UI_THEME', theme, timeout=timeout)
    await _press_native_theme_recovery(process_id, inspector_port)
    await expect(page.locator("html")).to_have_attribute("data-theme", "classic", timeout=timeout_ms)
    await _wait_desktop_value(page, 'AMADEUS_UI_THEME', 'classic', timeout=timeout)
    checks['theme_recovery_uses_native_keyboard_boundary'] = True

    await page.get_by_role("button", name="Settings", exact=True).click()
    await page.get_by_role("heading", name="Settings", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_role("button", name="General", exact=True).click()
    checks.update(await _exercise_startup_settings(page, timeout=timeout))

    await page.get_by_role("button", name="Voice", exact=True).click()
    await page.get_by_text("Speech synthesis", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_role("button", name="Capabilities", exact=True).click()
    await page.get_by_role("tab", name="Runtime packages", exact=True).click()
    await page.get_by_text("Visual Runtime Pack", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_text("Kurisu Character Pack", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    checks["settings_and_runtime_packages_rendered"] = True

    await page.get_by_role("button", name="Models", exact=True).click()
    await page.get_by_text("Advanced role overrides", exact=True).click()
    auip_action_role = page.get_by_text("AUIP action decision", exact=True).last
    await auip_action_role.wait_for(state="visible", timeout=timeout_ms)
    auip_action_card = auip_action_role.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' setting-card ')][1]"
    )
    await auip_action_card.get_by_text("Needs setup", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await auip_action_card.get_by_text(
        re.compile(r"Application actions remain blocked")
    ).wait_for(state="visible", timeout=timeout_ms)
    checks["b2_unavailable_is_visible"] = True

    await page.get_by_role("button", name="Chat", exact=True).click()
    await chat_input.wait_for(state="visible", timeout=timeout_ms)
    rail = page.get_by_role("navigation", name="Chat and artifact navigation")
    await rail.hover()
    await page.get_by_text("PROJECTS", exact=True).first.wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_role("button", name="New Project", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    checks["project_navigation_rendered"] = True

    # The drawer is owned by hover or focus. Retain keyboard focus while its
    # contents change instead of moving the pointer across the resizing panel.
    await rail.focus()
    artifact_mode = rail.get_by_role("button", name="Artifacts", exact=True)
    await artifact_mode.click()
    from playwright.async_api import expect
    await expect(artifact_mode).to_have_attribute("aria-pressed", "true", timeout=timeout_ms)
    await rail.focus()
    await page.get_by_label("Artifact collections", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    await page.get_by_role(
        "button", name=re.compile(r"^Draft artifacts")
    ).click()
    await page.get_by_role("heading", name="Artifacts", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    files_tab = page.get_by_role("tab", name="Files · 0", exact=True)
    await files_tab.wait_for(state="visible", timeout=timeout_ms)
    if await files_tab.get_attribute("aria-selected") != "true":
        raise RuntimeError("Artifact navigation did not start with the file view")
    await page.get_by_text("No files yet", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    # Leave the hover/focus-owned history drawer before pointer navigation.
    await files_tab.focus()
    viewport = await page.evaluate(
        "() => ({ width: window.innerWidth, height: window.innerHeight })"
    )
    await page.mouse.move(max(300, int(viewport["width"]) - 20), 100)
    await page.get_by_role("tab", name="Interactive apps · 0", exact=True).click()
    await page.get_by_text("No interactive apps ready to open", exact=True).wait_for(
        state="visible", timeout=timeout_ms
    )
    checks["file_and_app_views_separate"] = True
    checks["artifact_navigation_opened"] = True

    back_to_chat = page.get_by_role("button", name="Back to chat", exact=True)
    await back_to_chat.focus()
    await page.mouse.move(
        max(300, int(viewport["width"]) - 20),
        max(20, int(viewport["height"]) // 2),
    )
    collapse_deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < collapse_deadline:
        if await rail.get_attribute("aria-expanded") == "false":
            break
        await asyncio.sleep(0.1)
    else:
        raise TimeoutError("Chat history rail did not close after pointer exit")

    await back_to_chat.click()
    await chat_input.wait_for(state="visible", timeout=timeout_ms)
    await page.wait_for_timeout(250)

    return checks


async def _wait_clean_exit(
    product: Any,
    *,
    backend_port: int,
    port_is_open: Any,
    timeout: float,
) -> None:
    if product.page is None or product.process is None:
        raise RuntimeError("Electron product did not expose its main window and process")

    process = product.process
    await product.page.close()
    try:
        await asyncio.to_thread(process.wait, max(1.0, timeout))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Electron did not exit after its main window closed") from exc
    if process.returncode != 0:
        raise RuntimeError(f"Electron exited with code {process.returncode}")

    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if not port_is_open(backend_port):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError("Electron exited but its owned backend port remained open")


async def run_smoke(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.extension_publication and (args.extension_fixture or args.extension_failures or args.native_confirmations):
        raise ValueError("--extension-publication is a separate complete journey")
    if args.native_confirmations and (not args.extension_fixture or args.extension_failures):
        raise ValueError("--native-confirmations requires --extension-fixture and excludes --extension-failures")
    if args.extension_failures and not args.extension_fixture:
        raise ValueError("--extension-failures requires --extension-fixture")
    from tools.e2e_live_product_journey import (
        BACKEND_PORT,
        ElectronProduct,
        WsProbe,
        _free_port,
        _port_is_open,
    )

    class ModelLessElectronProduct(ElectronProduct):
        """Use the current test interpreter as the explicitly selected backend."""

        def _environment(self) -> dict[str, str]:
            environment = model_less_backend_environment(
                super()._environment(), python_executable=sys.executable
            )
            if args.extension_fixture or args.extension_publication:
                environment.update(PI_PROVIDER_ENABLED="true", PI_MODEL_PROVIDER="fixture",
                    PI_MODEL="fixture", PI_AGENT_DIR=str(self.run_root / "state/pi"))
            if self.packaged_executable:
                # The current distribution packages the Electron frontend;
                # supply the separate Python runtime explicitly for this smoke.
                environment["PYTHONPATH"] = str(ROOT)
            return environment

    if (ROOT / ".env").exists():
        raise RuntimeError("Use an isolated checkout without .env for the main-inspector GUI smoke")
    started_at = datetime.now(timezone.utc)
    report_root = Path(args.report_dir).resolve()
    run_root = report_root / (
        f"electron_model_less_{started_at.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    )
    run_root.mkdir(parents=True, exist_ok=False)
    report_path = run_root / "report.json"
    product = ModelLessElectronProduct(
        run_root=run_root,
        debug_port=int(args.debug_port or _free_port()),
        main_debug_port=_free_port(),
        no_tts=True,
        identity=launcher_code_identity(ROOT),
        packaged_executable=args.packaged_executable.resolve() if args.packaged_executable else None,
    )
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "launcher_code_identity": product.identity,
        "frontend_build": _frontend_build_identity(args.packaged_executable.parent / "resources/app.asar" if args.packaged_executable else ROOT / "electron" / "dist"),
        "status": "failed",
        "started_at": started_at.isoformat(),
        "paths": {
            "run_root": str(run_root),
            "report": str(report_path),
            "process_log": str(product.log_path),
        },
        "checks": {},
    }

    if args.extension_fixture or args.extension_publication:
        # A synthetic pre-catalog v2 settings file, never a copied user profile.
        settings = run_root / "state/electron-user-data/settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        pi = run_root / "state/pi"
        pi.mkdir(parents=True)
        # Native Pi auth check is local/no-refresh. No model turn is sent; an
        # accidental request can reach only a closed loopback port, never a service.
        (pi / "models.json").write_text(json.dumps({"providers":{"fixture":{
            "baseUrl":"http://127.0.0.1:1/v1", "api":"openai-completions",
            "apiKey":"local-fixture", "models":[{"id":"fixture", "reasoning":False, "input":["text"]}]
        }}}), encoding="utf-8")
        settings.write_text(json.dumps({"version":2, "values":{"AMADEUS_UI_LOCALE":"en-US"},
            "encryptedSecrets":{}, "mcpConnections":{}, "extensions":{},
            "pendingRevisions":{}, "nextRevision":1}), encoding="utf-8")
    try:
        await product.start(startup_timeout=float(args.startup_timeout))
        report["checks"]["electron_started_backend"] = True

        async with WsProbe(
            f"ws://127.0.0.1:{BACKEND_PORT}/ws",
            subprotocols=product.backend_websocket_protocols,
        ) as probe:
            runtime = await probe.request("runtime.status", {}, timeout=20.0)
        if not isinstance(runtime.get("server"), dict):
            raise RuntimeError("authenticated runtime.status did not return server state")
        report["checks"]["authenticated_runtime_status"] = True

        def verify_wallpaper_stop() -> None:
            for headers, expected in (
                ({}, 401),
                ({"X-Amadeus-Token": product.backend_token, "Origin": "https://untrusted.invalid"}, 403),
                ({"X-Amadeus-Token": product.backend_token}, 200),
            ):
                connection = http.client.HTTPConnection("127.0.0.1", BACKEND_PORT, timeout=10)
                try:
                    connection.request("POST", "/wallpaper/stop", headers=headers)
                    response = connection.getresponse()
                    body = response.read()
                    if response.status != expected:
                        raise RuntimeError(f"wallpaper stop boundary returned {response.status}, expected {expected}")
                    if expected == 200 and json.loads(body).get("status") != "stopped":
                        raise RuntimeError("wallpaper stop did not acknowledge lifecycle cleanup")
                finally:
                    connection.close()

        await asyncio.to_thread(verify_wallpaper_stop)
        report["checks"]["authenticated_wallpaper_stop"] = True

        if product.page is None:
            raise RuntimeError("Electron renderer is unavailable")
        report["checks"].update(
            await _exercise_renderer(product.page, timeout=float(args.ui_timeout), process_id=product.process.pid, inspector_port=product.main_debug_port)
        )
        if args.extension_publication:
            from tools.probes.extension_store_roundtrip import exercise_publication
            report["publication"] = await exercise_publication(product, args.extension_publication.resolve())
        elif args.native_confirmations:
            from tools.probes.extension_native_confirmation import exercise_native
            report["native_confirmations"] = await exercise_native(product, args.extension_fixture.resolve())
        elif args.extension_fixture:
            from tools.probes.extension_release_shell import exercise
            report["extensions"] = await exercise(product, args.extension_fixture.resolve())
            if args.extension_failures:
                from tools.probes.extension_release_failures import exercise_failures
                report["extension_failures"] = await exercise_failures(product, args.extension_fixture.resolve())
        if product.app_page_errors or product.app_console_errors:
            raise RuntimeError(
                "Electron renderer reported errors: "
                + "; ".join(
                    [
                        *product.app_page_errors[:5],
                        *product.app_console_errors[:5],
                    ]
                )
            )

        await _wait_clean_exit(
            product,
            backend_port=BACKEND_PORT,
            port_is_open=_port_is_open,
            timeout=float(args.shutdown_timeout),
        )
        report["checks"]["electron_and_backend_exited"] = True
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["error_traceback"] = traceback.format_exc()
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["app_surface_diagnostics"] = product.app_diagnostics()
        await product.stop()
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return (0 if report["status"] == "passed" else 1), report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir",
        default=str(RUNTIME / "electron_model_less_smoke"),
    )
    parser.add_argument("--packaged-executable", type=Path, help="Launch the actual packaged frontend with this checkout supplying its external Python runtime")
    parser.add_argument("--native-confirmations", action="store_true", help="Wait for an external UI operator to cancel/install/enable through real native dialogs")
    parser.add_argument("--extension-failures", action="store_true", help="Run shipping IPC failure and cross-process corruption recovery cases; requires --extension-fixture")
    parser.add_argument("--extension-publication", type=Path, help="Isolated full publisher/client demonstration configuration; use extension_store_demo.py")
    parser.add_argument("--extension-fixture", type=Path, help="Explicitly execute reference wheels from a prepared isolated catalog fixture")
    parser.add_argument("--debug-port", type=int, default=0)
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--ui-timeout", type=float, default=30.0)
    parser.add_argument("--shutdown-timeout", type=float, default=20.0)
    return parser


def main() -> int:
    # The shared live-product launcher imports normal settings. Pin only this
    # command process before the lazy import in run_smoke; importing this module
    # from pytest remains side-effect free.
    os.environ["AMADEUS_E2E_NO_TTS"] = "1"
    os.environ["TTS_BACKEND"] = "disabled"
    os.environ["TTS_DEVICE"] = "cpu"
    os.environ["WAKE_ENABLED"] = "0"
    exit_code, report = asyncio.run(run_smoke(_parser().parse_args()))
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "report": report.get("paths", {}).get("report"),
                "error": report.get("error", ""),
                "checks": report.get("checks", {}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
