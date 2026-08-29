from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_ROOTS = (
    "agent_host",
    "asr",
    "config",
    "core",
    "llm",
    "openclaw",
    "render",
    "server",
    "tts",
    "vn_player",
    "vts",
    "wallpaper",
)
RETIRED_IMPORT_ROOTS = ("PyQt5", "qasync", "qfluentwidgets", "legacy.pyqt")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            values.add(node.module)
    return values


def test_supported_python_runtime_does_not_import_pyqt_or_legacy_surfaces() -> None:
    violations: list[str] = []
    for root_name in SUPPORTED_ROOTS:
        for path in (ROOT / root_name).rglob("*.py"):
            for imported in _imports(path):
                if imported.startswith(RETIRED_IMPORT_ROOTS):
                    violations.append(f"{path.relative_to(ROOT).as_posix()}: {imported}")
    assert violations == []


def test_retired_entries_are_isolated_from_the_repository_root() -> None:
    for relative in (
        "chatGui.py.deprecated",
        "render/engine.py",
        "run_spriteforge.py",
        "run_desktop_wallpaper.py",
        "run_wallpaper.py",
    ):
        assert not (ROOT / relative).exists(), relative

    for relative in (
        "legacy/pyqt/archive/chatGui.py.deprecated",
        "legacy/pyqt/render_engine.py",
        "legacy/pyqt/run_spriteforge.py",
        "legacy/pyqt/run_desktop_wallpaper.py",
        "legacy/pyqt/run_wallpaper.py",
        "legacy/pyqt/requirements.txt",
    ):
        assert (ROOT / relative).is_file(), relative


def test_product_dependencies_and_wallpaper_owner_stay_pyqt_free() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    handler = (ROOT / "server" / "handlers" / "wallpaper_handler.py").read_text(
        encoding="utf-8"
    )
    electron_main = (ROOT / "electron" / "src" / "main" / "index.ts").read_text(
        encoding="utf-8"
    )

    assert "legacy-pyqt" not in pyproject
    assert "PyQt5" not in pyproject
    assert "wallpaper.wallpaper_engine_bridge" in handler
    assert "windows_desktop_layer.py" in electron_main
