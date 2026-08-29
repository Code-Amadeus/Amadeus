"""Authoritative filesystem locations for repository-owned assets."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = PROJECT_ROOT / "assets"
IMAGE_ROOT = ASSET_ROOT / "images"
AUDIO_ROOT = ASSET_ROOT / "audio"
ICON_ROOT = ASSET_ROOT / "icons"
MODEL_ROOT = ASSET_ROOT / "models"
PREVIEW_ROOT = ASSET_ROOT / "previews"
SCENARIO_ROOT = ASSET_ROOT / "scenarios"
SOURCE_ROOT = ASSET_ROOT / "source"

SPRITEFORGE_ROOT = ASSET_ROOT / "spriteforge"
SPRITEFORGE_RUNTIME_ROOT = SPRITEFORGE_ROOT / "runtime" / "kurisu"
SPRITEFORGE_RUNTIME_MANIFEST = SPRITEFORGE_RUNTIME_ROOT / "runtime_manifest.json"
SPRITEFORGE_GRAPH_CONFIG = SPRITEFORGE_RUNTIME_ROOT / "graph_config.json"
SPRITEFORGE_MOUTH_CONFIG = SPRITEFORGE_RUNTIME_ROOT / "spriteforge_mouth_config.json"
PROJECT_ASSET_ROOT = SOURCE_ROOT / "project-asset"
SCENARIO_SOURCE_ROOT = PROJECT_ASSET_ROOT / "scenarios"
SCENARIO_RUNTIME_ROOT = SCENARIO_ROOT / "runtime"
