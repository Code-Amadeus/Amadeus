"""Prove that a live Work Preview becomes an AUIP surface in-place.

This journey starts the shipping Electron main process and Python backend with
an isolated Work Ledger.  One running Attempt first owns an ordinary static web
preview.  The same Attempt then gains the Host-authored AUIP metadata and a
validated reactor bundle, so the preview enters ``assembling``.  Finally the
trusted renderer prepares Attach and opens the returned launch descriptor.

The strongest assertion is native-window identity: the OS reports the same
top-level Electron window before and after Attach.  The old Preview child stays
alive but detached so a later Work Attempt can thaw it; a standalone AUIP
``BrowserWindow`` would add a second native window.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import shutil
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.provider_authoring import materialize_auip_runtime_assets
from agent_host.work_ledger_store import WorkLedgerStore
from server.auip_bundle_validation import validate_staged_auip_web_bundle
from server.work_ledger_coordinator import WorkLedgerCoordinator
from tools.e2e_live_product_journey import (
    BACKEND_PORT,
    ElectronProduct,
    WsProbe,
    _free_port,
)
from tools.semantic_journey_evidence import code_identity


RUNTIME = ROOT / "runtime" / "work_preview_journeys"
REACTOR = ROOT / "examples" / "auip-reactor"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed(run_root: Path) -> tuple[str, Path]:
    state = run_root / "state"
    workspace = state / "scratch" / "preview-to-auip"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "index.html").write_text(
        """<!doctype html>
<html><head><meta charset="utf-8"><title>Work in progress</title></head>
<body><main id="phase">ORDINARY LIVE PREVIEW</main></body></html>
""",
        encoding="utf-8",
    )
    ledger_path = state / "work_ledger.sqlite3"
    with WorkLedgerStore(ledger_path) as store:
        project = store.create_or_get_project(workspace, name="Preview AUIP handoff")
        item = store.create_work_item(
            project.project_id,
            title="Preview to AUIP handoff",
            workspace_path=workspace,
        )
        startup_attempt = store.create_attempt(
            item.work_item_id,
            provider="fixture",
            task="Seed a stable WorkItem before Electron startup",
        )
        # Startup reconciliation retires active Attempts without a live
        # Provider.  The actual journey Attempt is created after startup.
        store.update_attempt(startup_attempt.attempt_id, execution_status="succeeded")
        WorkLedgerCoordinator(store).select(item.work_item_id)
        return item.work_item_id, workspace


def _materialize_reactor(workspace: Path) -> dict[str, dict[str, str]]:
    """Install the real sample and retarget its SDK refs to app-owned files."""

    for name in ("index.html", "simulation.js", "auip.manifest.json"):
        shutil.copy2(REACTOR / name, workspace / name)
    assets = materialize_auip_runtime_assets(workspace)
    entry = workspace / "index.html"
    html = entry.read_text(encoding="utf-8")
    for relative_name in assets:
        basename = Path(relative_name).name
        html = html.replace(
            f'../../sdk/auip-core/{basename}',
            f'./sdk/auip-core/{basename}',
        ).replace(
            f'../../sdk/auip-web/{basename}',
            f'./sdk/auip-web/{basename}',
        )
    entry.write_text(html, encoding="utf-8")
    validate_staged_auip_web_bundle(
        workspace,
        entry_filename="index.html",
        materialized_files=tuple(sorted(assets)),
    )
    return assets


def _register_bundle(
    ledger_path: Path,
    *,
    work_item_id: str,
    attempt_id: str,
    workspace: Path,
    assets: dict[str, dict[str, str]],
) -> str:
    with WorkLedgerStore(ledger_path) as store:
        store.update_attempt(
            attempt_id,
            metadata={
                "auip_authoring_skill_path": str(
                    ROOT / "skills" / "auip-authoring" / "SKILL.md"
                ),
                "auip_authoring_bundle_mode": "lean_host_managed",
                "auip_bundle_root": str(workspace),
                "auip_host_validates_bundle": True,
                "auip_host_materialized_files": sorted(assets),
            },
        )
        entry = workspace / "index.html"
        manifest = workspace / "auip.manifest.json"
        entry_record = store.register_artifact(
            work_item_id,
            attempt_id=attempt_id,
            kind="business.file",
            title="Reactor Drift",
            path=entry,
            status="registered",
            sha256=_sha256(entry),
            size_bytes=entry.stat().st_size,
            metadata={"e2e": "preview_auip_handoff"},
        )
        store.register_artifact(
            work_item_id,
            attempt_id=attempt_id,
            kind="business.file",
            title="AUIP manifest",
            path=manifest,
            status="registered",
            sha256=_sha256(manifest),
            size_bytes=manifest.stat().st_size,
            metadata={"e2e": "preview_auip_handoff"},
        )
        return entry_record.artifact_id


async def _find_page(product: ElectronProduct, predicate, *, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    observed: list[str] = []
    while time.monotonic() < deadline:
        pages = await product.app_pages()
        observed = [str(page.url) for page in pages]
        for page in pages:
            if predicate(str(page.url)):
                return page
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Electron page did not appear; observed={observed!r}")


async def _wait_for_page_absence(
    product: ElectronProduct,
    predicate,
    *,
    timeout: float = 12.0,
) -> list[str]:
    deadline = time.monotonic() + timeout
    observed: list[str] = []
    while time.monotonic() < deadline:
        pages = await product.app_pages()
        observed = [str(page.url) for page in pages]
        if not any(predicate(url) for url in observed):
            return observed
        await asyncio.sleep(0.1)
    raise AssertionError(f"obsolete Electron target remained alive: {observed!r}")


async def _wait_locator_text(
    page: Any,
    selector: str,
    expected: str,
    *,
    timeout: float = 10.0,
) -> None:
    """Wait without eval so the trusted shell's CSP remains fully enforced."""

    deadline = time.monotonic() + timeout
    observed = ""
    locator = page.locator(selector)
    while time.monotonic() < deadline:
        try:
            observed = str(await locator.text_content(timeout=500) or "").strip()
        except Exception:
            observed = ""
        if observed == expected:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"{selector} did not become {expected!r}; observed={observed!r}"
    )


