"""Baseline-aware business artifact and historical diff tests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.work_ledger_store import WorkLedgerStore
from server.work_artifact_registry import WorkArtifactRegistry


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


@pytest.mark.parametrize('git_workspace', [False, True])
def test_amend_recovery_keeps_earlier_deliverable_ownership(tmp_path, git_workspace):
    from test_work_recovery_input_requirements import _marker, _recovery
    from agent_host.work_ledger_store import PROVIDER_RECOVERY_METADATA_KEYS

    async def run():
        root = tmp_path / 'workspace'
        root.mkdir()
        if git_workspace:
            _git(root, 'init')
            _git(root, 'config', 'user.email', 'test@example.invalid')
            _git(root, 'config', 'user.name', 'Synthetic Test')
            (root / 'README.md').write_text('baseline')
            _git(root, 'add', '.')
            _git(root, 'commit', '-m', 'baseline')
        (root / 'user.txt').write_text('unrelated pre-existing work')
        with WorkLedgerStore(tmp_path / 'ledger.sqlite3') as store:
            project = store.create_or_get_project(root)
            item = store.create_work_item(project.project_id, title='Application', workspace_path=root)
            registry = WorkArtifactRegistry(store)
            _, first = store.create_operation_attempt(item.work_item_id, intent='execute',
                instruction='Create application', provider='codex', task='Create application')
            await registry.capture_baseline(first, item)
            (root / 'manifest.json').write_text('{"version":1}')
            (root / 'index.html').write_text('<p>v1</p>')
            await registry.finalize_attempt(store.get_attempt(first.attempt_id), item)
            store.update_attempt(first.attempt_id, execution_status='succeeded')
            operation, amendment = store.create_operation_attempt(item.work_item_id, intent='amend',
                instruction='Update application', provider='codex', task='Update application',
                attempt_metadata={'intent': 'amend'})
            await registry.capture_baseline(amendment, item)
            (root / 'index.html').write_text('<p>v2 requires updated manifest</p>')
            await registry.finalize_attempt(store.get_attempt(amendment.attempt_id), item)
            amendment = store.update_attempt(amendment.attempt_id, execution_status='succeeded')
            reason = 'auip_validation_failed'
            recovery = store.create_attempt(item.work_item_id, operation_id=operation.operation_id,
                provider='codex', task='Repair manifest', provider_run_id='repair-run',
                metadata={'intent': 'amend', 'provider_recovery': _recovery(reason, amendment).to_dict()})
            recovery = store.update_attempt(recovery.attempt_id, execution_status='running')
            store.update_attempt(amendment.attempt_id, metadata={
                PROVIDER_RECOVERY_METADATA_KEYS[reason]: _marker(reason, amendment, recovery)})
            assert store.get_recovery_predecessor(recovery) is not None
            baseline = await registry.capture_baseline(recovery, item)
            assert set(baseline['lineage_owned_dirty_paths']) == {'manifest.json', 'index.html'}
            (root / 'manifest.json').write_text('{"version":2}')
            delta = await registry.finalize_attempt(store.get_attempt(recovery.attempt_id), item)
            assert delta['ambiguous_paths'] == []
            assert delta['conflicts'] == []
            manifest, = [artifact for artifact in store.list_artifacts(item.work_item_id,
                attempt_id=recovery.attempt_id) if artifact.metadata.get('relative_path') == 'manifest.json']
            assert manifest.status == 'registered'
            assert manifest.metadata['attribution'] == 'lineage_amendment'

    asyncio.run(run())


@pytest.mark.parametrize('reason', ['auip_validation_failed', 'progress_only_completion'])
@pytest.mark.parametrize('changed_between_attempts', [False, True])
def test_recovery_inherits_only_unchanged_predecessor_file_facts(tmp_path, reason, changed_between_attempts):
    from test_work_recovery_input_requirements import _seed_parent, _add_successor

    async def run():
        with WorkLedgerStore(tmp_path / 'ledger.sqlite3') as store:
            item, operation, parent, _ = _seed_parent(store, tmp_path)
            root = Path(item.workspace_path)
            owned = root / 'app.txt'
            owned.write_bytes(b'failed application')
            (root / 'user.txt').write_bytes(b'unrelated dirty file')
            store.register_artifact(item.work_item_id, attempt_id=parent.attempt_id,
                kind='business.file', path=owned, sha256=hashlib.sha256(owned.read_bytes()).hexdigest(),
                metadata={'relative_path': 'app.txt'})
            if changed_between_attempts:
                owned.write_bytes(b'user changed this before recovery')
            _, successor = _add_successor(store, item, operation, parent, reason=reason)
            registry = WorkArtifactRegistry(store)
            baseline = await registry.capture_baseline(successor, item)
            assert baseline.get('lineage_owned_dirty_paths', []) == ([] if changed_between_attempts else ['app.txt'])
            owned.write_bytes(b'repaired application')
            (root / 'user.txt').write_bytes(b'attempt changed unrelated dirty file')
            delta = await registry.finalize_attempt(store.get_attempt(successor.attempt_id), item)
            assert 'user.txt' in delta['ambiguous_paths']
            assert ('app.txt' in delta['ambiguous_paths']) is changed_between_attempts

    asyncio.run(run())


def test_committed_delta_survives_and_baseline_dirty_is_not_stolen() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_artifact_git_") as temp:
            root = Path(temp) / "project"
            root.mkdir()
            _git(root, "init")
            _git(root, "config", "user.email", "amadeus-test@example.invalid")
            _git(root, "config", "user.name", "Amadeus Test")
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")
            (root / "preexisting.txt").write_text("clean\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "baseline")

            # Dirty before the attempt and left untouched during it. This file
            # must remain visible as excluded baseline, not a task artifact.
            (root / "preexisting.txt").write_text("dirty before run\n", encoding="utf-8")
            store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
            project = store.create_or_get_project(root)
            item = store.create_work_item(project.project_id, title="Generate committed file")
            attempt = store.create_attempt(item.work_item_id, provider="locus", task="Generate file")
            registry = WorkArtifactRegistry(store)
            baseline = await registry.capture_baseline(attempt, item)

            (root / "generated.py").write_text("print('generated')\n", encoding="utf-8")
            _git(root, "add", "generated.py")
            _git(root, "commit", "-m", "provider generated file")
            (root / "working.txt").write_text("not committed\n", encoding="utf-8")

            current_attempt = store.get_attempt(attempt.attempt_id)
            assert current_attempt is not None
            delta = await registry.finalize_attempt(current_attempt, item)
            assert delta["available"] is True
            assert "generated.py" in delta["changed_files"]
            assert "working.txt" in delta["changed_files"]
            assert "preexisting.txt" not in delta["changed_files"]
            assert "preexisting.txt" in delta["excluded_baseline_paths"]
            assert delta["head_changed"] is True
            assert "generated.py" in delta["patch"]
            assert "working.txt" in delta["patch"]
            assert "+not committed" in delta["patch"]
            assert delta["untracked_patch_omissions"] == []

            persisted = registry.delta_for_attempt(attempt.attempt_id)
            assert persisted is not None
            assert persisted["baseline_head"] == baseline["head"]
            artifacts = store.list_artifacts(item.work_item_id, attempt_id=attempt.attempt_id)
            assert any(artifact.kind == "git.delta" for artifact in artifacts)
            assert any(artifact.title == "generated.py" for artifact in artifacts)
            store.close()

    asyncio.run(run())


def test_changed_preexisting_dirty_path_is_ambiguous_not_claimed_exact() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_artifact_ambiguous_") as temp:
            root = Path(temp) / "project"
            root.mkdir()
            _git(root, "init")
            _git(root, "config", "user.email", "amadeus-test@example.invalid")
            _git(root, "config", "user.name", "Amadeus Test")
            target = root / "shared.py"
            target.write_text("value = 1\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "baseline")
            target.write_text("value = 2\n", encoding="utf-8")

            store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
            project = store.create_or_get_project(root)
            item = store.create_work_item(project.project_id, title="Touch shared file")
            attempt = store.create_attempt(item.work_item_id, provider="locus", task="Edit shared.py")
            registry = WorkArtifactRegistry(store)
            await registry.capture_baseline(attempt, item)
            target.write_text("value = 3\n", encoding="utf-8")

            current_attempt = store.get_attempt(attempt.attempt_id)
            assert current_attempt is not None
            delta = await registry.finalize_attempt(current_attempt, item)
            assert delta["ambiguous_paths"] == ["shared.py"]
            assert delta["conflicts"]
            artifact = next(
                artifact
                for artifact in store.list_artifacts(item.work_item_id, attempt_id=attempt.attempt_id)
                if artifact.title == "shared.py"
            )
            assert artifact.status == "pending"
            assert artifact.metadata["attribution"] == "ambiguous_origin"
            store.close()

    asyncio.run(run())


def test_non_ascii_entry_revision_is_registered_as_the_real_path() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_artifact_unicode_") as temp:
            root = Path(temp) / "project"
            root.mkdir()
            _git(root, "init")
            _git(root, "config", "user.email", "amadeus-test@example.invalid")
            _git(root, "config", "user.name", "Amadeus Test")
            target = root / "区域防御响应模拟器.html"
            target.write_text("base\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "baseline")

            store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
            project = store.create_or_get_project(root)
            item = store.create_work_item(project.project_id, title="Adapt application")
            attempt = store.create_attempt(
                item.work_item_id,
                provider="codex",
                task="Add integration",
            )
            registry = WorkArtifactRegistry(store)
            await registry.capture_baseline(attempt, item)
            target.write_text("adapted\n", encoding="utf-8")

            current_attempt = store.get_attempt(attempt.attempt_id)
            assert current_attempt is not None
            delta = await registry.finalize_attempt(current_attempt, item)
            assert delta["changed_files"] == ["区域防御响应模拟器.html"]
            assert delta["file_facts"]["区域防御响应模拟器.html"]["exists"] is True
            artifact = next(
                artifact
                for artifact in store.list_artifacts(
                    item.work_item_id,
                    attempt_id=attempt.attempt_id,
                )
                if artifact.kind == "business.file"
            )
            assert artifact.path == str(target.resolve())
            assert artifact.status == "registered"
            store.close()

    asyncio.run(run())


def test_non_git_workspace_registers_new_files_without_claiming_preexisting_changes() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_artifact_filesystem_") as temp:
            root = Path(temp)/"project"
            root.mkdir()
            marker = root/"marker.txt"
            marker.write_text("before\n", encoding="utf-8")
            store = WorkLedgerStore(Path(temp)/"ledger.sqlite3")
            project = store.create_or_get_project(root)
            item = store.create_work_item(project.project_id,
                title="Generate a report", workspace_path=str(root))
            attempt = store.create_attempt(item.work_item_id,
                provider="locus", task="Generate report.md")
            registry = WorkArtifactRegistry(store)
            baseline = await registry.capture_baseline(attempt, item)
            assert baseline["available"] is True
            assert baseline["source"] == "filesystem"
            assert set(baseline["files"]) == {"marker.txt"}

            report = root/"report.md"
            report.write_text("delivered\n", encoding="utf-8")
            marker.write_text("changed during attempt\n", encoding="utf-8")
            current_attempt = store.get_attempt(attempt.attempt_id)
            assert current_attempt is not None
            delta = await registry.finalize_attempt(current_attempt, item)
            assert delta["source"] == "filesystem"
            assert delta["changed_files"] == ["marker.txt", "report.md"]
            assert delta["ambiguous_paths"] == ["marker.txt"]
            assert delta["untracked"] == ["report.md"]
            artifacts = store.list_artifacts(item.work_item_id,
                attempt_id=attempt.attempt_id)
            assert any(artifact.kind == "filesystem.delta" for artifact in artifacts)
            report_artifact = next(artifact for artifact in artifacts
                if artifact.title == "report.md")
            assert report_artifact.status == "registered"
            assert report_artifact.sha256
            marker_artifact = next(artifact for artifact in artifacts
                if artifact.title == "marker.txt")
            assert marker_artifact.status == "pending"
            assert marker_artifact.metadata["attribution"] == "ambiguous_origin"
            store.close()

    asyncio.run(run())


def test_explicit_amend_may_change_predecessor_owned_dirty_artifact() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="work_artifact_amend_lineage_") as temp:
            root = Path(temp) / "project"
            root.mkdir()
            _git(root, "init")
            _git(root, "config", "user.email", "amadeus-test@example.invalid")
            _git(root, "config", "user.name", "Amadeus Test")
            (root / "README.md").write_text("base\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "baseline")

            store = WorkLedgerStore(Path(temp) / "ledger.sqlite3")
            project = store.create_or_get_project(root)
            predecessor = store.create_work_item(
                project.project_id,
                title="Create game",
                workspace_path=str(root),
            )
            predecessor_attempt = store.create_attempt(
                predecessor.work_item_id,
                provider="locus",
                task="Create game.py",
            )
            target = root / "game.py"
            target.write_text("players = 1\n", encoding="utf-8")
            store.register_artifact(
                predecessor.work_item_id,
                attempt_id=predecessor_attempt.attempt_id,
                kind="business.file",
                title="game.py",
                path=str(target),
                identity="git.path:game.py",
                status="registered",
                metadata={
                    "relative_path": "game.py",
                    "attribution": "workspace_window",
                    "exists": True,
                },
            )

            amendment = store.create_work_item(
                project.project_id,
                title="Make it multiplayer",
                workspace_path=str(root),
                metadata={
                    "intent": "amend",
                    "related_work_item_id": predecessor.work_item_id,
                },
            )
            attempt = store.create_attempt(
                amendment.work_item_id,
                provider="locus",
                task="Make game.py multiplayer",
                metadata={
                    "intent": "amend",
                    "related_work_item_id": predecessor.work_item_id,
                },
            )
            registry = WorkArtifactRegistry(store)
            baseline = await registry.capture_baseline(attempt, amendment)
            assert baseline["lineage_owned_dirty_paths"] == ["game.py"]
            target.write_text("players = 2\n", encoding="utf-8")

            current_attempt = store.get_attempt(attempt.attempt_id)
            assert current_attempt is not None
            delta = await registry.finalize_attempt(current_attempt, amendment)
            assert delta["changed_files"] == ["game.py"]
            assert delta["lineage_owned_paths"] == ["game.py"]
            assert delta["ambiguous_paths"] == []
            assert delta["conflicts"] == []
            artifact = next(
                artifact
                for artifact in store.list_artifacts(
                    amendment.work_item_id,
                    attempt_id=attempt.attempt_id,
                )
                if artifact.title == "game.py"
            )
            assert artifact.status == "registered"
            assert artifact.metadata["attribution"] == "lineage_amendment"
            store.close()

    asyncio.run(run())


def _main() -> None:
    test_committed_delta_survives_and_baseline_dirty_is_not_stolen()
    test_changed_preexisting_dirty_path_is_ambiguous_not_claimed_exact()
    test_non_ascii_entry_revision_is_registered_as_the_real_path()
    test_non_git_workspace_registers_new_files_without_claiming_preexisting_changes()
    test_explicit_amend_may_change_predecessor_owned_dirty_artifact()
    print("ok: work artifact registry preserves attempt Git boundaries")


if __name__ == "__main__":
    _main()
