"""UI boundary checks for the minimal AUIP Experience projection."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "electron" / "src" / "renderer" / "App.tsx"
MAIN = ROOT / "electron" / "src" / "main" / "index.ts"
WORK_PAGE = ROOT / "electron" / "src" / "renderer" / "components" / "WorkPage.tsx"
WORK_PREVIEW_PAGE = (
    ROOT / "electron" / "src" / "renderer" / "components" / "WorkPreviewPage.tsx"
)
DRAWER = (
    ROOT
    / "electron"
    / "src"
    / "renderer"
    / "components"
    / "work"
    / "WorkDetailDrawer.tsx"
)
CARD = (
    ROOT
    / "electron"
    / "src"
    / "renderer"
    / "components"
    / "work"
    / "AuipExperienceCard.tsx"
)


def test_surface_tracks_only_the_launched_artifact_session() -> None:
    source = WORK_PAGE.read_text(encoding="utf-8")

    assert "send('auip.attach.prepare', { artifact_id: artifactId, mode })" in source
    assert "const artifactRef = String(prepared.artifact_ref || '')" in source
    assert "subscribe('auip.updated'" in source
    assert "String(payload.artifact_ref || '') !== previous.artifactRef" in source


def test_projection_excludes_raw_application_state() -> None:
    source = WORK_PAGE.read_text(encoding="utf-8")
    start = source.index("function projectAuipExperience(")
    end = source.index("function formatQuietSeconds", start)
    projection = source[start:end]

    assert "payload.state" not in projection
    assert "latest_verified_self_action" in projection
    assert "latest_delivered_narration" in projection
    assert "experience_capsule" in projection
    assert "event.type" in projection


def test_drawer_keeps_auip_as_a_status_surface_not_a_second_game_view() -> None:
    source = DRAWER.read_text(encoding="utf-8")

    assert 'type="attached-experience"' in source
    assert "Only host-accepted semantic facts appear here." in source
    assert "Connection lost" in source
    assert "Latest accepted action" in source
    assert "Terminal fact" in source
    assert "auipExperience.state" not in source
    assert "revision" not in source


def test_only_verified_auip_delivery_gets_launch_affordance() -> None:
    app = APP.read_text(encoding="utf-8")
    drawer = DRAWER.read_text(encoding="utf-8")

    assert "subscribe('auip.launch.requested'" in app
    assert "send('auip.launch.result'" in app
    assert "label: auipLaunchArtifactId === auipArtifactId" in drawer
    assert ": 'Open'" in drawer
    assert "onLaunchAuip(auipArtifactId, 'observe')" in drawer
    assert "Open as AUIP app" not in drawer


def test_external_app_uses_a_sandboxed_web_surface() -> None:
    source = MAIN.read_text(encoding="utf-8")
    start = source.index("ipcMain.handle('auip-app.open'")
    end = source.index("ipcMain.handle('work-overlay.open'", start)
    launch = source[start:end]

    assert "new BrowserWindow" in launch
    assert "contextIsolation: true" in launch
    assert "nodeIntegration: false" in launch
    assert "sandbox: true" in launch
    assert "webSecurity: true" in launch
    assert "setWindowOpenHandler(() => ({ action: 'deny' }))" in launch
    assert "restrictAuipContentNetwork(appWindow.webContents.session, policy)" in launch
    assert "partition: `auip-app-${workPreviewPartitionToken" in launch
    assert "await shell.openExternal" not in launch
    assert "ipcMain.handle('auip-app.close'" in launch
    assert "auipAppSurfacesById" in launch
    assert "appWindow.destroy()" in launch


def test_verified_auip_reuses_a_matching_work_preview_without_sharing_content() -> None:
    source = MAIN.read_text(encoding="utf-8")
    preload = (
        ROOT / "electron" / "src" / "preload" / "index.mts"
    ).read_text(encoding="utf-8")
    work_page = WORK_PAGE.read_text(encoding="utf-8")

    assert "openAuipInWorkPreview" in source
    assert "auip-work-preview-${partitionToken}" in source
    assert "Loading is presentation readiness only" in source
    assert "descriptor.revision <= pending.startRevision" in source
    assert "descriptor.attemptId === pending.attemptId" in source
    assert "descriptor.artifactRef === pending.artifactRef" in source
    assert "descriptor.hostSurfaceId === pending.hostSurfaceId" in source
    assert "Boolean(descriptor.appSessionId)" in source
    assert "if (pending.loaded) commitPendingAuip" in source
    assert "authoritativeLifecycle" not in source
    assert "publishWorkPreviewLifecycle" not in source
    assert "surface.auipView && resolvedDescriptor.lifecycle === 'frozen'" in source
    assert "releaseAttachedAuipView(surface)" in source
    handoff_start = source.index("async function openAuipInWorkPreview(")
    handoff_end = source.index("// IPC.", handoff_start)
    handoff = source[handoff_start:handoff_end]
    assert "surface.view.webContents.close" not in handoff
    assert "kind: 'work-preview'" in source
    assert "workItemId?: string" in preload
    assert "openAuipApp(launchUrl, hostSurfaceId, workItemId)" in work_page
    assert "The Host did not deliver the exact WorkItem App Surface." in source
    assert "exists only for legacy callers" in source


def test_work_preview_opens_at_a_compact_resizable_default_size() -> None:
    source = MAIN.read_text(encoding="utf-8")
    start = source.index("function createWorkPreviewSurface(")
    end = source.index("function closeAllWorkPreviewSurfaces(", start)
    preview = source[start:end]

    assert "width: 1024," in preview
    assert "height: 768," in preview
    assert "minWidth: 720," in preview
    assert "minHeight: 520," in preview
    assert "transparent: true," in preview
    assert "backgroundColor: '#00000000'," in preview


def test_work_preview_uses_smaller_controls_and_one_rounded_outer_shell() -> None:
    css = (ROOT / "electron" / "src" / "renderer" / "styles" / "workPreview.css").read_text(
        encoding="utf-8"
    )
    shell_start = css.index(".work-preview-shell {")
    shell_end = css.index(".work-preview-grid", shell_start)
    controls_start = css.index(".work-preview-window-actions button {")
    controls_end = css.index(".work-preview-window-actions button:hover", controls_start)
    frame_start = css.index(".work-preview-frame {")
    frame_end = css.index(".work-preview-viewport", frame_start)
    viewport_start = css.index(".work-preview-viewport {")
    viewport_end = css.index(".work-preview-placeholder", viewport_start)

    assert "border-radius: 28px;" in css[shell_start:shell_end]
    assert "width: 25px;" in css[controls_start:controls_end]
    assert "height: 23px;" in css[controls_start:controls_end]
    assert "border-radius: 7px;" in css[controls_start:controls_end]
    assert "border-radius: 0;" in css[frame_start:frame_end]
    assert "border-radius: 0;" in css[viewport_start:viewport_end]
    assert ".work-preview-window body" in css
    assert "background: transparent !important;" in css


def test_auip_content_has_a_closed_network_and_file_root() -> None:
    source = MAIN.read_text(encoding="utf-8")
    start = source.index("function parseAuipLaunchPolicy(")
    end = source.index("async function openAuipInWorkPreview(", start)
    policy = source[start:end]

    assert "pathIsWithin(policy.entryRoot, fileURLToPath(target))" in policy
    assert "target.protocol === 'data:' || target.protocol === 'blob:'" in policy
    assert "target.toString() === policy.webSocketUrl" in policy
    assert "webSocket.pathname !== '/auip/ws'" in policy
    assert "callback({ cancel: !isAllowedAuipResource(policy, details.url) })" in policy
    assert "target.protocol === 'http:'" not in policy


def test_preview_window_close_waits_for_exact_auip_leave_and_host_freeze() -> None:
    source = WORK_PREVIEW_PAGE.read_text(encoding="utf-8")

    assert "app_session_id: appSessionId" in source
    assert "String(left.host_surface_id || '') !== hostSurfaceId" in source
    assert "current.presentedAppSessionId" in source
    assert "current.presentedHostSurfaceId" in source
    assert "verifiedSameAttemptClose" in source
    assert "verifiedConflictClose" in source
    assert "The AppSession did not reach a verified frozen surface" in source

    main = MAIN.read_text(encoding="utf-8")
    close_start = main.index("ipcMain.handle('work-preview.close'")
    close_end = main.index("ipcMain.handle('work-preview.set-bounds'", close_start)
    close_handler = main[close_start:close_end]
    assert "surface.pendingAuip" in close_handler
    assert "surface.auipView" in close_handler
    assert "wait for its surface-close receipt" in close_handler
    assert "nativeCloseFallback" in main
    assert "forcing native close after acknowledgement timeout" in main


def test_trusted_main_surface_reports_auip_window_close_receipts() -> None:
    app = APP.read_text(encoding="utf-8")
    preload = (
        ROOT / "electron" / "src" / "preload" / "index.mts"
    ).read_text(encoding="utf-8")

    assert "subscribe('auip.surface.close.requested'" in app
    assert "send('auip.surface.close.result'" in app
    assert "closeAuipApp(hostSurfaceId, appSessionId)" in app
    assert "ipcRenderer.invoke('auip-app.close', hostSurfaceId, appSessionId)" in preload


def test_trusted_desktop_bridge_uses_the_supported_esm_preload_boundary() -> None:
    source = MAIN.read_text(encoding="utf-8")
    trusted = source[: source.index("ipcMain.handle('auip-app.open'")]

    assert "preload', 'index.mjs'" in trusted
    assert "sandbox: false" in trusted
    assert (ROOT / "electron" / "src" / "preload" / "index.mts").is_file()


def test_floating_card_is_a_bounded_experience_status_not_an_embedded_app() -> None:
    source = CARD.read_text(encoding="utf-8")
    work_page = WORK_PAGE.read_text(encoding="utf-8")

    assert "<AuipExperienceCard" in work_page
    assert "experience={auipExperience}" in work_page
    assert "ATTACHED EXPERIENCE" in source
    assert "Connected" in source
    assert "Spectating" in source
    assert "Participating" in source
    assert "Take one turn" in source
    assert "Leave experience" in source
    assert "Let Kurisu play" in source
    assert "raw state stays outside the conversation" in source
    assert "iframe" not in source.lower()
    assert "payload.state" not in source


if __name__ == "__main__":
    test_surface_tracks_only_the_launched_artifact_session()
    test_projection_excludes_raw_application_state()
    test_drawer_keeps_auip_as_a_status_surface_not_a_second_game_view()
    test_only_verified_auip_delivery_gets_launch_affordance()
    test_external_app_uses_a_sandboxed_web_surface()
    test_verified_auip_reuses_a_matching_work_preview_without_sharing_content()
    test_auip_content_has_a_closed_network_and_file_root()
    test_preview_window_close_waits_for_exact_auip_leave_and_host_freeze()
    test_trusted_main_surface_reports_auip_window_close_receipts()
    test_trusted_desktop_bridge_uses_the_supported_esm_preload_boundary()
    test_floating_card_is_a_bounded_experience_status_not_an_embedded_app()
    print("ok: AUIP Slice surface remains a bounded host projection")
