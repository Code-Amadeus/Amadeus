from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "electron" / "src" / "main" / "index.ts"


def _main_source() -> str:
    return MAIN.read_text(encoding="utf-8")


def test_owned_renderer_shells_refuse_external_navigation() -> None:
    source = _main_source()

    assert (
        "const isDev = !app.isPackaged && process.env.NODE_ENV !== 'production'"
        in source
    )
    assert "function guardTrustedRendererShell(window: BrowserWindow)" in source
    assert "setWindowOpenHandler(() => ({ action: 'deny' }))" in source
    assert "window.webContents.on('will-navigate', guardNavigation)" in source
    assert "window.webContents.on('will-redirect', guardNavigation)" in source
    assert "guardTrustedRendererShell(mainWindow)" in source
    assert "guardTrustedRendererShell(workPanelWindow)" in source
    assert "guardTrustedRendererShell(workGlowWindow)" in source


def test_desktop_ipc_matches_the_renderer_that_actually_owns_each_surface() -> None:
    source = _main_source()

    assert "if (!isMainRenderer(event.sender)) return false" in source
    assert "if (!isPrimaryDesktopRenderer(event.sender)) return false" in source
    assert source.count("if (!isWorkPanelRenderer(event.sender)) return false") == 3
    assert source.count("if (!isPrimaryDesktopRenderer(event.sender)) {") == 2
