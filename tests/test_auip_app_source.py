"""The host attaches registered artifacts only at their registered revision."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_host.provider_authoring import official_auip_runtime_assets
from server.auip_app_source import (
    _validate_host_managed_workspace_bundle,
    is_runnable_artifact,
    validate_registered_app,
)
from server.auip_bundle_validation import finalize_staged_auip_web_bundle
from server.auip_contract import AuipProtocolError


APP_HTML = "<!doctype html><title>Gomoku</title><script src='auip-v0.js'></script>"


@dataclass
class _Artifact:
    artifact_id: str
    work_item_id: str
    attempt_id: str
    kind: str
    title: str
    path: str
    status: str
    sha256: str


@dataclass
class _WorkItem:
    workspace_path: str


class _Store:
    def __init__(self, artifact: _Artifact, workspace: str) -> None:
        self._artifact = artifact
        self._item = _WorkItem(workspace_path=workspace)

    def get_artifact(self, artifact_id: str) -> Any:
        return self._artifact if artifact_id == self._artifact.artifact_id else None

    def get_work_item(self, work_item_id: str) -> Any:
        return self._item if work_item_id == self._artifact.work_item_id else None


def _fixture(
    root: Path,
    *,
    body: str = APP_HTML,
    name: str = "gomoku.html",
    status: str = "registered",
    kind: str = "business.file",
    sha: str | None = None,
) -> _Store:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body.encode("utf-8"))
    artifact = _Artifact(
        artifact_id="artifact-1",
        work_item_id="item-1",
        attempt_id="attempt-1",
        kind=kind,
        title="Gomoku",
        path=str(target),
        status=status,
        sha256=hashlib.sha256(body.encode("utf-8")).hexdigest() if sha is None else sha,
    )
    return _Store(artifact, str(workspace))


def _refusal(store: _Store, artifact_id: str = "artifact-1") -> str:
    try:
        validate_registered_app(store, artifact_id)
    except AuipProtocolError as error:
        return error.code
    raise AssertionError("expected the host to refuse this artifact")


def test_a_registered_html_artifact_resolves_to_a_revision_bound_entry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _fixture(Path(tmp))
        loaded = validate_registered_app(store, "artifact-1")

    assert loaded["entry_path"].endswith("gomoku.html")
    assert "html" not in loaded
    assert loaded["work_item_id"] == "item-1"
    # The ref names the bytes, not just the row, so a surface can tell one
    # revision of the app from the next.
    assert loaded["artifact_ref"].startswith("artifact:artifact-1@")
    assert loaded["sha256"] == hashlib.sha256(APP_HTML.encode("utf-8")).hexdigest()


def test_the_caller_cannot_name_a_file_only_a_registered_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _fixture(Path(tmp))
        assert _refusal(store, "artifact-missing") == "unknown_artifact"
        assert _refusal(store, "") == "missing_value"


def test_ambiguous_or_non_file_artifacts_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # "pending" is the registry saying it could not attribute the file to
        # this attempt. Running it would present someone else's file as this
        # WorkItem's app.
        assert _refusal(_fixture(Path(tmp) / "a", status="pending")) == "artifact_not_registered"
        assert _refusal(_fixture(Path(tmp) / "b", kind="git.delta")) == "artifact_not_a_file"


def test_only_web_app_entry_documents_are_runnable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # Relative assets are allowed by the future local app server, but the
        # registered entry itself must still be a web document.
        assert _refusal(_fixture(Path(tmp), name="auip-v0.js")) == "artifact_not_runnable"


def test_an_artifact_outside_its_workspace_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = _fixture(root)
        outside = root / "elsewhere.html"
        outside.write_bytes(APP_HTML.encode("utf-8"))
        store._artifact.path = str(outside)
        assert _refusal(store) == "artifact_outside_workspace"


def test_bytes_that_moved_on_from_the_registered_revision_are_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = _fixture(root)
        # The app was edited after registration. Serving it would run code the
        # ledger does not describe, under a session that claims otherwise.
        (root / "workspace" / "gomoku.html").write_bytes(b"<!doctype html><title>Changed</title>")
        assert _refusal(store) == "artifact_revision_changed"


def test_attempt_private_authoring_inputs_cannot_become_runtime_dependencies() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        body = (
            "<!doctype html><script src='.amadeus/runtime/authoring_inputs/"
            "attempt-1/sdk/auip-web/auip-v0.js'></script>"
        )
        store = _fixture(Path(tmp), body=body)
        assert _refusal(store) == "artifact_depends_on_private_authoring_input"


def test_proposed_export_staging_is_never_a_runnable_app_revision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _fixture(
            Path(tmp),
            name=".amadeus/proposed_exports/attempt-1/gomoku.html",
        )
        assert _refusal(store) == "artifact_is_proposed_export"
        assert is_runnable_artifact(store._artifact) is False


def test_run_affordance_is_offered_without_touching_the_filesystem() -> None:
    runnable = _Artifact(
        artifact_id="a",
        work_item_id="i",
        attempt_id="t",
        kind="business.file",
        title="Gomoku",
        path="/nowhere/gomoku.html",
        status="registered",
        sha256="",
    )
    assert is_runnable_artifact(runnable) is True

    for field, value in (("status", "pending"), ("kind", "git.delta"), ("path", "/nowhere/notes.md")):
        candidate = _Artifact(**{**runnable.__dict__, field: value})
        assert is_runnable_artifact(candidate) is False
    assert is_runnable_artifact(None) is False


def test_host_managed_attach_preserves_the_exact_bundle_validation_error() -> None:
    with tempfile.TemporaryDirectory(prefix="auip_attach_diagnostic_") as temp:
        root = Path(temp)
        materialized = (
            "sdk/auip-core/managed-v0.js",
            "sdk/auip-web/auip-v0.js",
        )
        official = official_auip_runtime_assets()
        for relative in materialized:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                Path(str(official[relative]["source_path"])).read_bytes()
            )
        manifest = root / "auip.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "amadeus.auip/v0",
                    "app": {
                        "id": "attach-test",
                        "title": "Attach Test",
                        "interactionSummary": (
                            "Advance once maps to app.advance."
                        ),
                    },
                    "events": {"app.changed": {"beat": True}},
                    "actions": {
                        "app.advance": {
                            "description": "Advance once.",
                            "risk": "local_execution",
                        }
                    },
                    "stances": ["spectator", "participant"],
                    "situationKinds": ["choice/v1"],
                }
            ),
            encoding="utf-8",
        )
        entry = root / "index.html"
        entry.write_text(
            "<!doctype html>"
            '<script id="auip-manifest" type="application/json">{}</script>'
            '<script src="./sdk/auip-core/managed-v0.js"></script>'
            '<script src="./sdk/auip-web/auip-v0.js"></script>',
            encoding="utf-8",
        )
        finalize_staged_auip_web_bundle(
            root,
            entry_filename=entry.name,
            materialized_files=materialized,
        )
        (root / materialized[0]).write_text("tampered", encoding="utf-8")
        alias = root / "alias"
        alias.mkdir()
        aliased_root = alias / ".."

        class AttemptStore:
            @staticmethod
            def get_attempt(attempt_id: str) -> Any:
                assert attempt_id == "attempt-host-managed"
                return SimpleNamespace(
                    metadata={
                        "auip_host_validates_bundle": True,
                        "auip_bundle_root": str(root),
                        "auip_host_materialized_files": list(materialized),
                    }
                )

        try:
            _validate_host_managed_workspace_bundle(
                AttemptStore(),  # type: ignore[arg-type]
                manifest_path=aliased_root / manifest.name,
                entry_path=aliased_root / entry.name,
                attempt_ids={"attempt-host-managed"},
            )
        except AuipProtocolError as exc:
            assert exc.code == "auip_runtime_asset_modified"
            assert exc.detail == materialized[0]
        else:
            raise AssertionError("attach validation must retain the exact error code")


def _main() -> None:
    test_a_registered_html_artifact_resolves_to_a_revision_bound_entry()
    test_the_caller_cannot_name_a_file_only_a_registered_artifact()
    test_ambiguous_or_non_file_artifacts_fail_closed()
    test_only_web_app_entry_documents_are_runnable()
    test_an_artifact_outside_its_workspace_is_refused()
    test_bytes_that_moved_on_from_the_registered_revision_are_refused()
    test_attempt_private_authoring_inputs_cannot_become_runtime_dependencies()
    test_proposed_export_staging_is_never_a_runnable_app_revision()
    test_run_affordance_is_offered_without_touching_the_filesystem()
    print("ok: only registered, unchanged app entries receive attach identity")


if __name__ == "__main__":
    _main()
