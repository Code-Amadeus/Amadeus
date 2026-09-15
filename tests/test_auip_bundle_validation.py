from __future__ import annotations

import asyncio
import json
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

from agent_host.provider_authoring import materialize_auip_runtime_assets
from server.auip_bundle_validation import (
    AuipHostMaterializationError,
    finalize_staged_auip_web_bundle,
    validate_auip_web_bundle_execution,
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


def test_host_owned_sdk_upgrade_preserves_old_validation_and_rejects_tampering(tmp_path, monkeypatch):
    from agent_host import provider_authoring
    from server import auip_bundle_validation

    root = tmp_path / "application"
    old = _bundle(root)
    official = provider_authoring.official_auip_runtime_assets()
    relative = "sdk/auip-core/situations-v0.js"
    replacement = tmp_path / "next-situations.js"
    replacement.write_bytes(Path(official[relative]["source_path"]).read_bytes() + b"\n// next Host version\n")
    next_assets = {name:dict(value) for name, value in official.items()}
    next_assets[relative].update(source_path=str(replacement),
        sha256=hashlib.sha256(replacement.read_bytes()).hexdigest())
    monkeypatch.setattr(provider_authoring, "official_auip_runtime_assets", lambda:next_assets)
    monkeypatch.setattr(auip_bundle_validation, "official_auip_runtime_assets", lambda:next_assets)

    assert validate_staged_auip_web_bundle(root,
        materialized_files=tuple(old), expected_assets=old)["verified"]
    original_entry = (root / "index.html").read_bytes()
    upgraded = materialize_auip_runtime_assets(root, replace_existing=False,
        previously_materialized=old)
    assert (root / relative).read_bytes() == replacement.read_bytes()
    assert (root / "index.html").read_bytes() == original_entry
    assert validate_staged_auip_web_bundle(root,
        materialized_files=tuple(upgraded), expected_assets=upgraded)["verified"]

    target = root / relative
    target.write_text("app-local modification", encoding="utf-8")
    with pytest.raises(OSError, match="conflicting AUIP runtime asset"):
        materialize_auip_runtime_assets(root, replace_existing=False,
            previously_materialized=upgraded)
    assert target.read_text(encoding="utf-8") == "app-local modification"
    with pytest.raises(AuipProtocolError) as refused:
        validate_staged_auip_web_bundle(root,
            materialized_files=tuple(upgraded), expected_assets=upgraded)
    assert refused.value.code == "auip_runtime_asset_modified"


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


async def test_execution_wrapper_rejects_static_app_error_without_browser(
        tmp_path, monkeypatch):
    root = tmp_path / "bad-manifest"
    assets = _bundle(root)
    (root / "auip.manifest.json").write_text("not json", encoding="utf-8")
    boot = AsyncMock(side_effect=AssertionError(
        "static rejection must not launch a browser"))
    monkeypatch.setattr("server.auip_bundle_validation.validate_entry", boot)

    result = await validate_auip_web_bundle_execution(
        root, materialized_files=tuple(assets), expected_assets=assets)
    required = await validate_auip_web_bundle_execution(
        root,
        materialized_files=tuple(assets),
        expected_assets=assets,
        required_mode="collaborate",
    )

    assert result["verified"] is False
    assert result["kind"] == "app_error"
    assert result["code"] == "manifest_read_failed"
    assert result["detail"]
    assert result["checks"] == [] and result["boot"] is None
    assert required["required_mode"] == "collaborate"
    assert "supported_modes" not in required
    boot.assert_not_awaited()


async def test_execution_wrapper_classifies_host_sdk_bytes_by_exception_type(
        tmp_path, monkeypatch):
    root = tmp_path / "host-bytes"
    assets = _bundle(root)
    relative = "sdk/auip-core/managed-v0.js"
    (root / relative).write_text("tampered", encoding="utf-8")
    boot = AsyncMock(side_effect=AssertionError(
        "Host materialization rejection must not launch a browser"))
    monkeypatch.setattr("server.auip_bundle_validation.validate_entry", boot)

    with pytest.raises(AuipHostMaterializationError) as raised:
        validate_staged_auip_web_bundle(root,
            materialized_files=tuple(assets), expected_assets=assets)
    assert raised.value.code == "auip_runtime_asset_modified"
    result = await validate_auip_web_bundle_execution(
        root, materialized_files=tuple(assets), expected_assets=assets)

    assert result == {
        "verified":False,
        "kind":"host_materialization_error",
        "code":"auip_runtime_asset_modified",
        "detail":relative,
        "checks":[],
        "boot":None,
    }
    boot.assert_not_awaited()

    wiring_root = tmp_path / "app-wiring"
    wiring_assets = _bundle(wiring_root)
    entry = wiring_root / "index.html"
    entry.write_text(entry.read_text(encoding="utf-8").replace(
        "./sdk/auip-core/managed-v0.js", "./managed-v0.js"), encoding="utf-8")
    wiring = await validate_auip_web_bundle_execution(wiring_root,
        materialized_files=tuple(wiring_assets), expected_assets=wiring_assets)
    assert wiring["verified"] is False
    assert wiring["kind"] == "app_error"
    assert wiring["code"] == "auip_runtime_asset_reference_mismatch"
    boot.assert_not_awaited()


async def test_execution_wrapper_keeps_os_and_browser_failures_out_of_app_repair(
        tmp_path, monkeypatch):
    from server import auip_bundle_validation

    root = tmp_path / "environment-errors"
    assets = _bundle(root)
    original = auip_bundle_validation.validate_staged_auip_web_bundle

    def denied(*_args, **_kwargs):
        raise PermissionError("controlled permission denial")

    monkeypatch.setattr(auip_bundle_validation,
        "validate_staged_auip_web_bundle", denied)
    denied_result = await validate_auip_web_bundle_execution(root,
        materialized_files=tuple(assets), expected_assets=assets)
    assert denied_result["kind"] == "tool_error"
    assert denied_result["code"] == "auip_bundle_validation_io_failed"

    monkeypatch.setattr(auip_bundle_validation,
        "validate_staged_auip_web_bundle", original)
    monkeypatch.setattr(auip_bundle_validation, "validate_entry", AsyncMock(
        return_value={"ok":False, "kind":"browser_environment_error",
            "diagnostics":[{"code":"browser_unavailable",
                "message":"controlled browser failure"}]}))
    browser_result = await validate_auip_web_bundle_execution(root,
        materialized_files=tuple(assets), expected_assets=assets)
    assert browser_result["kind"] == "browser_environment_error"
    assert browser_result["code"] == "browser_unavailable"


async def test_execution_wrapper_awaits_boot_without_blocking_event_loop(
        tmp_path, monkeypatch):
    root = tmp_path / "blocked-boot"
    assets = _bundle(root)
    entered, release, advanced = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_boot(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"ok":True, "kind":"ok", "diagnostics":[],
            "scope":{"doesNotProve":"live Attach or gameplay"}}

    monkeypatch.setattr("server.auip_bundle_validation.validate_entry", blocked_boot)
    pending = asyncio.create_task(validate_auip_web_bundle_execution(
        root, materialized_files=tuple(assets), expected_assets=assets))
    await asyncio.wait_for(entered.wait(), 2)
    asyncio.get_running_loop().call_soon(advanced.set)
    await asyncio.wait_for(advanced.wait(), 1)
    assert not pending.done()
    release.set()
    result = await asyncio.wait_for(pending, 2)

    assert result["verified"] is True and result["kind"] == result["code"] == "ok"
    assert result["checks"][-1] == "entry_boot"
    assert result["boot"]["scope"]["doesNotProve"] == "live Attach or gameplay"


async def test_execution_wrapper_keeps_boot_and_required_mode_as_separate_facts(
    tmp_path, monkeypatch
):
    root = tmp_path / "spectator-only"
    assets = _bundle(root)
    boot = {"ok": True, "kind": "ok", "diagnostics": []}
    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_entry",
        AsyncMock(return_value=boot),
    )

    legacy = await validate_auip_web_bundle_execution(
        root, materialized_files=tuple(assets), expected_assets=assets
    )
    observe = await validate_auip_web_bundle_execution(
        root,
        materialized_files=tuple(assets),
        expected_assets=assets,
        required_mode="observe",
    )
    collaborate = await validate_auip_web_bundle_execution(
        root,
        materialized_files=tuple(assets),
        expected_assets=assets,
        required_mode="collaborate",
    )

    assert legacy["verified"] is True
    assert "required_mode" not in legacy
    assert observe["verified"] is True
    assert observe["supported_modes"] == ["observe"]
    assert collaborate["verified"] is False
    assert collaborate["kind"] == "app_error"
    assert collaborate["code"] == "auip_engagement_mode_unsupported"
    assert collaborate["boot"]["ok"] is True
    assert "collaborate" in collaborate["detail"]
    assert "observe" in collaborate["detail"]


async def test_boot_error_remains_primary_when_required_mode_also_mismatches(
    tmp_path, monkeypatch
):
    root = tmp_path / "boot-and-mode"
    assets = _bundle(root)
    monkeypatch.setattr(
        "server.auip_bundle_validation.validate_entry",
        AsyncMock(
            return_value={
                "ok": False,
                "kind": "app_error",
                "diagnostics": [
                    {
                        "source": "pageerror",
                        "code": "javascript_page_error",
                        "message": "initial snapshot failed",
                    }
                ],
            }
        ),
    )

    result = await validate_auip_web_bundle_execution(
        root,
        materialized_files=tuple(assets),
        expected_assets=assets,
        required_mode="collaborate",
    )

    assert result["code"] == "javascript_page_error"
    assert result["detail"] == "initial snapshot failed"
    assert result["boot"]["ok"] is False
    assert result["supported_modes"] == ["observe"]
    assert any(
        item["code"] == "auip_engagement_mode_unsupported"
        for item in result["boot"]["diagnostics"]
    )
