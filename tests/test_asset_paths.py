from __future__ import annotations

from config import asset_paths


def test_canonical_asset_paths_stay_under_asset_root() -> None:
    paths = (
        asset_paths.IMAGE_ROOT,
        asset_paths.AUDIO_ROOT,
        asset_paths.ICON_ROOT,
        asset_paths.MODEL_ROOT,
        asset_paths.PREVIEW_ROOT,
        asset_paths.SCENARIO_ROOT,
        asset_paths.SOURCE_ROOT,
        asset_paths.SPRITEFORGE_ROOT,
        asset_paths.SPRITEFORGE_RUNTIME_ROOT,
        asset_paths.PROJECT_ASSET_ROOT,
    )

    for path in paths:
        path.resolve().relative_to(asset_paths.ASSET_ROOT.resolve())


def test_required_git_owned_runtime_asset_roots_exist() -> None:
    required = (
        asset_paths.IMAGE_ROOT,
        asset_paths.AUDIO_ROOT / "sfx",
        asset_paths.ICON_ROOT / "app",
        asset_paths.ICON_ROOT / "ui",
    )

    assert all(path.exists() for path in required)
    assert (asset_paths.IMAGE_ROOT / "amadeus_desktop_wallpaper.png").is_file()
    assert (asset_paths.ICON_ROOT / "app" / "app_icon.ico").is_file()

    # Model directories are external-package destinations. They may be absent
    # from a clean public checkout until the user installs a model bundle.
    assert asset_paths.MODEL_ROOT.is_relative_to(asset_paths.ASSET_ROOT)
