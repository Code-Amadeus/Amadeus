from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_host.provider_authoring import materialize_auip_runtime_assets
from server.auip_bundle_validation import (
    finalize_staged_auip_web_bundle,
    validate_staged_auip_web_bundle,
)
from server.auip_contract import AuipProtocolError


def _manifest() -> dict:
    return {
        "schema": "amadeus.auip/v0",
        "app": {
            "id": "shared-grid",
            "title": "Shared Grid",
            "version": "0.1.0",
            "objective": "Place one mark in an available cell.",
        },
        "events": {"game.ready": {"beat": True}},
        "actions": {},
        "stances": ["spectator"],
    }


def _bundle(root: Path) -> dict[str, dict[str, str]]:
    assets = materialize_auip_runtime_assets(root)
    manifest = _manifest()
    (root / "auip.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    embedded = json.dumps(manifest, ensure_ascii=False, indent=2)
    (root / "index.html").write_text(
        "<!doctype html>\n"
        '<script id="auip-manifest" type="application/json">\n'
        f"{embedded}\n"
        "</script>\n"
        '<script src="./sdk/auip-core/managed-v0.js"></script>\n'
        '<script src="./sdk/auip-core/situations-v0.js"></script>\n'
        '<script src="./sdk/auip-web/auip-v0.js"></script>\n',
        encoding="utf-8",
    )
    return assets


def _refusal(root: Path, assets: dict[str, dict[str, str]]) -> str:
    try:
        validate_staged_auip_web_bundle(
            root,
            entry_filename="index.html",
            materialized_files=tuple(assets),
        )
    except AuipProtocolError as exc:
        return exc.code
    raise AssertionError("expected staged AUIP bundle refusal")


def test_host_validates_packaging_without_provider_authored_commands() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_bundle_validation_") as temp:
        root = Path(temp)
        assets = _bundle(root)
        result = validate_staged_auip_web_bundle(
            root,
            entry_filename="index.html",
            materialized_files=tuple(assets),
        )
        assert result["verified"] is True
        assert result["app_id"] == "shared-grid"
        assert result["runtime_assets"] == [
            "sdk/auip-core/controller-v0.js",
            "sdk/auip-core/managed-v0.js",
            "sdk/auip-core/situations-v0.js",
            "sdk/auip-web/auip-v0.js",
        ]
        assert result["checks"] == [
            "manifest",
            "embedded_manifest_sync",
            "runtime_asset_integrity",
            "entry_wiring",
        ]


def test_controller_manifest_requires_the_official_controller_core_reference() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_controller_bundle_") as temp:
        root = Path(temp)
        assets = _bundle(root)
        manifest = _manifest()
        manifest["stances"] = ["spectator", "participant"]
        manifest["app"]["interactionSummary"] = (
            "The participant can set one sustained navigation policy. "
            "For example, 'go home' selects the matching declared destination."
        )
        manifest["situationKinds"] = ["controller/v1"]
        manifest["events"]["vehicle.controller_effect"] = {
            "importance": "important",
            "controllerEffect": True,
        }
        manifest["actions"] = {
            "vehicle.set_policy": {
                "description": "Set the exact application navigation policy.",
                "risk": "local_execution",
                "inputSchema": {
                    "type": "object",
                    "properties": {"destination": {"type": "string"}},
                    "required": ["destination"],
                    "additionalProperties": False,
                },
            }
        }
        manifest["controller"] = {
            "policyActions": ["vehicle.set_policy"],
            "leaseDurationMs": 30_000,
            "maxActionRateHz": 12,
            "takeover": "immediate",
        }
        (root / "auip.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        try:
            finalize_staged_auip_web_bundle(
                root,
                entry_filename="index.html",
                materialized_files=tuple(assets),
            )
        except AuipProtocolError as exc:
            assert exc.code == "auip_controller_asset_not_referenced"
        else:
            raise AssertionError("Controller profile requires its official Core")
        assert _refusal(root, assets) == "auip_controller_asset_not_referenced"

        entry = root / "index.html"
        html = entry.read_text(encoding="utf-8").replace(
            '<script src="./sdk/auip-core/situations-v0.js"></script>',
            '<script src="./sdk/auip-core/controller-v0.js"></script>\n'
            '<script src="./sdk/auip-core/situations-v0.js"></script>',
        )
        entry.write_text(html, encoding="utf-8")
        assert validate_staged_auip_web_bundle(
            root,
            entry_filename="index.html",
            materialized_files=tuple(assets),
        )["verified"] is True


def test_host_rejects_modified_runtime_and_stale_embedded_manifest() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_bundle_runtime_drift_") as temp:
        root = Path(temp)
        assets = _bundle(root)
        (root / "sdk" / "auip-core" / "managed-v0.js").write_text(
            "modified", encoding="utf-8"
        )
        assert _refusal(root, assets) == "auip_runtime_asset_modified"

    with tempfile.TemporaryDirectory(prefix="auip_bundle_manifest_drift_") as temp:
        root = Path(temp)
        assets = _bundle(root)
        manifest = _manifest()
        manifest["app"]["title"] = "Changed"
        (root / "auip.manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        assert _refusal(root, assets) == "embedded_manifest_out_of_sync"

        finalized = finalize_staged_auip_web_bundle(
            root,
            entry_filename="index.html",
            materialized_files=tuple(assets),
        )
        assert finalized["verified"] is True
        assert finalized["generated_steps"] == ["embedded_manifest_sync"]


def test_host_rejects_runtime_wiring_in_the_wrong_order() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_bundle_wiring_") as temp:
        root = Path(temp)
        assets = _bundle(root)
        entry = root / "index.html"
        html = entry.read_text(encoding="utf-8")
        html = html.replace(
            '<script src="./sdk/auip-core/managed-v0.js"></script>\n'
            '<script src="./sdk/auip-core/situations-v0.js"></script>\n'
            '<script src="./sdk/auip-web/auip-v0.js"></script>',
            '<script src="./sdk/auip-web/auip-v0.js"></script>\n'
            '<script src="./sdk/auip-core/managed-v0.js"></script>',
        )
        entry.write_text(html, encoding="utf-8")
        assert _refusal(root, assets) == "auip_runtime_asset_order_invalid"


def test_entry_must_reference_the_host_materialized_asset_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_bundle_asset_reference_") as temp:
        root = Path(temp)
        assets = _bundle(root)
        entry = root / "index.html"
        html = entry.read_text(encoding="utf-8").replace(
            "./sdk/auip-core/managed-v0.js",
            "./managed-v0.js",
        )
        entry.write_text(html, encoding="utf-8")

        assert _refusal(root, assets) == "auip_runtime_asset_reference_mismatch"
