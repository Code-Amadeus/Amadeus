from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.project_registry import (
    cwd_in_project_registry,
    project_registry_config,
)
from server.scratch_workspace import ensure_scratch_root
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _git(cwd: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")


def test_host_trust_root_is_the_only_online_project_registry() -> None:
    with tempfile.TemporaryDirectory(prefix="project_registry_split_") as temp:
        root = Path(temp)
        host_project = root / "host-project"
        unlisted_project = root / "unlisted-project"
        host_project.mkdir()
        unlisted_project.mkdir()
        previous_host = settings.WORK_PROJECT_ALLOWLIST
        try:
            settings.WORK_PROJECT_ALLOWLIST = str(host_project)
            config = project_registry_config()
            assert config.source == "work_project_allowlist"
            assert config.roots == (str(host_project),)
            assert cwd_in_project_registry(str(host_project)) is True
            assert cwd_in_project_registry(str(unlisted_project)) is False
        finally:
            settings.WORK_PROJECT_ALLOWLIST = previous_host


def test_project_registry_accepts_linked_worktrees() -> None:
    with tempfile.TemporaryDirectory(prefix="project_registry_worktree_") as temp:
        root = Path(temp)
        project = root / "project"
        worktree = root / "linked-worktree"
        project.mkdir()
        _git(project, "init", "--quiet")
        _git(project, "config", "user.email", "amadeus-test@example.invalid")
        _git(project, "config", "user.name", "Amadeus Test")
        (project / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git(project, "add", "seed.txt")
        _git(project, "commit", "--quiet", "-m", "seed")
        _git(project, "worktree", "add", "-b", "registry-test", str(worktree))
        previous_host = settings.WORK_PROJECT_ALLOWLIST
        try:
            settings.WORK_PROJECT_ALLOWLIST = str(project)
            assert cwd_in_project_registry(str(worktree)) is True
        finally:
            settings.WORK_PROJECT_ALLOWLIST = previous_host
            _git(project, "worktree", "remove", "--force", str(worktree))


def test_scratch_is_host_trusted_without_any_deployment_root() -> None:
    with tempfile.TemporaryDirectory(prefix="project_registry_scratch_") as temp:
        previous_host = settings.WORK_PROJECT_ALLOWLIST
        previous_scratch = settings.WORK_SCRATCH_ROOT
        try:
            settings.WORK_PROJECT_ALLOWLIST = ""
            settings.WORK_SCRATCH_ROOT = str(Path(temp) / "scratch")
            scratch = ensure_scratch_root()
            config = project_registry_config()
            assert config.source == "none"
            assert cwd_in_project_registry(str(scratch)) is True
        finally:
            settings.WORK_PROJECT_ALLOWLIST = previous_host
            settings.WORK_SCRATCH_ROOT = previous_scratch


def test_session_focus_cannot_bypass_project_registry() -> None:
    with tempfile.TemporaryDirectory(prefix="project_registry_focus_") as temp:
        root = Path(temp)
        trusted = root / "trusted"
        untrusted = root / "untrusted"
        trusted.mkdir()
        untrusted.mkdir()
        previous_host = settings.WORK_PROJECT_ALLOWLIST
        try:
            settings.WORK_PROJECT_ALLOWLIST = str(trusted)
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                trusted_project = store.create_or_get_project(trusted)
                untrusted_project = store.create_or_get_project(untrusted)
                chosen = coordinator.set_session_project(
                    "registry-session",
                    trusted_project.project_id,
                )
                assert chosen["projectId"] == trusted_project.project_id
                try:
                    coordinator.set_session_project(
                        "registry-session",
                        untrusted_project.project_id,
                    )
                except WorkLedgerConflict as exc:
                    assert "outside the trusted registry" in str(exc)
                else:
                    raise AssertionError("focus accepted an untrusted Project")
                settings.WORK_PROJECT_ALLOWLIST = str(untrusted)
                assert coordinator.session_project("registry-session") == ""
        finally:
            settings.WORK_PROJECT_ALLOWLIST = previous_host


def test_new_project_cannot_expand_the_host_trust_root() -> None:
    with tempfile.TemporaryDirectory(prefix="project_registry_create_") as temp:
        root = Path(temp)
        trusted = root / "trusted"
        untrusted = root / "untrusted"
        trusted.mkdir()
        untrusted.mkdir()
        previous_host = settings.WORK_PROJECT_ALLOWLIST
        try:
            settings.WORK_PROJECT_ALLOWLIST = str(trusted)
            with WorkLedgerStore(root / "ledger.sqlite3") as store:
                coordinator = WorkLedgerCoordinator(store)
                created = coordinator.create_project(str(trusted))
                assert created["created"] is True
                assert created["workspacePath"] == str(trusted.resolve())
                try:
                    coordinator.create_project(str(untrusted))
                except WorkLedgerConflict as exc:
                    assert str(exc) == "workspace_outside_project_registry"
                else:
                    raise AssertionError("Project creation expanded the trusted registry")
        finally:
            settings.WORK_PROJECT_ALLOWLIST = previous_host


def _main() -> None:
    test_host_trust_root_is_the_only_online_project_registry()
    test_project_registry_accepts_linked_worktrees()
    test_scratch_is_host_trusted_without_any_deployment_root()
    test_session_focus_cannot_bypass_project_registry()
    test_new_project_cannot_expand_the_host_trust_root()
    print("ok: Amadeus Project Registry owns host trust")


if __name__ == "__main__":
    _main()
