"""The chart publisher must not write to the protected application branch."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_chart_publication_is_idempotent_and_isolated_from_main(tmp_path: Path) -> None:
    git_executable = shutil.which("git")
    if not git_executable:
        pytest.skip("Git is required for the publication contract")
    bash = shutil.which("bash")
    if os.name == "nt":
        bundled_bash = Path(git_executable).resolve().parent.parent / "bin" / "bash.exe"
        if bundled_bash.is_file():
            bash = str(bundled_bash)
        else:
            pytest.skip("Git for Windows Bash is required for this Bash workflow")
    if not bash:
        pytest.skip("Bash is required for the publication workflow")

    workflow = yaml.safe_load((ROOT / ".github/workflows/star-history.yml").read_text(encoding="utf-8"))
    publish_step = next(
        step for step in workflow["jobs"]["update"]["steps"]
        if step.get("name") == "Publish a changed chart"
    )
    script = tmp_path / "publish.sh"
    script.write_text(publish_step["run"], encoding="utf-8", newline="\n")
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    work.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(
            [git_executable, *args], cwd=work, text=True, stderr=subprocess.STDOUT,
        ).strip()

    git("init", "--bare", str(remote))
    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("remote", "add", "origin", str(remote))
    (work / "README.md").write_text("Protected application content\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "fixture")
    git("push", "origin", "main")
    application_commit = git("rev-parse", "HEAD")
    (work / "assets").mkdir()
    chart = work / "assets/star-history.svg"

    def publish() -> str:
        subprocess.run(
            [bash, "-e", "-o", "pipefail", script.as_posix()],
            cwd=work, check=True, capture_output=True, text=True,
        )
        return git("ls-remote", "--heads", "origin", "refs/heads/star-history").split()[0]

    chart.write_text("<svg>initial</svg>\n", encoding="utf-8")
    first = publish()
    assert publish() == first

    updated = "<svg>updated</svg>\n"
    chart.write_text(updated, encoding="utf-8")
    second = publish()
    assert second != first
    assert git("rev-parse", f"{second}^") == first
    assert git("ls-tree", "--name-only", second) == "star-history.svg"
    assert git("show", f"{second}:star-history.svg") == updated.strip()
    assert git("ls-remote", "--heads", "origin", "refs/heads/main").split()[0] == application_commit
    assert git("rev-parse", "HEAD") == application_commit
    assert git("branch", "--show-current") == "main"
