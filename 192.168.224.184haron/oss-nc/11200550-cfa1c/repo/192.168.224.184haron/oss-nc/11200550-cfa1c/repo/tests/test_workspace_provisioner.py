"""Real Git evidence for the Host-owned WorkItem workspace allocator."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from server.workspace_provisioner import ensure_workspace


def _git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(process.stderr or process.stdout)
    return str(process.stdout or "").strip()


def _repository(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "amadeus-test@example.invalid")
    _git(path, "config", "user.name", "Amadeus Test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "--quiet", "-m", "seed")


def test_real_git_worktrees_are_idempotent_and_work_item_isolated() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-host-worktree-") as temp:
        root = Path(temp)
        project = root / "project"
        _repository(project)
        previous_root = settings.WORK_WORKTREE_ROOT
        settings.WORK_WORKTREE_ROOT = str(root / "allocated")
        try:
            first = ensure_workspace(
                work_item_external_id="work_first",
                project_cwd=str(project),
                name="First task",
            )
            repeated = ensure_workspace(
                work_item_external_id="work_first",
                project_cwd=str(project),
                name="First task",
            )
            second = ensure_workspace(
                work_item_external_id="work_second",
                project_cwd=str(project),
                name="Second task",
            )
        finally:
            settings.WORK_WORKTREE_ROOT = previous_root

        first_workspace = first["workspace"]
        repeated_workspace = repeated["workspace"]
        second_workspace = second["workspace"]
        first_path = Path(first_workspace["cwd"])
        second_path = Path(second_workspace["cwd"])
        assert first["created"] is True
        assert repeated["created"] is False
        assert first_path == Path(repeated_workspace["cwd"])
        assert first_path != second_path
        assert first_workspace["backend"] == "host-git-worktree"
        assert Path(_git(first_path, "rev-parse", "--show-toplevel")).resolve() == first_path
        assert Path(_git(second_path, "rev-parse", "--show-toplevel")).resolve() == second_path
        assert _git(first_path, "rev-parse", "--git-common-dir")
        assert first_workspace["gitBranch"] == "amadeus/work/work_first"
        assert second_workspace["gitBranch"] == "amadeus/work/work_second"


def test_non_git_project_degrades_to_honest_local_single_writer_workspace() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-host-local-") as temp:
        project = Path(temp) / "project"
        project.mkdir()
        envelope = ensure_workspace(
            work_item_external_id="work_local",
            project_cwd=str(project),
            name="Local task",
        )
        assert envelope["created"] is False
        assert envelope["workspace"]["policy"] == "local"
        assert envelope["workspace"]["backend"] == "host-local"
        assert Path(envelope["workspace"]["cwd"]) == project


def _main() -> None:
    test_real_git_worktrees_are_idempotent_and_work_item_isolated()
    test_non_git_project_degrades_to_honest_local_single_writer_workspace()
    print("ok: Host-owned Git worktree provisioning is real and Provider-neutral")


if __name__ == "__main__":
    _main()
