"""Two-phase Desktop export policy and security-boundary tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.work_export_service import WorkExportService


def _records(store: WorkLedgerStore, workspace: Path, task: str):
    workspace.mkdir(parents=True, exist_ok=True)
    project = store.create_or_get_project(workspace)
    item = store.create_work_item(project.project_id, title="Desktop export", goal=task)
    attempt = store.create_attempt(item.work_item_id, provider="locus", task=task)
    return item, attempt


def test_default_desktop_path_prefers_environment_override() -> None:
    with tempfile.TemporaryDirectory(prefix="desktop_override_") as temp:
        root = Path(temp)
        override = root / "Managed Desktop"
        with (
            patch.dict(os.environ, {"AMADEUS_DESKTOP_PATH": str(override)}),
            patch(
                "server.work_export_service._windows_known_desktop_path"
            ) as known_folder,
            WorkLedgerStore(root / "ledger.sqlite3") as store,
        ):
            service = WorkExportService(store)
        assert service.desktop_path == override.resolve()
        known_folder.assert_not_called()


def test_default_desktop_path_uses_windows_known_folder_before_home_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="desktop_known_folder_") as temp:
        root = Path(temp)
        known_desktop = root / "OneDrive" / "Desktop"
        env = dict(os.environ)
        env.pop("AMADEUS_DESKTOP_PATH", None)
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "server.work_export_service._windows_known_desktop_path",
                return_value=known_desktop,
            ),
            WorkLedgerStore(root / "ledger.sqlite3") as store,
        ):
            service = WorkExportService(store)
        assert service.desktop_path == known_desktop.resolve()


def test_default_desktop_path_falls_back_to_home_desktop() -> None:
    with tempfile.TemporaryDirectory(prefix="desktop_home_fallback_") as temp:
        root = Path(temp)
        env = dict(os.environ)
        env.pop("AMADEUS_DESKTOP_PATH", None)
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "server.work_export_service._windows_known_desktop_path",
                return_value=None,
            ),
            patch("server.work_export_service.Path.home", return_value=root),
            WorkLedgerStore(root / "ledger.sqlite3") as store,
        ):
            service = WorkExportService(store)
        assert service.desktop_path == (root / "Desktop").resolve()


def test_desktop_task_is_staged_then_exported_only_after_exact_approval() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "デスクトップにチェスを書き、ファイル名は chess_game.py。"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={},
            )
            assert plan is not None
            assert plan["requested_filename"] == "chess_game.py"
            prompt = service.provider_prompt(task, plan)
            assert str(plan["staging_root"]) in prompt
            assert "Do not write" in prompt and "Desktop" in prompt

            staged = Path(plan["staging_root"]) / "chess_game.py"
            staged.write_text("print('chess')\n", encoding="utf-8")
            outcome = service.discover_staged_exports(attempt, item, plan)
            permission = outcome["permission"]
            assert permission is not None and permission.status == "pending"
            assert permission.scope_paths[0] == str(desktop / "chess_game.py")
            assert permission.scope_paths[1] == permission.metadata["entries"][0]["temporary_path"]
            assert not (desktop / "chess_game.py").exists()
            assert "Desktop/chess_game.py" in outcome["changed_files"]
            assert "+print('chess')" in outcome["patch"]

            resolution = service.resolve(permission.request_id, allow=True)
            assert resolution.permission.status == "allowed"
            assert (desktop / "chess_game.py").read_text(encoding="utf-8") == "print('chess')\n"
            exported = [
                artifact
                for artifact in store.list_artifacts(item.work_item_id)
                if artifact.kind == "business.export"
            ]
            assert len(exported) == 1 and exported[0].status == "approved"
            replay = service.discover_staged_exports(attempt, item, plan)
            assert replay["pending_export"] is False
            assert replay["reason"] == "external_export_complete"
            assert [
                artifact.status
                for artifact in store.list_artifacts(item.work_item_id)
                if artifact.kind == "business.export"
            ] == ["approved"]


def test_approval_rejects_changed_source_existing_target_and_path_escape() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_guard_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Save a file to Desktop; filename is result.py"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus", mode="agent", task=task, item=item, attempt=attempt, metadata={}
            )
            assert plan is not None
            staged = Path(plan["staging_root"]) / "result.py"
            staged.write_text("value = 1\n", encoding="utf-8")
            permission = service.discover_staged_exports(attempt, item, plan)["permission"]
            assert permission is not None
            staged.write_text("value = 2\n", encoding="utf-8")
            try:
                service.resolve(permission.request_id, allow=True)
            except WorkLedgerConflict as exc:
                assert "changed after approval" in str(exc)
            else:
                raise AssertionError("mutated staged source must fail closed")
            assert store.get_permission_request(permission.request_id).status == "pending"  # type: ignore[union-attr]

            # An attempt owns exactly one immutable export contract, so use a
            # separate attempt to exercise the unrelated-target boundary.
            target_item, target_attempt = _records(
                store,
                root / "target-workspace",
                task,
            )
            target_plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=task,
                item=target_item,
                attempt=target_attempt,
                metadata={},
            )
            assert target_plan is not None
            target_staged = Path(target_plan["staging_root"]) / "result.py"
            target_staged.write_text("value = 3\n", encoding="utf-8")
            fresh = service.discover_staged_exports(
                target_attempt,
                target_item,
                target_plan,
            )["permission"]
            assert fresh is not None and fresh.request_id != permission.request_id
            (desktop / "result.py").write_text("owned by user\n", encoding="utf-8")
            try:
                service.resolve(fresh.request_id, allow=True)
            except WorkLedgerConflict as exc:
                assert "will not be overwritten" in str(exc)
            else:
                raise AssertionError("existing Desktop files must not be overwritten")

            escaped = dict(plan)
            escaped["staging_root"] = str(root)
            try:
                service.discover_staged_exports(attempt, item, escaped)
            except WorkLedgerConflict:
                pass
            else:
                raise AssertionError("staging scope escape must fail closed")


def test_amend_inherits_and_replaces_the_last_approved_desktop_export() -> None:
    """A follow-up edits the delivered bytes, not an empty scratch file."""

    with tempfile.TemporaryDirectory(prefix="work_export_amend_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            service = WorkExportService(store, desktop_path=desktop)
            original_task = "Create endless_game.html on the Desktop"
            original_item, original_attempt = _records(store, workspace, original_task)
            original_plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=original_task,
                item=original_item,
                attempt=original_attempt,
                metadata={},
            )
            assert original_plan is not None
            original_bytes = "<html><p>one player</p></html>\n"
            (Path(original_plan["staging_root"]) / "endless_game.html").write_text(
                original_bytes,
                encoding="utf-8",
            )
            first_permission = service.discover_staged_exports(
                original_attempt,
                original_item,
                original_plan,
            )["permission"]
            service.resolve(first_permission.request_id, allow=True)

            amend_task = "Add a second player to endless_game.html"
            amend_item, amend_attempt = _records(store, workspace, amend_task)
            amend_plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=amend_task,
                item=amend_item,
                attempt=amend_attempt,
                metadata={
                    "intent": "amend",
                    "related_work_item_id": original_item.work_item_id,
                },
            )
            assert amend_plan is not None
            assert amend_plan["replace_existing"] is True
            inherited = Path(amend_plan["staging_root"]) / "endless_game.html"
            assert inherited.read_text(encoding="utf-8") == original_bytes
            prompt = service.provider_prompt(amend_task, amend_plan)
            assert "already been copied" in prompt
            assert "modify that existing staged file in place" in prompt

            revised_bytes = "<html><p>two players</p></html>\n"
            inherited.write_text(revised_bytes, encoding="utf-8")
            outcome = service.discover_staged_exports(amend_attempt, amend_item, amend_plan)
            permission = outcome["permission"]
            assert permission is not None and permission.status == "pending"
            assert "--- a/Desktop/endless_game.html" in outcome["patch"]
            assert "new file mode" not in outcome["patch"]
            entry = permission.metadata["entries"][0]
            assert entry["replace_existing"] is True
            assert entry["expected_target_sha256"] == amend_plan["expected_target_sha256"]

            service.resolve(permission.request_id, allow=True)
            assert (desktop / "endless_game.html").read_text(encoding="utf-8") == revised_bytes


def test_natural_auip_amend_inherits_approved_identity_not_model_filename() -> None:
    """A role paraphrase cannot rename the Artifact selected by WorkItem identity."""

    with tempfile.TemporaryDirectory(prefix="work_export_auip_lineage_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            service = WorkExportService(store, desktop_path=desktop)
            original_task = "Create gomoku.html on the Desktop"
            item, original_attempt = _records(store, workspace, original_task)
            original_plan = service.prepare_plan(
                provider="codex",
                mode="agent",
                task=original_task,
                item=item,
                attempt=original_attempt,
                metadata={"external_export": {"target": "desktop"}},
            )
            assert original_plan is not None
            original_bytes = b"<!doctype html><title>Gomoku</title>\n"
            (Path(original_plan["staging_root"]) / "gomoku.html").write_bytes(
                original_bytes
            )
            permission = service.discover_staged_exports(
                original_attempt,
                item,
                original_plan,
            )["permission"]
            assert permission is not None
            service.resolve(permission.request_id, allow=True)
            store.update_attempt(
                original_attempt.attempt_id,
                execution_status="succeeded",
            )

            # This reproduces the real conversational seam: the user refers
            # to the approved delivery by identity, while role prose invents
            # a new filename that the user never requested.
            misleading_task = (
                "五子棋をAUIP対応にして、ファイル名は wuziqi.html とする。"
            )
            amend_attempt = store.create_attempt(
                item.work_item_id,
                provider="codex",
                task=misleading_task,
            )
            amend_plan = service.prepare_plan(
                provider="codex",
                mode="agent",
                task=misleading_task,
                item=item,
                attempt=amend_attempt,
                metadata={
                    "intent": "amend",
                    "related_work_item_id": item.work_item_id,
                    "source_user_text": "你能接入它吗？我想和你玩一把。",
                    "host_outcome_requirement": {
                        "operation": "prepare",
                        "facet": "auip.application",
                        "expected": {"current_attempt_contribution": True},
                    },
                },
            )

            assert amend_plan is not None
            assert amend_plan["detected_by"] == "related_approved_export"
            assert amend_plan["publication_shape"] == "bundle"
            assert amend_plan["entry_filename"] == "gomoku.html"
            assert amend_plan["inherited_source"] is True
            assert Path(amend_plan["staging_root"]).name == amend_attempt.attempt_id
            assert (
                Path(amend_plan["staging_root"]) / "gomoku.html"
            ).read_bytes() == original_bytes


def test_auip_amend_inherits_one_approved_multifile_revision_as_a_unit() -> None:
    """HTML, manifest and SDK from one approval never become three candidates."""

    with tempfile.TemporaryDirectory(prefix="work_export_auip_bundle_lineage_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            service = WorkExportService(store, desktop_path=desktop)
            task = "Create an interactive Gomoku app on the Desktop"
            item, first_attempt = _records(store, workspace, task)
            first_plan = service.prepare_plan(
                provider="codex",
                mode="agent",
                task=task,
                item=item,
                attempt=first_attempt,
                metadata={"external_export": {"target": "desktop"}},
            )
            assert first_plan is not None
            original = {
                "gomoku.html": b"<!doctype html><title>Gomoku</title>\n",
                "auip.manifest.json": b'{"protocol":"auip/v0"}\n',
                "auip-v0.js": b"window.AmadeusAUIP = {};\n",
            }
            first_stage = Path(first_plan["staging_root"])
            for name, content in original.items():
                (first_stage / name).write_bytes(content)
            first_permission = service.discover_staged_exports(
                first_attempt,
                item,
                first_plan,
            )["permission"]
            assert first_permission is not None
            service.resolve(first_permission.request_id, allow=True)
            store.update_attempt(
                first_attempt.attempt_id,
                execution_status="succeeded",
            )

            amend_attempt = store.create_attempt(
                item.work_item_id,
                provider="codex",
                task="Make the approved game available for Amadeus participation",
            )
            amend_plan = service.prepare_plan(
                provider="codex",
                mode="agent",
                task=amend_attempt.task,
                item=item,
                attempt=amend_attempt,
                metadata={
                    "intent": "amend",
                    "related_work_item_id": item.work_item_id,
                    "source_user_text": "打开刚才那个游戏，我们一起玩。",
                    "host_outcome_requirement": {
                        "operation": "prepare",
                        "facet": "auip.application",
                        "expected": {"current_attempt_contribution": True},
                    },
                },
            )

            assert amend_plan is not None
            assert amend_plan["publication_shape"] == "bundle"
            assert amend_plan["inherited_permission_request_id"] == (
                first_permission.request_id
            )
            assert set(amend_plan["inherited_staging_paths"]) == set(original)
            assert amend_plan["entry_filename"] == "gomoku.html"
            amend_stage = Path(amend_plan["staging_root"])
            assert {
                name: (amend_stage / name).read_bytes()
                for name in original
            } == original
            assert "one immutable, user-approved Desktop publication" in (
                service.provider_prompt(amend_attempt.task, amend_plan)
            )


def test_amend_never_overwrites_desktop_drift_after_last_approval() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_amend_drift_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            service = WorkExportService(store, desktop_path=desktop)
            item, attempt = _records(
                store,
                workspace,
                "Create endless_game.html on the Desktop",
            )
            plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=attempt.task,
                item=item,
                attempt=attempt,
                metadata={},
            )
            assert plan is not None
            (Path(plan["staging_root"]) / "endless_game.html").write_text(
                "one player\n", encoding="utf-8"
            )
            permission = service.discover_staged_exports(attempt, item, plan)["permission"]
            service.resolve(permission.request_id, allow=True)

            amend_item, amend_attempt = _records(store, workspace, "Add player two")
            amend_plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task="Add player two",
                item=amend_item,
                attempt=amend_attempt,
                metadata={"intent": "amend", "related_work_item_id": item.work_item_id},
            )
            assert amend_plan is not None
            (Path(amend_plan["staging_root"]) / "endless_game.html").write_text(
                "two players\n", encoding="utf-8"
            )
            (desktop / "endless_game.html").write_text(
                "user changed this directly\n", encoding="utf-8"
            )
            try:
                service.discover_staged_exports(amend_attempt, amend_item, amend_plan)
            except WorkLedgerConflict as exc:
                assert "changed since its last approval" in str(exc)
            else:
                raise AssertionError("Desktop drift must block an amendment replacement")


def test_denial_is_durable_and_creates_no_desktop_file() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_deny_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create note.txt on the desktop"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus", mode="agent", task=task, item=item, attempt=attempt, metadata={}
            )
            assert plan is not None
            staged = Path(plan["staging_root"]) / "note.txt"
            staged.write_text("note\n", encoding="utf-8")
            permission = service.discover_staged_exports(attempt, item, plan)["permission"]
            denied = service.resolve(permission.request_id, allow=False)
            assert denied.permission.status == "denied"
            assert list(desktop.iterdir()) == []
            rejected = [
                artifact
                for artifact in store.list_artifacts(item.work_item_id)
                if artifact.kind == "business.export"
            ]
            assert len(rejected) == 1 and rejected[0].status == "rejected"


def test_desktop_application_text_is_not_treated_as_export_destination() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_intent_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Build a desktop application with a tkinter GUI inside this repository."
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=root / "Desktop")
            assert service.prepare_plan(
                provider="locus",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={},
            ) is None


def test_desktop_destination_detection_distinguishes_output_from_context() -> None:
    positive_tasks = (
        "开发一个国际象棋程序放桌面",
        "制作国际象棋游戏到桌面",
        "给我做个游戏放桌面",
        "生成 chess.py 到我的桌面",
        "Create chess.py on the Desktop",
        "Save the generated files to my Desktop",
        r"Export result.json to C:\Users\user-example\Desktop\result.json",
        "把结果保存到桌面",
        "请在桌面上创建 chess.py",
        "生成 chess.py 放在我的桌面上",
        "帮我的桌面上做一个画面精美的 Real Life 游戏",
        "把它移到桌面",
        "把生成的文件移动到我的桌面上",
    )
    negative_tasks = (
        "Build a desktop application inside this repository",
        "Create a Python tool to list files on the Desktop",
        "Make the game run on the Desktop",
        "Write docs about shortcuts on the Desktop",
        "Copy files from the Desktop into this repository",
        "Do not save anything to Desktop; keep it in the repository",
        r"Fix parser using C:\Users\user-example\Desktop\trace.log as reference",
        "创建一个读取桌面文件的 Python 脚本",
        "写一份说明，介绍桌面文件管理",
        "把桌面文件复制到项目目录",
        "不要导出到桌面",
    )
    for task in positive_tasks:
        assert WorkExportService._has_desktop_destination(task), task
    for task in negative_tasks:
        assert not WorkExportService._has_desktop_destination(task), task


def test_provider_paraphrase_cannot_mint_desktop_export_authority() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_source_authority_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "journey_timer.html をデスクトップへコピーする。"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=root / "Desktop")
            assert service.prepare_plan(
                provider="locus",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={"source_user_text": "不要复制到桌面。"},
            ) is None


def test_target_created_during_resolution_is_never_overwritten() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_race_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create race.txt on the Desktop"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus", mode="agent", task=task, item=item, attempt=attempt, metadata={}
            )
            assert plan is not None
            staged = Path(plan["staging_root"]) / "race.txt"
            staged.write_text("approved bytes\n", encoding="utf-8")
            permission = service.discover_staged_exports(attempt, item, plan)["permission"]
            target = desktop / "race.txt"
            real_os_open = os.open

            def race_open(path, flags, mode=0o777):
                target.write_text("user won the race\n", encoding="utf-8")
                return real_os_open(path, flags, mode)

            with patch("server.work_export_service.os.open", side_effect=race_open):
                try:
                    service.resolve(permission.request_id, allow=True)
                except WorkLedgerConflict as exc:
                    assert "appeared during approval" in str(exc)
                else:
                    raise AssertionError("a target race must fail closed")
            assert target.read_text(encoding="utf-8") == "user won the race\n"
            assert store.get_permission_request(permission.request_id).status == "allowed"  # type: ignore[union-attr]


def test_allowed_publish_interruption_is_verified_and_recoverable() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_recovery_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create recovery.txt on the Desktop"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus", mode="agent", task=task, item=item, attempt=attempt, metadata={}
            )
            assert plan is not None
            staged = Path(plan["staging_root"]) / "recovery.txt"
            staged.write_text("complete bytes\n", encoding="utf-8")
            permission = service.discover_staged_exports(attempt, item, plan)["permission"]
            original_publish = service._publish_atomic_no_replace

            def interrupt_after_atomic_publish(*args, **kwargs):
                original_publish(*args, **kwargs)
                raise RuntimeError("simulated process interruption")

            with patch.object(
                service,
                "_publish_atomic_no_replace",
                side_effect=interrupt_after_atomic_publish,
            ):
                try:
                    service.resolve(permission.request_id, allow=True)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("the simulated interruption must escape")

            target = desktop / "recovery.txt"
            assert target.read_text(encoding="utf-8") == "complete bytes\n"
            assert store.get_permission_request(permission.request_id).status == "allowed"  # type: ignore[union-attr]
            interrupted = store.get_attempt(attempt.attempt_id)
            assert interrupted is not None
            assert interrupted.metadata["export_resolution"]["status"] == "authorized_uncommitted"

            # An allowed request alone must not be reported as a completed
            # export.  A fresh process can safely resume from the immutable
            # scope, re-verify the exact target, and commit the ledger state.
            replay = service.discover_staged_exports(attempt, item, plan)
            assert replay["reason"] == "external_export_recovery_required"
            assert replay["recovery_required"] is True
            assert [
                artifact.status
                for artifact in store.list_artifacts(item.work_item_id)
                if artifact.kind == "business.export"
            ] == ["pending"]

            recovered = WorkExportService(store, desktop_path=desktop).resume_authorized(
                permission.request_id,
            )
            assert recovered.permission.status == "allowed"
            committed = store.get_attempt(attempt.attempt_id)
            assert committed is not None
            assert committed.metadata["export_resolution"]["status"] == "committed"
            assert [
                artifact.status
                for artifact in store.list_artifacts(item.work_item_id)
                if artifact.kind == "business.export"
            ] == ["approved"]
            assert not list(desktop.glob(".*.amadeus-*.tmp"))


def test_allowed_receipt_recovers_if_process_stops_before_attempt_journal() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_receipt_recovery_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create receipt-gap.txt on the Desktop"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={},
            )
            assert plan is not None
            staged = Path(plan["staging_root"]) / "receipt-gap.txt"
            staged.write_text("recover from the durable permission receipt\n", encoding="utf-8")
            permission = service.discover_staged_exports(attempt, item, plan)["permission"]
            real_update = store.update_attempt
            interrupted = False

            def stop_before_journal(attempt_id: str, **kwargs):
                nonlocal interrupted
                export_resolution = (kwargs.get("metadata") or {}).get("export_resolution")
                if (
                    not interrupted
                    and isinstance(export_resolution, dict)
                    and export_resolution.get("status") == "authorized"
                ):
                    interrupted = True
                    raise RuntimeError("simulated stop before attempt journal")
                return real_update(attempt_id, **kwargs)

            with patch.object(store, "update_attempt", side_effect=stop_before_journal):
                try:
                    service.resolve(permission.request_id, allow=True)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("the simulated journal interruption must escape")

            allowed = store.get_permission_request(permission.request_id)
            assert allowed is not None and allowed.status == "allowed"
            assert service.can_resume_authorized(allowed) is True
            assert not (desktop / "receipt-gap.txt").exists()
            recovered = service.resume_authorized(permission.request_id)
            assert recovered.exported_paths == (str(desktop / "receipt-gap.txt"),)
            assert (desktop / "receipt-gap.txt").read_text(encoding="utf-8") == (
                "recover from the durable permission receipt\n"
            )


def test_discovery_rejects_over_budget_text_without_permission() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_preview_guard_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            service = WorkExportService(store, desktop_path=desktop)
            invalid_payloads = (
                ("too_many_lines.txt", ("line\n" * 2401).encode("utf-8")),
            )
            for index, (filename, payload) in enumerate(invalid_payloads):
                workspace = root / f"workspace-{index}"
                task = f"Create {filename} on the Desktop"
                item, attempt = _records(store, workspace, task)
                plan = service.prepare_plan(
                    provider="locus",
                    mode="agent",
                    task=task,
                    item=item,
                    attempt=attempt,
                    metadata={
                        "external_export": {
                            "target": "desktop",
                            "filename": filename,
                        }
                    },
                )
                assert plan is not None
                (Path(plan["staging_root"]) / filename).write_bytes(payload)
                try:
                    service.discover_staged_exports(attempt, item, plan)
                except WorkLedgerConflict as exc:
                    assert "preview" in str(exc).lower() or "utf-8" in str(exc).lower()
                else:
                    raise AssertionError(
                        f"{filename} must fail closed when its complete preview is unavailable"
                    )
                assert store.list_permission_requests(item.work_item_id) == []


def test_binary_bundle_uses_immutable_identity_preview_and_exports_exact_bytes() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_binary_preview_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create a profile website with an image and export it to Desktop."
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="codex",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={"external_export": {"target": "desktop"}},
                provider_capabilities={
                    "workspace_access": "write",
                    "workspace_ownership": "caller",
                },
            )
            assert plan is not None
            stage = Path(plan["staging_root"])
            (stage / "index.html").write_text(
                '<!doctype html><img src="kurisu-hero.png">\n',
                encoding="utf-8",
            )
            image_bytes = b"\x89PNG\r\n\x1a\n\x00binary-image-payload"
            image_path = stage / "kurisu-hero.png"
            image_path.write_bytes(image_bytes)

            outcome = service.discover_staged_exports(attempt, item, plan)
            permission = outcome["permission"]
            assert permission is not None and permission.status == "pending"
            entries = {
                entry["staging_relative_path"]: entry
                for entry in permission.metadata["entries"]
            }
            image_entry = entries["kurisu-hero.png"]
            assert image_entry["preview_status"] == "binary_identity"
            assert image_entry["media_type_hint"] == "image/png"
            assert image_entry["size_bytes"] == len(image_bytes)
            assert image_entry["sha256"] == hashlib.sha256(image_bytes).hexdigest()
            assert entries["index.html"]["preview_status"] == "complete_text"
            assert permission.metadata["preview_version"] == 2
            assert permission.metadata["preview_complete"] is True
            assert "preview_binary_files" not in permission.metadata
            assert f"Desktop/{image_entry['relative_path']}" in outcome["changed_files"]
            assert "Binary file identity" in outcome["patch"]
            assert image_entry["sha256"] in outcome["patch"]
            assert not any(desktop.rglob("kurisu-hero.png"))

            resolution = service.resolve(permission.request_id, allow=True)
            exported_image = next(
                Path(path)
                for path in resolution.exported_paths
                if Path(path).name == "kurisu-hero.png"
            )
            assert exported_image.read_bytes() == image_bytes


def test_binary_export_rechecks_the_approved_hash_before_publication() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_binary_toctou_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            filename = "approved-image.png"
            task = f"Create {filename} on the Desktop"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="codex",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={
                    "external_export": {
                        "target": "desktop",
                        "filename": filename,
                    }
                },
            )
            assert plan is not None
            staged = Path(plan["staging_root"]) / filename
            staged.write_bytes(b"\x89PNG\r\n\x1a\napproved")
            permission = service.discover_staged_exports(attempt, item, plan)["permission"]
            assert permission is not None

            staged.write_bytes(b"\x89PNG\r\n\x1a\nchanged-after-approval")
            try:
                service.resolve(permission.request_id, allow=True)
            except WorkLedgerConflict as exc:
                assert "changed after approval" in str(exc)
            else:
                raise AssertionError("mutated binary bytes must not be exported")
            assert store.get_permission_request(permission.request_id).status == "pending"
            assert not (desktop / filename).exists()


def test_host_verified_opaque_assets_do_not_consume_text_preview_budget() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_host_opaque_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            service = WorkExportService(store, desktop_path=desktop)
            item, attempt = _records(
                store,
                root / "workspace",
                "Create a verified AUIP application bundle on the Desktop",
            )
            plan = service.prepare_plan(
                provider="codex",
                mode="agent",
                task=attempt.task,
                item=item,
                attempt=attempt,
                metadata={"external_export": {"target": "desktop"}},
            )
            assert plan is not None
            plan["requested_filename"] = ""
            plan["publication_shape"] = "bundle"
            plan["host_validates_auip_bundle"] = True
            stage = Path(plan["staging_root"])
            (stage / "index.html").write_text(
                "<!doctype html><title>Verified app</title>\n",
                encoding="utf-8",
            )
            (stage / "auip.manifest.json").write_text(
                '{"schema":"amadeus.auip/v0"}\n',
                encoding="utf-8",
            )
            opaque = ("official runtime line\n" * 2_500).encode("utf-8")
            (stage / "controller-v0.js").write_bytes(opaque)
            plan["host_materialized_files"] = ["controller-v0.js"]
            plan["host_materialized_assets"] = {
                "controller-v0.js": {
                    "sha256": hashlib.sha256(opaque).hexdigest(),
                    "size_bytes": len(opaque),
                }
            }

            outcome = service.discover_staged_exports(attempt, item, plan)
            permission = outcome["permission"]
            assert permission is not None
            entries = {
                entry["staging_relative_path"]: entry
                for entry in permission.metadata["entries"]
            }
            assert entries["controller-v0.js"]["preview_status"] == (
                "host_verified_opaque"
            )
            assert entries["controller-v0.js"]["host_materialized"] is True
            assert "controller-v0.js" not in outcome["patch"]
            opaque_relative = entries["controller-v0.js"]["relative_path"]
            assert permission.metadata["preview_opaque_files"] == [
                f"Desktop/{opaque_relative}"
            ]
            assert all(
                "controller-v0.js" not in value
                for value in outcome["changed_files"]
            )
            resolution = service.resolve(permission.request_id, allow=True)
            assert any(
                Path(path).name == "controller-v0.js"
                for path in resolution.exported_paths
            )


def test_requested_file_excludes_neighboring_runtime_byproducts() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_requested_file_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create chess_game.py on the Desktop"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={
                    "external_export": {
                        "target": "desktop",
                        "filename": "chess_game.py",
                    }
                },
            )
            assert plan is not None
            staging_root = Path(plan["staging_root"])
            requested = staging_root / "chess_game.py"
            requested.write_text("print('ready')\n", encoding="utf-8")
            cache_file = staging_root / "__pycache__" / "chess_game.cpython-312.pyc"
            cache_file.parent.mkdir()
            cache_file.write_bytes(b"\x00runtime byproduct")

            outcome = service.discover_staged_exports(attempt, item, plan)
            permission = outcome["permission"]
            assert permission is not None
            assert permission.scope_paths[0] == str(desktop / "chess_game.py")
            assert permission.scope_paths[1] == permission.metadata["entries"][0]["temporary_path"]
            assert outcome["changed_files"] == ["Desktop/chess_game.py"]
            assert "__pycache__" not in outcome["patch"]
            assert ".pyc" not in outcome["patch"]

            service.resolve(permission.request_id, allow=True)
            assert (desktop / "chess_game.py").read_text(encoding="utf-8") == "print('ready')\n"
            assert not (desktop / "__pycache__").exists()
            assert sorted(path.name for path in desktop.iterdir()) == ["chess_game.py"]


def test_nested_application_bundle_is_previewed_permissioned_and_exported() -> None:
    """A multi-file app may remain one named Desktop deliverable directory."""

    with tempfile.TemporaryDirectory(prefix="work_export_bundle_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create the Infinite Tower application and export it to Desktop."
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="future_workspace_provider",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={"external_export": {"target": "desktop"}},
                provider_capabilities={
                    "workspace_access": "write",
                    "workspace_ownership": "caller",
                },
            )
            assert plan is not None
            staging_root = Path(plan["staging_root"])
            bundle = staging_root / "Infinite Tower"
            bundle.mkdir()
            (bundle / "infinite_tower.py").write_text(
                "print('tower ready')\n",
                encoding="utf-8",
            )
            (bundle / "README.md").write_text("# Infinite Tower\n", encoding="utf-8")
            configs = bundle / "configs"
            configs.mkdir()
            for index in range(7):
                (configs / f"floor-{index}.txt").write_text(
                    f"floor={index}\n",
                    encoding="utf-8",
                )
            runtime_cache = bundle / "__pycache__" / "infinite_tower.cpython-312.pyc"
            runtime_cache.parent.mkdir()
            runtime_cache.write_bytes(b"\x00runtime byproduct")

            outcome = service.discover_staged_exports(attempt, item, plan)
            permission = outcome["permission"]
            assert permission is not None and permission.status == "pending"
            assert len(outcome["changed_files"]) == 9
            assert {
                "Desktop/Infinite Tower/infinite_tower.py",
                "Desktop/Infinite Tower/README.md",
                "Desktop/Infinite Tower/configs/floor-6.txt",
            }.issubset(set(outcome["changed_files"]))
            assert "+print('tower ready')" in outcome["patch"]
            assert "__pycache__" not in outcome["patch"]
            assert permission.metadata["directory_paths"] == [
                str(desktop / "Infinite Tower"),
                str(desktop / "Infinite Tower" / "configs"),
            ]
            assert permission.scope_paths[0] == str(desktop / "Infinite Tower")
            assert not (desktop / "Infinite Tower").exists()

            resolution = service.resolve(permission.request_id, allow=True)

            assert resolution.permission.status == "allowed"
            assert (desktop / "Infinite Tower" / "README.md").read_text(
                encoding="utf-8"
            ) == "# Infinite Tower\n"
            assert (desktop / "Infinite Tower" / "infinite_tower.py").read_text(
                encoding="utf-8"
            ) == "print('tower ready')\n"
            assert len(list((desktop / "Infinite Tower" / "configs").iterdir())) == 7
            assert not (desktop / "Infinite Tower" / "__pycache__").exists()


def test_nested_bundles_remain_isolated_by_attempt_and_never_merge() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_bundle_attempts_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        desktop = root / "Desktop"
        workspace.mkdir()
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            project = store.create_or_get_project(workspace)
            item = store.create_work_item(
                project.project_id,
                title="Infinite Tower",
                goal="Export Infinite Tower to Desktop.",
            )
            first = store.create_attempt(
                item.work_item_id,
                provider="provider-a",
                task=item.goal,
            )
            service = WorkExportService(store, desktop_path=desktop)

            def stage(attempt, content: str):
                plan = service.prepare_plan(
                    provider=attempt.provider,
                    mode="agent",
                    task=item.goal,
                    item=item,
                    attempt=attempt,
                    metadata={"external_export": {"target": "desktop"}},
                    provider_capabilities={
                        "workspace_access": "write",
                        "workspace_ownership": "caller",
                    },
                )
                assert plan is not None
                bundle = Path(plan["staging_root"]) / "Infinite Tower"
                bundle.mkdir()
                (bundle / "game.py").write_text(content, encoding="utf-8")
                return plan

            first_plan = stage(first, "print('first batch')\n")
            first_outcome = service.discover_staged_exports(first, item, first_plan)
            store.update_attempt(first.attempt_id, execution_status="succeeded")
            second = store.create_attempt(
                item.work_item_id,
                provider="provider-b",
                task=item.goal,
            )
            second_plan = stage(second, "print('second batch')\n")
            second_outcome = service.discover_staged_exports(second, item, second_plan)
            first_permission = first_outcome["permission"]
            second_permission = second_outcome["permission"]
            assert first_permission is not None and second_permission is not None
            assert first_permission.request_id != second_permission.request_id
            assert first_permission.attempt_id == first.attempt_id
            assert second_permission.attempt_id == second.attempt_id
            assert (
                first_permission.metadata["entries"][0]["source_path"]
                != second_permission.metadata["entries"][0]["source_path"]
            )

            service.resolve(first_permission.request_id, allow=True)
            try:
                service.resolve(second_permission.request_id, allow=True)
            except WorkLedgerConflict as exc:
                assert "will not be merged" in str(exc)
            else:
                raise AssertionError("a later attempt must not merge into an earlier batch")
            assert store.get_permission_request(second_permission.request_id).status == "pending"  # type: ignore[union-attr]
            assert (desktop / "Infinite Tower" / "game.py").read_text(
                encoding="utf-8"
            ) == "print('first batch')\n"


def test_nested_bundle_resumes_only_its_authorized_partial_publication() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_bundle_recovery_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Export the application bundle to Desktop."
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="provider-a",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={"external_export": {"target": "desktop"}},
            )
            assert plan is not None
            bundle = Path(plan["staging_root"]) / "Bundle"
            bundle.mkdir()
            (bundle / "a.txt").write_text("a\n", encoding="utf-8")
            (bundle / "b.txt").write_text("b\n", encoding="utf-8")
            permission = service.discover_staged_exports(attempt, item, plan)[
                "permission"
            ]
            assert permission is not None
            publish = service._publish_atomic_no_replace
            publication_count = 0

            def interrupt_after_first_file(*args, **kwargs):
                nonlocal publication_count
                publish(*args, **kwargs)
                publication_count += 1
                if publication_count == 1:
                    raise RuntimeError("simulated interruption")

            with patch.object(
                service,
                "_publish_atomic_no_replace",
                side_effect=interrupt_after_first_file,
            ):
                try:
                    service.resolve(permission.request_id, allow=True)
                except RuntimeError as exc:
                    assert "simulated interruption" in str(exc)
                else:
                    raise AssertionError("the simulated interruption must escape")

            interrupted = store.get_permission_request(permission.request_id)
            assert interrupted is not None and interrupted.status == "allowed"
            recovered = WorkExportService(store, desktop_path=desktop).resume_authorized(
                permission.request_id
            )
            assert recovered.permission.status == "allowed"
            assert (desktop / "Bundle" / "a.txt").read_text(encoding="utf-8") == "a\n"
            assert (desktop / "Bundle" / "b.txt").read_text(encoding="utf-8") == "b\n"


def test_staged_export_is_provider_neutral() -> None:
    """The host export boundary follows the workspace contract, not an id."""

    with tempfile.TemporaryDirectory(prefix="work_export_provider_neutral_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create result.txt on my Desktop"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="future_workspace_provider",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={"external_export": {"target": "desktop"}},
                provider_capabilities={
                    "workspace_access": "write",
                    "workspace_ownership": "caller",
                },
            )

            assert plan is not None
            assert Path(plan["staging_root"]).is_relative_to(
                Path(item.workspace_path).resolve()
            )
            assert Path(plan["target_root"]) == desktop.resolve()
            assert plan["provider"] == "future_workspace_provider"

            try:
                service.prepare_plan(
                    provider="non_workspace_provider",
                    mode="agent",
                    task=task,
                    item=item,
                    attempt=attempt,
                    metadata={"external_export": {"target": "desktop"}},
                    provider_capabilities={
                        "workspace_access": "none",
                        "workspace_ownership": "none",
                    },
                )
            except WorkLedgerConflict as exc:
                assert "host-controlled writable workspace" in str(exc)
            else:
                raise AssertionError(
                    "an external export must require a host-controlled writable workspace"
                )


def test_discovery_rejects_more_than_six_unspecified_top_level_files() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_file_limit_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Deliver the generated files"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={"external_export": {"target": "desktop"}},
            )
            assert plan is not None
            staging_root = Path(plan["staging_root"])
            for index in range(7):
                (staging_root / f"part-{index}.txt").write_text(
                    f"part {index}\n",
                    encoding="utf-8",
                )

            try:
                service.discover_staged_exports(attempt, item, plan)
            except WorkLedgerConflict as exc:
                assert "bounded top-level" in str(exc)
            else:
                raise AssertionError("seven unspecified top-level exports must be rejected")
            assert store.list_permission_requests(item.work_item_id) == []


def test_proposed_exports_link_or_junction_escape_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_reparse_guard_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        outside = root / "outside"
        outside.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create escape.txt on the Desktop"
            item, attempt = _records(store, workspace, task)
            private_root = workspace / ".amadeus"
            private_root.mkdir()
            proposed = private_root / "proposed_exports"
            used_real_link = False
            try:
                os.symlink(outside, proposed, target_is_directory=True)
                used_real_link = True
            except (NotImplementedError, OSError):
                proposed.mkdir()

            service = WorkExportService(store, desktop_path=root / "Desktop")

            def prepare() -> None:
                service.prepare_plan(
                    provider="locus",
                    mode="agent",
                    task=task,
                    item=item,
                    attempt=attempt,
                    metadata={},
                )

            if used_real_link:
                try:
                    prepare()
                except WorkLedgerConflict as exc:
                    assert "symlink or junction" in str(exc)
                else:
                    raise AssertionError("a real staging-directory link must be rejected")
            else:
                proposed_key = os.path.normcase(os.path.abspath(proposed))

                def is_junction(path: Path) -> bool:
                    return os.path.normcase(os.path.abspath(path)) == proposed_key

                with patch.object(Path, "is_junction", new=is_junction, create=True):
                    try:
                        prepare()
                    except WorkLedgerConflict as exc:
                        assert "symlink or junction" in str(exc)
                    else:
                        raise AssertionError("a staging-directory junction must be rejected")

            assert not (outside / attempt.attempt_id).exists()
            assert store.list_permission_requests(item.work_item_id) == []


def test_committed_allow_once_cannot_be_replayed_after_target_deletion() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_allow_once_") as temp:
        root = Path(temp)
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create receipt.txt on the Desktop"
            item, attempt = _records(store, root / "workspace", task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={},
            )
            assert plan is not None
            staged = Path(plan["staging_root"]) / "receipt.txt"
            staged.write_text("approved once\n", encoding="utf-8")
            permission = service.discover_staged_exports(attempt, item, plan)["permission"]
            assert permission is not None
            service.resolve(permission.request_id, allow=True)

            target = desktop / "receipt.txt"
            target.unlink()
            drift = service.discover_staged_exports(attempt, item, plan)
            assert drift["reason"] == "external_export_drift"
            assert drift["recovery_required"] is False
            try:
                WorkExportService(store, desktop_path=desktop).resume_authorized(
                    permission.request_id
                )
            except WorkLedgerConflict as exc:
                assert "recoverable transaction journal" in str(exc)
            else:
                raise AssertionError("a committed allow-once grant must never be replayed")
            assert not target.exists()
            committed = store.get_attempt(attempt.attempt_id)
            assert committed is not None
            assert committed.metadata["export_resolution"]["status"] == "committed"


def test_junction_detection_falls_back_to_reparse_attribute() -> None:
    class LegacyWindowsPath:
        def is_symlink(self) -> bool:
            return False

        def lstat(self):
            class Info:
                st_file_attributes = 0x400

            return Info()

    assert WorkExportService._is_link_or_junction(LegacyWindowsPath()) is True  # type: ignore[arg-type]


def test_private_workspace_exports_are_hidden_from_git_status() -> None:
    with tempfile.TemporaryDirectory(prefix="work_export_gitignore_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        workspace.mkdir()
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        desktop = root / "Desktop"
        desktop.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            task = "Create hidden.txt on the Desktop"
            item, attempt = _records(store, workspace, task)
            service = WorkExportService(store, desktop_path=desktop)
            plan = service.prepare_plan(
                provider="locus",
                mode="agent",
                task=task,
                item=item,
                attempt=attempt,
                metadata={},
            )
            assert plan is not None
            (Path(plan["staging_root"]) / "hidden.txt").write_text(
                "private staging\n",
                encoding="utf-8",
            )

        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert ".amadeus" not in status
        assert status.strip() == ""


def _main() -> None:
    test_desktop_task_is_staged_then_exported_only_after_exact_approval()
    test_approval_rejects_changed_source_existing_target_and_path_escape()
    test_denial_is_durable_and_creates_no_desktop_file()
    test_desktop_application_text_is_not_treated_as_export_destination()
    test_desktop_destination_detection_distinguishes_output_from_context()
    test_provider_paraphrase_cannot_mint_desktop_export_authority()
    test_natural_auip_amend_inherits_approved_identity_not_model_filename()
    test_auip_amend_inherits_one_approved_multifile_revision_as_a_unit()
    test_target_created_during_resolution_is_never_overwritten()
    test_allowed_publish_interruption_is_verified_and_recoverable()
    test_allowed_receipt_recovers_if_process_stops_before_attempt_journal()
    test_discovery_rejects_over_budget_text_without_permission()
    test_binary_bundle_uses_immutable_identity_preview_and_exports_exact_bytes()
    test_binary_export_rechecks_the_approved_hash_before_publication()
    test_requested_file_excludes_neighboring_runtime_byproducts()
    test_nested_application_bundle_is_previewed_permissioned_and_exported()
    test_nested_bundles_remain_isolated_by_attempt_and_never_merge()
    test_nested_bundle_resumes_only_its_authorized_partial_publication()
    test_discovery_rejects_more_than_six_unspecified_top_level_files()
    test_proposed_exports_link_or_junction_escape_is_rejected()
    test_committed_allow_once_cannot_be_replayed_after_target_deletion()
    test_junction_detection_falls_back_to_reparse_attribute()
    test_private_workspace_exports_are_hidden_from_git_status()
    print("ok: Desktop exports require immutable staged-file approval")


if __name__ == "__main__":
    _main()
