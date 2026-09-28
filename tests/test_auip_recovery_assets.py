"""AUIP discovery treats current registered source files as one bundle revision."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_host.provider_authoring import official_auip_runtime_assets
from agent_host.work_ledger_store import WorkLedgerStore
from server.auip_app_source import discover_registered_auip_app
from server.auip_bundle_validation import finalize_staged_auip_web_bundle


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


MATERIALIZED = (
    "sdk/auip-core/managed-v0.js",
    "sdk/auip-web/auip-v0.js",
)


def _attempt_metadata(bundle, *, verified: bool) -> dict:
    official = official_auip_runtime_assets()
    return {
        "auip_host_validates_bundle":True,
        "auip_bundle_root":str(bundle),
        "auip_host_materialized_files":list(MATERIALIZED),
        "auip_host_materialized_assets":{
            name:official[name] for name in MATERIALIZED},
        "host_auip_bundle_validation":{
            "verified":verified,
            "kind":"app_ready" if verified else "app_error",
            "boot":{"ok":verified},
            "entry":"index.html",
            "manifest":"auip.manifest.json",
        },
    }


def _seed_bundle(
    store: WorkLedgerStore,
    tmp_path,
    *,
    parent_verified: bool,
    stances=("spectator", "participant"),
):
    workspace = tmp_path / "workspace"
    bundle = workspace / "application"
    bundle.mkdir(parents=True)
    project = store.create_or_get_project(workspace)
    item = store.create_work_item(
        project.project_id,
        title="Recoverable AUIP application",
        workspace_path=workspace,
    )
    manifest = bundle / "auip.manifest.json"
    manifest.write_text(json.dumps({
        "schema":"amadeus.auip/v0",
        "app":{
            "id":"recovery-assets",
            "title":"Recovery Assets",
            "interactionSummary":"Run invokes app.run.",
        },
        "events":{"app.changed":{"beat":True}},
        "actions":{
            "app.run":{
                "description":"Run once.",
                "risk":"local_execution",
            }
        },
        "stances":list(stances),
        "situationKinds":["choice/v1"],
    }), encoding="utf-8")
    entry = bundle / "index.html"
    entry.write_text(
        "<!doctype html>"
        '<script id="auip-manifest" type="application/json">{}</script>'
        '<script src="./sdk/auip-core/managed-v0.js"></script>'
        '<script src="./sdk/auip-web/auip-v0.js"></script>'
        '<script src="./app.js"></script>',
        encoding="utf-8",
    )
    official = official_auip_runtime_assets()
    for name in MATERIALIZED:
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(str(official[name]["source_path"])).read_bytes())
    app_js = bundle / "app.js"
    app_js.write_text("globalThis.run = () => 'old';\n", encoding="utf-8")
    finalize_staged_auip_web_bundle(
        bundle,
        entry_filename=entry.name,
        materialized_files=MATERIALIZED,
        expected_assets={name:official[name] for name in MATERIALIZED},
    )
    operation, parent = store.create_operation_attempt(
        item.work_item_id,
        intent="execute",
        instruction="Create the AUIP application.",
        provider="codex",
        task="Create the AUIP application.",
        provider_run_id="run-parent",
        attempt_metadata=_attempt_metadata(bundle, verified=parent_verified),
    )
    parent = store.update_attempt(parent.attempt_id, execution_status="succeeded")
    entry_artifact = store.register_artifact(
        item.work_item_id,
        kind="business.file",
        title=entry.name,
        attempt_id=parent.attempt_id,
        path=entry,
        sha256=_digest(entry),
    )
    manifest_artifact = store.register_artifact(
        item.work_item_id,
        kind="business.file",
        title=manifest.name,
        attempt_id=parent.attempt_id,
        path=manifest,
        sha256=_digest(manifest),
    )
    return item, operation, parent, bundle, entry_artifact, manifest_artifact


def test_mode_mismatch_does_not_hide_a_boot_verified_observe_app(tmp_path) -> None:
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        item, _operation, parent, _bundle, _entry, _manifest = _seed_bundle(
            store,
            tmp_path,
            parent_verified=False,
            stances=("spectator",),
        )
        store.update_attempt(
            parent.attempt_id,
            metadata={
                "host_auip_bundle_validation": {
                    "verified": False,
                    "kind": "app_error",
                    "code": "auip_engagement_mode_unsupported",
                    "detail": (
                        "required engagement mode 'collaborate' is unsupported; "
                        "supported modes: observe"
                    ),
                    "entry": "index.html",
                    "manifest": "auip.manifest.json",
                    "boot": {"ok": True},
                    "required_mode": "collaborate",
                    "supported_modes": ["observe"],
                }
            },
        )

        discovered = discover_registered_auip_app(store, item.work_item_id)

        assert discovered is not None
        assert discovered["stances"] == ["spectator"]


def _successor(store, item, operation, bundle, *, run_id: str = "run-repair"):
    attempt = store.create_attempt(
        item.work_item_id,
        operation_id=operation.operation_id,
        provider="codex",
        task="Repair the same AUIP application.",
        provider_run_id=run_id,
        metadata=_attempt_metadata(bundle, verified=True),
    )
    return store.update_attempt(attempt.attempt_id, execution_status="succeeded")


@pytest.mark.parametrize("asset_name", ["app.js", "styles.css", "README.md"])
def test_registered_bundle_only_repair_contributes_the_successor_attempt(
        tmp_path, asset_name) -> None:
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        item, operation, parent, bundle, entry_artifact, manifest_artifact = (
            _seed_bundle(store, tmp_path, parent_verified=False))
        repair = _successor(store, item, operation, bundle)
        asset = bundle / asset_name
        asset.write_text("repaired bundle source\n", encoding="utf-8")
        store.register_artifact(
            item.work_item_id,
            kind="business.file",
            title=asset.name,
            attempt_id=repair.attempt_id,
            path=asset,
            sha256=_digest(asset),
        )

        discovered = discover_registered_auip_app(store, item.work_item_id)
        assert discovered is not None
        assert discovered["artifact_id"] == entry_artifact.artifact_id
        assert discovered["manifest_artifact_id"] == manifest_artifact.artifact_id
        assert discovered["contributing_attempt_ids"] == sorted(
            [parent.attempt_id, repair.attempt_id])
        assert len(store.list_attempts(item.work_item_id)) == 2
        assert len(store.list_operations(item.work_item_id)) == 1


def test_host_validation_success_without_registered_bundle_bytes_cannot_contribute(
        tmp_path) -> None:
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        item, operation, _parent, bundle, _entry, _manifest = _seed_bundle(
            store, tmp_path, parent_verified=False)
        _successor(store, item, operation, bundle)

        assert discover_registered_auip_app(store, item.work_item_id) is None


def test_outside_stale_pending_rejected_and_other_work_files_do_not_contribute(
        tmp_path) -> None:
    with WorkLedgerStore(tmp_path / "ledger.sqlite3") as store:
        item, operation, parent, bundle, entry_artifact, manifest_artifact = (
            _seed_bundle(store, tmp_path, parent_verified=True))
        workspace = tmp_path / "workspace"

        outside_attempt = _successor(
            store, item, operation, bundle, run_id="run-outside")
        outside = workspace / "outside.js"
        outside.write_text("outside\n", encoding="utf-8")
        store.register_artifact(
            item.work_item_id, kind="business.file", attempt_id=outside_attempt.attempt_id,
            path=outside, sha256=_digest(outside))

        external_attempt = _successor(
            store, item, operation, bundle, run_id="run-external")
        external = tmp_path / "external.js"
        external.write_text("external\n", encoding="utf-8")
        store.register_artifact(
            item.work_item_id, kind="business.file", attempt_id=external_attempt.attempt_id,
            path=external, sha256=_digest(external), status="registered")

        private_attempt = _successor(
            store, item, operation, bundle, run_id="run-private")
        private = bundle / ".amadeus/runtime/authoring_inputs/attempt/private.js"
        private.parent.mkdir(parents=True)
        private.write_text("private\n", encoding="utf-8")
        store.register_artifact(
            item.work_item_id, kind="business.file", attempt_id=private_attempt.attempt_id,
            path=private, sha256=_digest(private))

        stale_attempt = _successor(store, item, operation, bundle, run_id="run-stale")
        stale = bundle / "stale.js"
        stale.write_text("registered\n", encoding="utf-8")
        store.register_artifact(
            item.work_item_id, kind="business.file", attempt_id=stale_attempt.attempt_id,
            path=stale, sha256=_digest(stale))
        stale.write_text("changed after registration\n", encoding="utf-8")

        pending_attempt = _successor(store, item, operation, bundle, run_id="run-pending")
        pending = bundle / "pending.css"
        pending.write_text("pending\n", encoding="utf-8")
        store.register_artifact(
            item.work_item_id, kind="business.file", attempt_id=pending_attempt.attempt_id,
            path=pending, sha256=_digest(pending), status="pending")

        rejected_attempt = _successor(store, item, operation, bundle, run_id="run-rejected")
        rejected = bundle / "rejected.js"
        rejected.write_text("rejected\n", encoding="utf-8")
        store.register_artifact(
            item.work_item_id, kind="business.file", attempt_id=rejected_attempt.attempt_id,
            path=rejected, sha256=_digest(rejected), status="rejected")

        other_workspace = tmp_path / "other"
        other_workspace.mkdir()
        other_project = store.create_or_get_project(other_workspace)
        other_item = store.create_work_item(
            other_project.project_id, title="Other Work", workspace_path=other_workspace)
        _, other_attempt = store.create_operation_attempt(
            other_item.work_item_id, intent="execute", instruction="Other Work",
            provider="codex", task="Other Work", provider_run_id="run-other")
        other_file = other_workspace / "other.js"
        other_file.write_text("other\n", encoding="utf-8")
        store.register_artifact(
            other_item.work_item_id, kind="business.file", attempt_id=other_attempt.attempt_id,
            path=other_file, sha256=_digest(other_file))

        discovered = discover_registered_auip_app(store, item.work_item_id)
        assert discovered is not None
        assert discovered["artifact_id"] == entry_artifact.artifact_id
        assert discovered["manifest_artifact_id"] == manifest_artifact.artifact_id
        assert discovered["contributing_attempt_ids"] == [parent.attempt_id]