def _electron_windows(process_id: int) -> list[dict[str, Any]]:
    """Enumerate visible native windows owned by the Electron browser process."""

    if sys.platform != "win32":
        raise RuntimeError("this real Electron journey currently requires Windows")
    user32 = ctypes.windll.user32
    windows: list[dict[str, Any]] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    def visit(hwnd: int, _lparam: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) != int(process_id) or not user32.IsWindowVisible(hwnd):
            return True
        title_length = int(user32.GetWindowTextLengthW(hwnd))
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        windows.append(
            {
                "handle": int(hwnd),
                "title": title_buffer.value,
                "class": class_buffer.value,
                "bounds": [
                    int(rect.left),
                    int(rect.top),
                    int(rect.right - rect.left),
                    int(rect.bottom - rect.top),
                ],
            }
        )
        return True

    callback = callback_type(visit)
    if not user32.EnumWindows(callback, 0):
        raise ctypes.WinError()
    return sorted(windows, key=lambda value: int(value["handle"]))


def _native_app_surface_windows(
    windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep content-bearing Electron windows, excluding transient widgets.

    Chromium creates small visible ``Chrome_WidgetWin_1`` windows for tooltips,
    IME helpers, and other transient UI.  They share the browser process but
    cannot host either the Work Preview or an AUIP application, so counting
    them as product surfaces makes the native-window identity assertion race
    with incidental renderer UI.
    """

    surfaces: list[dict[str, Any]] = []
    for window in windows:
        bounds = window.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4:
            continue
        width = int(bounds[2] or 0)
        height = int(bounds[3] or 0)
        # The smallest product BrowserWindow is the standalone AUIP surface
        # (minWidth=480, minHeight=360). Anything smaller cannot be one of the
        # native application surfaces whose identity this Journey verifies.
        if width >= 480 and height >= 360:
            surfaces.append(window)
    return surfaces


async def _wait_named_window(
    product: ElectronProduct,
    title: str,
    *,
    timeout: float = 10.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    assert product.process is not None
    deadline = time.monotonic() + timeout
    observed: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        observed = _native_app_surface_windows(
            _electron_windows(product.process.pid)
        )
        matching = [window for window in observed if window["title"] == title]
        if len(matching) == 1:
            return matching[0], observed
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"expected one visible Electron window titled {title!r}; observed={observed!r}"
    )


async def run(
    *,
    implicit_surface: bool = False,
    shell_close: bool = False,
) -> dict[str, Any]:
    if implicit_surface and shell_close:
        raise ValueError("implicit_surface and shell_close are mutually exclusive")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = "shell-close" if shell_close else "implicit" if implicit_surface else "explicit"
    run_root = RUNTIME / f"work_preview_auip_handoff_{mode}_{stamp}"
    run_root.mkdir(parents=True, exist_ok=False)
    work_item_id, workspace = _seed(run_root)
    ledger_path = run_root / "state" / "work_ledger.sqlite3"
    product = ElectronProduct(
        run_root=run_root,
        debug_port=_free_port(),
        no_tts=True,
        identity=code_identity(ROOT),
    )
    report: dict[str, Any] = {
        "schema": "amadeus.work-preview-auip-handoff.v1",
        "run_root": str(run_root),
        "work_item_id": work_item_id,
        "surface_mode": mode,
        "checks": {},
    }
    try:
        await product.start(startup_timeout=120.0)
        async with WsProbe(
            f"ws://127.0.0.1:{BACKEND_PORT}/ws",
            subprotocols=product.backend_websocket_protocols,
        ) as probe:
            created_session = await probe.request(
                "session.create",
                {"title": "Work Preview AUIP handoff E2E"},
            )
            session_id = str(created_session.get("current_session_id") or "")
            assert session_id
            with WorkLedgerStore(ledger_path) as store:
                store.set_session_active_work_item(
                    session_id,
                    work_item_id,
                    metadata={"source": "preview_auip_handoff_e2e"},
                )
                attempt = store.create_attempt(
                    work_item_id,
                    provider="e2e",
                    task="Turn an ordinary Preview into an attached AUIP surface",
                    metadata={"session_id": session_id},
                )
                store.update_attempt(attempt.attempt_id, execution_status="running")
            attempt_id = attempt.attempt_id
            report["attempt_id"] = attempt_id
            report["session_id"] = session_id

            listing = await probe.request("work.list", {})
            work = listing.get("work") if isinstance(listing.get("work"), dict) else {}
            assert work.get("selectedWorkItemId") == work_item_id
            screenshots = run_root / "screenshots"
            screenshots.mkdir(parents=True, exist_ok=True)
            preview_id = ""
            preview_url = ""
            shell = None
            windows_before: list[dict[str, Any]] = []
            preview_window_handle = 0
            pages_before: list[str] = []
            baseline_windows: list[dict[str, Any]] = []

            if implicit_surface:
                closed = await probe.request(
                    "work.preview.get",
                    {"work_item_id": work_item_id},
                )
                assert (closed.get("preview") or {}).get("status") == "closed"
                assert not any(
                    "previewWindow=1" in str(page.url)
                    for page in await product.app_pages()
                )
                assert not any(
                    event.method == "work.preview.open.requested"
                    for event in probe.state.events
                )
                assert product.process is not None
                baseline_windows = _native_app_surface_windows(
                    _electron_windows(product.process.pid)
                )
                report["checks"]["implicit_surface_started_closed"] = True
                report["checks"]["no_explicit_preview_open_was_sent"] = True
                assets = _materialize_reactor(workspace)
                artifact_id = _register_bundle(
                    ledger_path,
                    work_item_id=work_item_id,
                    attempt_id=attempt_id,
                    workspace=workspace,
                    assets=assets,
                )
            else:
                opened = await probe.request(
                    "work.preview.open",
                    {
                        "work_item_id": work_item_id,
                        "attempt_id": attempt_id,
                        "revision": work.get("revision"),
                    },
                )
                preview = (
                    opened.get("preview")
                    if isinstance(opened.get("preview"), dict)
                    else {}
                )
                assert opened.get("ok") is True
                assert preview.get("status") == "ready"
                assert preview.get("lifecycle") == "live"
                preview_id = str(preview.get("previewId") or "")
                preview_url = str(preview.get("url") or "")
                assert preview_id and preview_url

                shell = await _find_page(
                    product,
                    lambda url: "previewWindow=1" in url,
                )
                preview_content = await _find_page(
                    product,
                    lambda url: url == preview_url,
                )
                await preview_content.locator("#phase").wait_for(
                    state="visible",
                    timeout=10_000,
                )
                assert (
                    await preview_content.locator("#phase").inner_text()
                    == "ORDINARY LIVE PREVIEW"
                )
                preview_window, windows_before = await _wait_named_window(
                    product,
                    "Preview to AUIP handoff",
                )
                preview_window_handle = int(preview_window["handle"])
                pages_before = [str(page.url) for page in await product.app_pages()]
                report["checks"]["ordinary_live_preview_opened"] = True
                report["checks"]["preview_shell_has_one_native_window"] = True

                assembling_start = len(probe.state.events)
                assets = _materialize_reactor(workspace)
                artifact_id = _register_bundle(
                    ledger_path,
                    work_item_id=work_item_id,
                    attempt_id=attempt_id,
                    workspace=workspace,
                    assets=assets,
                )
                assembling_event = await probe.wait_event(
                    lambda event: (
                        event.method == "work.preview.updated"
                        and str(
                            (event.params.get("preview") or {}).get("previewId")
                            or ""
                        )
                        == preview_id
                        and str(
                            (event.params.get("preview") or {}).get("lifecycle")
                            or ""
                        )
                        == "assembling"
                    ),
                    timeout=15.0,
                    after=assembling_start,
                    description="authoritative AUIP assembling lifecycle",
                )
                assembling_preview = assembling_event.params.get("preview") or {}
                assert assembling_preview.get("url") == ""
                await _wait_locator_text(
                    shell,
                    ".work-preview-load-label",
                    "ASSEMBLING",
                )
                assert (
                    await shell.locator(".work-preview-auip-stage strong").inner_text()
                    == "AUIP ASSEMBLING"
                )
                await shell.screenshot(path=str(screenshots / "assembling.png"))
                report["checks"]["same_attempt_entered_assembling"] = True
                report["checks"]["assembling_released_preview_server"] = True

            handoff_start = len(probe.state.events)
            prepared = await probe.request(
                "auip.attach.prepare",
                {"artifact_id": artifact_id, "mode": "observe"},
                timeout=30.0,
            )
            assert prepared.get("ok") is True, prepared
            assert prepared.get("work_item_id") == work_item_id
            assert prepared.get("attempt_id") == attempt_id
            launch_url = str(prepared.get("launch_url") or "")
            host_surface_id = str(prepared.get("host_surface_id") or "")
            artifact_ref = str(prepared.get("artifact_ref") or "")
            assert launch_url.startswith("file:") and host_surface_id and artifact_ref
            if implicit_surface:
                open_event = await probe.wait_event(
                    lambda event: event.method == "work.preview.open.requested",
                    timeout=10.0,
                    after=handoff_start,
                    description="Attach-first Host App Surface open request",
                )
                auto_preview = open_event.params.get("preview") or {}
                preview_id = str(auto_preview.get("previewId") or "")
                assert preview_id
                assert auto_preview.get("workItemId") == work_item_id
                assert auto_preview.get("attemptId") == attempt_id
                assert auto_preview.get("lifecycle") == "assembling"
                assert auto_preview.get("url") == ""

            handoff_event = await probe.wait_event(
                lambda event: (
                    event.method == "work.preview.updated"
                    and str((event.params.get("preview") or {}).get("previewId") or "")
                    == preview_id
                    and str((event.params.get("preview") or {}).get("lifecycle") or "")
                    == "handoff"
                ),
                timeout=10.0,
                after=handoff_start,
                description="prepared AUIP handoff lifecycle",
            )
            handoff_preview = handoff_event.params.get("preview") or {}
            assert handoff_preview.get("hostSurfaceId") == host_surface_id
            assert handoff_preview.get("artifactRef") == artifact_ref

            if implicit_surface:
                shell = await _find_page(
                    product,
                    lambda url: (
                        "previewWindow=1" in url and f"previewId={preview_id}" in url
                    ),
                )
                await _wait_locator_text(
                    shell,
                    ".work-preview-load-label",
                    "ATTACHING",
                )
                preview_window, windows_before = await _wait_named_window(
                    product,
                    "Preview to AUIP handoff",
                )
                preview_window_handle = int(preview_window["handle"])
                baseline_handles = {
                    int(window["handle"]) for window in baseline_windows
                }
                prepared_handles = {
                    int(window["handle"]) for window in windows_before
                }
                assert baseline_handles < prepared_handles
                assert prepared_handles - baseline_handles == {preview_window_handle}
                pages_before = [str(page.url) for page in await product.app_pages()]
                await shell.screenshot(path=str(screenshots / "implicit-handoff.png"))
                report["checks"]["attach_prepare_created_shared_app_surface"] = True
                report["checks"]["implicit_surface_opened_in_assembling"] = True
                report["checks"]["implicit_surface_reached_handoff"] = True
                report["checks"]["only_one_expected_native_surface_was_added"] = True
            report["checks"]["attach_prepare_bound_exact_work_attempt"] = True

            assert shell is not None
            assert product.page is not None
            opened_app = await product.page.evaluate(
                """async ({launchUrl, hostSurfaceId, workItemId}) => {
                  if (!window.amadeus?.openAuipApp) {
                    return {ok: false, detail: 'trusted AUIP bridge missing'}
                  }
                  return await window.amadeus.openAuipApp(
                    launchUrl, hostSurfaceId, workItemId
                  )
                }""",
                {
                    "launchUrl": launch_url,
                    "hostSurfaceId": host_surface_id,
                    "workItemId": work_item_id,
                },
            )
            assert opened_app.get("ok") is True, opened_app

            active_event = await probe.wait_event(
                lambda event: (
                    event.method == "auip.updated"
                    and str(event.params.get("status") or "") == "active"
                    and str(event.params.get("artifact_ref") or "") == artifact_ref
                ),
                timeout=20.0,
                after=handoff_start,
                description="AUIP app registered active",
            )
            app_session_id = str(active_event.params.get("app_session_id") or "")
            assert app_session_id
            await _wait_locator_text(
                shell,
                ".work-preview-load-label",
                "AUIP ACTIVE",
            )
            auip_page = await _find_page(
                product,
                # The SDK consumes and removes the one-time hash descriptor
                # immediately after reading it, so the live target exposes
                # only the verified file URL by the time CDP observes it.
                lambda url: (
                    url.startswith("file:")
                    and "/preview-to-auip/index.html" in url.replace("\\", "/")
                ),
            )
            await auip_page.wait_for_function(
                "window.__auipReactor?.isAttached?.() === true",
                timeout=15_000,
            )
            assert await auip_page.locator("#connection").inner_text() == "Attached to Amadeus"

            await asyncio.sleep(0.5)
            windows_after = _native_app_surface_windows(
                _electron_windows(product.process.pid)
            )
            assert {
                int(window["handle"]) for window in windows_after
            } == {
                int(window["handle"]) for window in windows_before
            }, {"before": windows_before, "after": windows_after}
            preview_windows_after = [
                window
                for window in windows_after
                if int(window["handle"]) == preview_window_handle
            ]
            assert len(preview_windows_after) == 1
            assert preview_windows_after[0]["title"] == "Preview to AUIP handoff"
            pages_after = [str(page.url) for page in await product.app_pages()]
            if preview_url:
                assert preview_url in pages_after
            else:
                assert not any(
                    url.startswith("http://127.0.0.1:") for url in pages_after
                )
            assert sum(
                1
                for url in pages_after
                if url.startswith("file:")
                and "/preview-to-auip/index.html" in url.replace("\\", "/")
            ) == 1
            assert not shell.is_closed()
            attached_host = await probe.request(
                "work.preview.get",
                {"work_item_id": work_item_id},
            )
            attached_preview = attached_host.get("preview") or {}
            assert attached_preview.get("previewId") == preview_id
            assert attached_preview.get("lifecycle") == "attached"
            await shell.screenshot(path=str(screenshots / "attached-shell.png"))
            await auip_page.screenshot(path=str(screenshots / "attached-reactor.png"))

            report["checks"]["auip_registered_active"] = True
            report["checks"]["preview_shell_identity_preserved"] = True
            report["checks"]["auip_child_shared_native_window"] = True
            report["checks"]["preview_child_retained_for_thaw"] = True
            report["checks"]["no_standalone_auip_window"] = True
            report["checks"]["host_committed_attached_lifecycle"] = True

            if shell_close:
                close_start = len(probe.state.events)
                await shell.locator('button[aria-label="Close preview"]').click()
                close_requested = await probe.wait_event(
                    lambda event: (
                        event.method == "auip.surface.close.requested"
                        and str(event.params.get("app_session_id") or "")
                        == app_session_id
                        and str(event.params.get("host_surface_id") or "")
                        == host_surface_id
                    ),
                    timeout=15.0,
                    after=close_start,
                    description="shell close requested exact AUIP surface closure",
                )
                assert close_requested.params["app_session_id"] == app_session_id

                pending_close = await probe.wait_event(
                    lambda event: (
                        event.method == "auip.updated"
                        and str(event.params.get("app_session_id") or "")
                        == app_session_id
                        and str(event.params.get("host_surface_id") or "")
                        == host_surface_id
                        and str(event.params.get("artifact_ref") or "")
                        == artifact_ref
                        and str(event.params.get("status") or "") == "closed"
                        and str(event.params.get("surface_close_status") or "")
                        == "pending"
                    ),
                    timeout=15.0,
                    after=close_start,
                    description="shell close exact leave entered pending receipt",
                )
                pending_capsule = pending_close.params.get("experience_capsule") or {}
                assert pending_capsule.get("close_reason") == "app_surface_window_closed"

                closed_update = await probe.wait_event(
                    lambda event: (
                        event.method == "auip.updated"
                        and str(event.params.get("app_session_id") or "")
                        == app_session_id
                        and str(event.params.get("host_surface_id") or "")
                        == host_surface_id
                        and str(event.params.get("artifact_ref") or "")
                        == artifact_ref
                        and str(event.params.get("status") or "") == "closed"
                        and str(event.params.get("surface_close_status") or "")
                        == "closed"
                    ),
                    timeout=15.0,
                    after=close_start,
                    description="shell close exact Electron surface receipt",
                )
                closed_capsule = closed_update.params.get("experience_capsule") or {}
                assert closed_capsule.get("close_reason") == "app_surface_window_closed"

                await probe.wait_event(
                    lambda event: (
                        event.method == "work.preview.updated"
                        and str(
                            (event.params.get("preview") or {}).get("previewId")
                            or ""
                        )
                        == preview_id
                        and str(
                            (event.params.get("preview") or {}).get("status") or ""
                        )
                        == "closed"
                    ),
                    timeout=15.0,
                    after=close_start,
                    description="shell close reclaimed Host Work Preview",
                )
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and not shell.is_closed():
                    await asyncio.sleep(0.1)
                assert shell.is_closed()
                await _wait_for_page_absence(
                    product,
                    lambda url: (
                        url.startswith("file:")
                        and "/preview-to-auip/index.html" in url.replace("\\", "/")
                    ),
                )
                host_closed = await probe.request(
                    "work.preview.get",
                    {"work_item_id": work_item_id},
                )
                assert (host_closed.get("preview") or {}).get("status") == "closed"
                app_closed = await probe.request(
                    "auip.session.get",
                    {"app_session_id": app_session_id},
                )
                assert app_closed.get("status") == "closed"
                assert app_closed.get("surface_close_status") == "closed"
                assert app_closed.get("host_surface_id") == host_surface_id
                assert app_closed.get("artifact_ref") == artifact_ref
                assert (
                    (app_closed.get("experience_capsule") or {}).get("close_reason")
                    == "app_surface_window_closed"
                )
                assert product.process is not None
                final_windows = _native_app_surface_windows(
                    _electron_windows(product.process.pid)
                )
                assert {
                    int(window["handle"]) for window in final_windows
                } == {
                    int(window["handle"])
                    for window in windows_before
                    if int(window["handle"]) != preview_window_handle
                }
                report["checks"]["shell_close_sent_exact_auip_leave"] = True
                report["checks"]["shell_close_received_exact_surface_receipt"] = True
                report["checks"]["shell_close_reclaimed_host_preview"] = True
                report["checks"]["shell_window_really_closed"] = True
                report["native_windows_after_shell_close"] = final_windows
            else:
                close_start = len(probe.state.events)
                left = await probe.request(
                    "auip.leave",
                    {
                        "app_session_id": app_session_id,
                        "reason": "work_preview_handoff_e2e_thaw",
                    },
                )
                assert left.get("status") == "closed"
                frozen_event = await probe.wait_event(
                    lambda event: (
                        event.method == "work.preview.updated"
                        and str(
                            (event.params.get("preview") or {}).get("previewId")
                            or ""
                        )
                        == preview_id
                        and str(
                            (event.params.get("preview") or {}).get("lifecycle")
                            or ""
                        )
                        == "frozen"
                    ),
                    timeout=15.0,
                    after=close_start,
                    description="exact AUIP surface receipt froze App Surface",
                )
                assert str(
                    (frozen_event.params.get("preview") or {}).get("appSessionId")
                    or ""
                ) == app_session_id
                await _wait_for_page_absence(
                    product,
                    lambda url: (
                        url.startswith("file:")
                        and "/preview-to-auip/index.html" in url.replace("\\", "/")
                    ),
                )
                report["checks"]["exact_leave_closed_auip_child"] = True
                report["checks"]["host_froze_after_surface_receipt"] = True

                thaw_start = len(probe.state.events)
                with WorkLedgerStore(ledger_path) as store:
                    store.update_attempt(attempt_id, execution_status="succeeded")
                    next_attempt = store.create_attempt(
                        work_item_id,
                        provider="e2e",
                        task="Resume the retained Preview after AppSession close",
                        metadata={"session_id": session_id},
                    )
                    store.update_attempt(
                        next_attempt.attempt_id,
                        execution_status="running",
                    )
                thawed_event = await probe.wait_event(
                    lambda event: (
                        event.method == "work.preview.updated"
                        and str(
                            (event.params.get("preview") or {}).get("previewId")
                            or ""
                        )
                        == preview_id
                        and str(
                            (event.params.get("preview") or {}).get("attemptId")
                            or ""
                        )
                        == next_attempt.attempt_id
                        and str(
                            (event.params.get("preview") or {}).get("lifecycle")
                            or ""
                        )
                        == "live"
                        and bool((event.params.get("preview") or {}).get("url"))
                    ),
                    timeout=15.0,
                    after=thaw_start,
                    description="new Work Attempt thawed retained Preview child",
                )
                thawed_preview = thawed_event.params.get("preview") or {}
                thawed_url = str(thawed_preview.get("url") or "")
                thawed_page = await _find_page(product, lambda url: url == thawed_url)
                await thawed_page.locator("#connection").wait_for(
                    state="visible",
                    timeout=10_000,
                )
                assert product.process is not None
                assert any(
                    int(window["handle"]) == preview_window_handle
                    for window in _electron_windows(product.process.pid)
                )
                await _wait_locator_text(
                    shell,
                    ".work-preview-load-label",
                    "LIVE",
                )
                report["checks"]["new_attempt_thawed_same_preview_child"] = True
                report["checks"]["thawed_preview_kept_native_window"] = True

            report["artifact_id"] = artifact_id
            report["artifact_ref"] = artifact_ref
            report["app_session_id"] = app_session_id
            report["preview_window_handle"] = preview_window_handle
            if implicit_surface:
                report["native_windows_before_implicit_surface"] = baseline_windows
            report["native_windows_before"] = windows_before
            report["native_windows_after"] = windows_after
            report["pages_before"] = pages_before
            report["pages_after"] = pages_after
            report["events"] = [event.to_dict() for event in probe.state.events]
            report["app_diagnostics"] = product.app_diagnostics()
    finally:
        await product.stop()

    diagnostics = report.get("app_diagnostics") or {}
    report["passed"] = all(report["checks"].values()) and not any(diagnostics.values())
    report_path = run_root / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--implicit-surface",
        action="store_true",
        help=(
            "prepare AUIP Attach before any explicit Work Preview open and "
            "require the Host to create the shared App Surface"
        ),
    )
    mode.add_argument(
        "--shell-close",
        action="store_true",
        help=(
            "after explicit Attach, close through the user-facing Preview "
            "shell instead of running the leave-and-thaw tail"
        ),
    )
    args = parser.parse_args()
    report = asyncio.run(
        run(
            implicit_surface=bool(args.implicit_surface),
            shell_close=bool(args.shell_close),
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
