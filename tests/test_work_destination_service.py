"""Contract tests for Project, Draft, and Session destination ownership."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.work_ledger_store import WorkLedgerStore
from server.work_destination_service import WorkDestinationService


def test_project_default_and_active_draft_remain_orthogonal() -> None:
    with tempfile.TemporaryDirectory(prefix="work_destination_") as temp:
        root = Path(temp)
        project_root = root / "project"
        scratch_root = root / "scratch"
        draft_root = scratch_root / "draft-one"
        project_root.mkdir()
        draft_root.mkdir(parents=True)
        old_scratch = settings.WORK_SCRATCH_ROOT
        old_allowlist = settings.WORK_PROJECT_ALLOWLIST
        settings.WORK_SCRATCH_ROOT = str(scratch_root)
        settings.WORK_PROJECT_ALLOWLIST = str(project_root)
        try:
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                project = store.create_or_get_project(project_root, name="Main")
                scratch = store.create_or_get_project(
                    scratch_root,
                    name="scratch",
                    metadata={"scratch": True},
                )
                draft = store.create_work_item(
                    scratch.project_id,
                    title="Draft goal",
                    goal="Build a draft.",
                    workspace_path=str(draft_root),
                )
                destination = WorkDestinationService(store)
                destination.set_session_project("voice", project.project_id)
                selected = destination.bind_session_context(
                    "voice",
                    "",
                    work_item_id=draft.work_item_id,
                    source="test",
                )
                assert selected["bindingKind"] == "work_item"
                assert selected["projectId"] == ""
                assert selected["projectName"] == "Draft"
                binding = destination.conversation_binding("voice")
                assert binding is not None
                assert binding["workItemId"] == draft.work_item_id
                assert binding["projectId"] == ""
                assert binding["defaultProjectId"] == project.project_id
                assert destination.session_project("voice") == project.project_id
        finally:
            settings.WORK_SCRATCH_ROOT = old_scratch
            settings.WORK_PROJECT_ALLOWLIST = old_allowlist


def test_workspace_route_authority_is_pin_then_explicit_then_session_then_draft() -> None:
    with tempfile.TemporaryDirectory(prefix="work_destination_route_") as temp:
        root = Path(temp)
        project_root = root / "project"
        other_root = root / "other"
        scratch_root = root / "scratch"
        project_root.mkdir()
        other_root.mkdir()
        scratch_root.mkdir()
        old_scratch = settings.WORK_SCRATCH_ROOT
        old_allowlist = settings.WORK_PROJECT_ALLOWLIST
        settings.WORK_SCRATCH_ROOT = str(scratch_root)
        settings.WORK_PROJECT_ALLOWLIST = f"{project_root};{other_root}"
        try:
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                project = store.create_or_get_project(project_root, name="Main")
                other = store.create_or_get_project(other_root, name="Other")
                item = store.create_work_item(
                    project.project_id,
                    title="Existing delivery",
                    goal="Keep continuity.",
                    workspace_path=str(project_root),
                )
                destination = WorkDestinationService(store)

                explicit = destination.resolve_workspace_route(
                    {"project_id": other.project_id}
                )
                assert explicit["cwd"] == str(other_root.resolve())
                assert explicit["source"] == "intent_project"

                continued = destination.resolve_workspace_route(
                    {"workspace_ref": item.work_item_id}
                )
                assert continued["workItemId"] == item.work_item_id
                assert continued["source"] == "intent_workspace_ref"

                destination.set_session_project("voice", project.project_id)
                inherited = destination.resolve_workspace_route(
                    {"session_id": "voice"}
                )
                assert inherited["projectId"] == project.project_id
                assert inherited["source"] == "session_project"

                one_off = destination.resolve_workspace_route(
                    {"session_id": "voice", "one_off": True}
                )
                assert one_off["cwd"] == str(scratch_root.resolve())
                assert one_off["source"] == "scratch_default"
        finally:
            settings.WORK_SCRATCH_ROOT = old_scratch
            settings.WORK_PROJECT_ALLOWLIST = old_allowlist


def test_session_project_recovers_from_ledger_and_clears_both_pointers() -> None:
    with tempfile.TemporaryDirectory(prefix="work_destination_restart_") as temp:
        root = Path(temp)
        project_root = root / "project"
        project_root.mkdir()
        old_allowlist = settings.WORK_PROJECT_ALLOWLIST
        settings.WORK_PROJECT_ALLOWLIST = str(project_root)
        try:
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                project = store.create_or_get_project(project_root, name="Main")
                item = store.create_work_item(
                    project.project_id,
                    title="Persistent context",
                    goal="Persist context.",
                    workspace_path=str(project_root),
                )
                first = WorkDestinationService(store)
                first.bind_session_context(
                    "voice",
                    project.project_id,
                    work_item_id=item.work_item_id,
                )
                restarted = WorkDestinationService(store)
                assert restarted.session_project("voice") == project.project_id
                assert restarted.conversation_binding("voice")["workItemId"] == item.work_item_id  # type: ignore[index]
                restarted.clear_session_project("voice")
                assert restarted.session_project("voice") == ""
                assert restarted.conversation_binding("voice") is None
        finally:
            settings.WORK_PROJECT_ALLOWLIST = old_allowlist


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all work destination service tests passed")


if __name__ == "__main__":
    _main()
