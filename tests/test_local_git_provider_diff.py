from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.handlers.provider_handler import ProviderHandler
from agent_host.work_ledger_store import WorkLedgerStore
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@contextmanager
def _repo() -> Path:
    with tempfile.TemporaryDirectory(prefix="local_git_diff_") as temp:
        root = Path(temp)
        _git(root, "init")
        _git(root, "config", "user.email", "test@example.invalid")
        _git(root, "config", "user.name", "Test User")
        yield root


async def _provider_diff(cwd: Path) -> dict:
    handler = ProviderHandler.__new__(ProviderHandler)
    return await handler._diff({"cwd": str(cwd)})


def test_provider_diff_uses_head_and_lists_untracked() -> None:
    async def run() -> None:
        with _repo() as repo:
            tracked = repo / "tracked.txt"
            tracked.write_text("one\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-m", "initial")
            tracked.write_text("two\n", encoding="utf-8")
            (repo / "new.txt").write_text("new\n", encoding="utf-8")

            res = await _provider_diff(repo)

        diff = res["diff"]
        assert diff["success"] is True
        assert diff["head"] is True
        assert "tracked.txt" in diff["patch"]
        assert "-one" in diff["patch"]
        assert "+two" in diff["patch"]
        assert diff["untracked"] == ["new.txt"]
        assert diff["changed_files"] == ["tracked.txt", "new.txt"]

    asyncio.run(run())


def test_provider_diff_preserves_non_ascii_and_spaced_paths() -> None:
    async def run() -> None:
        with _repo() as repo:
            tracked = repo / "区域 防御.html"
            tracked.write_text("base\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "initial")

            tracked.write_text("changed\n", encoding="utf-8")
            (repo / "新 策略.json").write_text("{}\n", encoding="utf-8")
            res = await _provider_diff(repo)

        diff = res["diff"]
        assert diff["changed_files"] == ["区域 防御.html", "新 策略.json"]
        assert diff["untracked"] == ["新 策略.json"]

    asyncio.run(run())


def test_provider_diff_handles_unborn_head() -> None:
    async def run() -> None:
        with _repo() as repo:
            (repo / "first.txt").write_text("first\n", encoding="utf-8")
            res = await _provider_diff(repo)

        diff = res["diff"]
        assert diff["success"] is True
        assert diff["head"] is False
        assert diff["untracked"] == ["first.txt"]
        assert diff["changed_files"] == ["first.txt"]

    asyncio.run(run())


def test_terminal_run_diff_uses_persisted_attempt_delta_not_current_cwd() -> None:
    async def run() -> None:
        with _repo() as repo:
            tracked = repo / "tracked.txt"
            tracked.write_text("one\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-m", "initial")
            with WorkLedgerStore(repo / "ledger.sqlite3") as store:
                project = store.create_or_get_project(repo)
                item = store.create_work_item(project.project_id, title="Historical diff")
                attempt = store.create_attempt(
                    item.work_item_id,
                    provider="codex",
                    task="Change tracked file",
                    provider_run_id="codex_historical_diff",
                )
                store.update_attempt(
                    attempt.attempt_id,
                    execution_status="succeeded",
                    metadata={
                        "git_delta": {
                            "available": True,
                            "repo_root": str(repo),
                            "baseline_head": "base123",
                            "current_head": "result456",
                            "patch": "diff --git a/tracked.txt b/tracked.txt\n-old\n+attempt result\n",
                            "changed_files": ["tracked.txt"],
                            "untracked": [],
                            "ambiguous_paths": [],
                        }
                    },
                )
                # The workspace moves on after the attempt. Historical View
                # Diff must still return the stored boundary.
                tracked.write_text("later unrelated change\n", encoding="utf-8")
                coordinator = WorkLedgerCoordinator(store)
                handler = ProviderHandler.__new__(ProviderHandler)
                handler._work_control = coordinator
                result = await handler._diff({"run_id": "codex_historical_diff"})

                diff = result["diff"]
                assert diff["source"] == "work_ledger"
                assert "+attempt result" in diff["patch"]
                assert "later unrelated change" not in diff["patch"]
                assert diff["attempt_id"] == attempt.attempt_id

    asyncio.run(run())


def test_codex_attempt_uses_provider_neutral_persisted_diff() -> None:
    async def run() -> None:
        with _repo() as repo:
            with WorkLedgerStore(repo / "ledger.sqlite3") as store:
                project = store.create_or_get_project(repo)
                item = store.create_work_item(project.project_id, title="Codex diff")
                attempt = store.create_attempt(
                    item.work_item_id,
                    provider="codex",
                    task="Create result.txt",
                    provider_run_id="codex_diff_run",
                )
                store.update_attempt(
                    attempt.attempt_id,
                    execution_status="succeeded",
                    metadata={
                        "git_delta": {
                            "available": True,
                            "repo_root": str(repo),
                            "baseline_head": "base123",
                            "current_head": "base123",
                            "patch": (
                                "diff --git a/result.txt b/result.txt\n"
                                "new file mode 100644\n"
                                "--- /dev/null\n"
                                "+++ b/result.txt\n"
                                "@@ -0,0 +1 @@\n"
                                "+created by codex\n"
                            ),
                            "changed_files": ["result.txt"],
                            "untracked": ["result.txt"],
                            "ambiguous_paths": [],
                        }
                    },
                )
                coordinator = WorkLedgerCoordinator(store)
                handler = ProviderHandler.__new__(ProviderHandler)
                handler._work_control = coordinator
                result = await handler._diff({"attempt_id": attempt.attempt_id})

                diff = result["diff"]
                assert diff["source"] == "work_ledger"
                assert diff["attempt_id"] == attempt.attempt_id
                assert "+created by codex" in diff["patch"]

    asyncio.run(run())


def _main() -> None:
    test_provider_diff_uses_head_and_lists_untracked()
    test_provider_diff_preserves_non_ascii_and_spaced_paths()
    test_provider_diff_handles_unborn_head()
    test_terminal_run_diff_uses_persisted_attempt_delta_not_current_cwd()
    test_codex_attempt_uses_provider_neutral_persisted_diff()
    print("ok: local git provider diff covers head, unborn head, and allowlist")


if __name__ == "__main__":
    _main()
