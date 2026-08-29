"""Deterministic Host validation for a staged AUIP Web application bundle."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from agent_host.provider_authoring import official_auip_runtime_assets
from server.auip_contract import AuipProtocolError
from tools.sync_auip_manifest import sync_manifest
from tools.validate_auip_manifest import validate_file


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if str(tag or "").casefold() != "script":
            return
        values = {str(key or "").casefold(): str(value or "") for key, value in attrs}
        if values.get("src"):
            self.sources.append(values["src"])


def finalize_staged_auip_web_bundle(
    root: Path,
    *,
    entry_filename: str = "",
    materialized_files: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Apply Host-owned generated synchronization, then validate the bundle."""

    bundle_root = Path(root).resolve()
    manifest_path = (bundle_root / "auip.manifest.json").resolve()
    entry_path = _entry_path(bundle_root, entry_filename)
    sync_manifest(manifest_path, entry_path)
    result = validate_staged_auip_web_bundle(
        bundle_root,
        entry_filename=entry_path.name,
        materialized_files=materialized_files,
    )
    return {
        **result,
        "generated_steps": ["embedded_manifest_sync"],
    }


def validate_staged_auip_web_bundle(
    root: Path,
    *,
    entry_filename: str = "",
    materialized_files: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Validate protocol packaging without trusting Provider prose or tools."""

    bundle_root = Path(root).resolve()
    if not bundle_root.is_dir() or bundle_root.is_symlink():
        raise AuipProtocolError("auip_bundle_missing", str(bundle_root))
    manifest_path = (bundle_root / "auip.manifest.json").resolve()
    if manifest_path.parent != bundle_root or not manifest_path.is_file():
        raise AuipProtocolError("auip_manifest_missing", str(manifest_path))
    canonical = validate_file(manifest_path)
    entry_path = _entry_path(bundle_root, entry_filename)
    try:
        html = entry_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AuipProtocolError("auip_entry_unreadable", str(exc)) from exc
    if ".amadeus/runtime/authoring_inputs" in html.replace("\\", "/").casefold():
        raise AuipProtocolError("artifact_depends_on_private_authoring_input")

    sync_manifest(manifest_path, entry_path, check=True)
    sources = _script_sources(html)
    managed_index = _source_index(sources, "managed-v0.js")
    web_index = _source_index(sources, "auip-v0.js")
    if managed_index < 0 or web_index < 0:
        raise AuipProtocolError("auip_runtime_asset_not_referenced")
    if managed_index >= web_index:
        raise AuipProtocolError("auip_runtime_asset_order_invalid")
    if canonical.get("controller") is not None:
        controller_index = _source_index(sources, "controller-v0.js")
        if controller_index < 0:
            raise AuipProtocolError("auip_controller_asset_not_referenced")

    official = official_auip_runtime_assets()
    verified_assets: list[str] = []
    for filename in sorted({str(value) for value in materialized_files if str(value)}):
        identity = official.get(filename)
        if identity is None:
            raise AuipProtocolError("unknown_host_runtime_asset", filename)
        candidate = (bundle_root / filename).resolve()
        if (
            bundle_root not in candidate.parents
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            raise AuipProtocolError("auip_runtime_asset_missing", filename)
        try:
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError as exc:
            raise AuipProtocolError("auip_runtime_asset_unreadable", filename) from exc
        if digest != identity["sha256"]:
            raise AuipProtocolError("auip_runtime_asset_modified", filename)
        verified_assets.append(filename)

    required_references = ["managed-v0.js", "auip-v0.js"]
    if canonical.get("controller") is not None:
        required_references.append("controller-v0.js")
    for basename in required_references:
        matching_assets = [
            relative
            for relative in verified_assets
            if Path(relative).name.casefold() == basename.casefold()
        ]
        if len(matching_assets) != 1 or _source_asset_index(
            sources,
            entry_path=entry_path,
            bundle_root=bundle_root,
            relative_asset=matching_assets[0] if matching_assets else "",
        ) < 0:
            raise AuipProtocolError(
                "auip_runtime_asset_reference_mismatch",
                basename,
            )

    return {
        "verified": True,
        "binding": "web/v0",
        "app_id": str(canonical["app"]["id"]),
        "entry": entry_path.name,
        "manifest": manifest_path.name,
        "runtime_assets": verified_assets,
        "checks": [
            "manifest",
            "embedded_manifest_sync",
            "runtime_asset_integrity",
            "entry_wiring",
        ],
    }


def _entry_path(root: Path, requested: str) -> Path:
    clean = Path(str(requested or "").strip()).name
    if clean:
        candidate = (root / clean).resolve()
        if candidate.parent == root and candidate.suffix.casefold() in {".html", ".htm"}:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        raise AuipProtocolError("auip_entry_missing", clean)
    preferred = root / "index.html"
    if preferred.is_file() and not preferred.is_symlink():
        return preferred.resolve()
    entries = [
        path.resolve()
        for path in root.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.casefold() in {".html", ".htm"}
    ]
    if len(entries) != 1:
        raise AuipProtocolError("auip_entry_ambiguous", str(len(entries)))
    return entries[0]


def _script_sources(html: str) -> list[str]:
    parser = _ScriptCollector()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception as exc:
        raise AuipProtocolError("auip_entry_html_invalid", str(exc)) from exc
    return parser.sources


def _source_index(sources: list[str], filename: str) -> int:
    target = str(filename or "").casefold()
    for index, source in enumerate(sources):
        parsed = urlsplit(source)
        path = Path(unquote(parsed.path.replace("\\", "/")))
        if path.name.casefold() == target and not parsed.scheme and not parsed.netloc:
            return index
    return -1


def _source_asset_index(
    sources: list[str],
    *,
    entry_path: Path,
    bundle_root: Path,
    relative_asset: str,
) -> int:
    if not relative_asset:
        return -1
    expected = (bundle_root / Path(relative_asset)).resolve()
    for index, source in enumerate(sources):
        parsed = urlsplit(source)
        if parsed.scheme or parsed.netloc:
            continue
        relative = Path(unquote(parsed.path.replace("\\", "/")))
        if relative.is_absolute():
            continue
        candidate = (entry_path.parent / relative).resolve()
        if candidate == expected:
            return index
    return -1
