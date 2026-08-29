"""Baseline-aware business artifact and historical diff tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

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
    test_explicit_amend_may_change_predecessor_owned_dirty_artifact()
    print("ok: work artifact registry preserves attempt Git boundaries")


if __name__ == "__main__":
    _main()
