"""Tests for the repository's pytest-discovery invariant."""

from __future__ import annotations

from pathlib import Path

from tools.test_discovery import (
    collected_by_file,
    declared_module_tests,
    uncollected_declarations,
)
from tools.run_tests import (
    SuiteResult,
    _count_skipped,
    _create_basetemp_root,
    _suite_env,
)


def test_declared_module_tests_includes_sync_and_async_top_level_tests(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "def test_sync():\n    pass\n\n"
        "async def test_async():\n    pass\n\n"
        "def helper():\n    pass\n",
        encoding="utf-8",
    )

    assert declared_module_tests(tests) == {
        "tests/test_sample.py": {"test_sync", "test_async"}
    }


def test_uncollected_declarations_accepts_parametrized_pytest_nodes() -> None:
    declared = {"tests/test_sample.py": {"test_plain", "test_matrix"}}
    collected = (
        "tests/test_sample.py::test_plain",
        "tests/test_sample.py::test_matrix[first]",
        "tests/test_sample.py::test_matrix[second]",
    )

    assert uncollected_declarations(declared, collected) == []
    assert collected_by_file(collected) == {
        "tests/test_sample.py": list(collected)
    }


def test_uncollected_declarations_reports_the_exact_missing_node() -> None:
    declared = {"tests/test_sample.py": {"test_seen", "test_hidden"}}

    assert uncollected_declarations(
        declared,
        ("tests/test_sample.py::test_seen",),
    ) == ["tests/test_sample.py::test_hidden"]


def test_suite_environment_ignores_local_control_plane_experiments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    alias = tmp_path / "alias"
    alias.mkdir()
    aliased_temp = alias / ".."
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(aliased_temp))
    monkeypatch.setenv("WORK_WORKTREE_ISOLATION", "1")
    monkeypatch.setenv("AUIP_APPSESSION_ROLE_BRANCH_MODE", "a1")
    monkeypatch.setenv("AUIP_ACTION_PROVIDER", "openai")
    monkeypatch.setenv("AUIP_ACTION_MODEL", "custom-model")
    monkeypatch.setenv("AUIP_ACTION_REASONING_EFFORT", "low")

    env = _suite_env()

    assert env["WORK_WORKTREE_ISOLATION"] == "0"
    assert env["AUIP_APPSESSION_ROLE_BRANCH_MODE"] == "b2"
    assert env["AUIP_ACTION_PROVIDER"] == ""
    assert env["AUIP_ACTION_MODEL"] == ""
    assert env["AUIP_ACTION_REASONING_EFFORT"] == "none"
    assert Path(env["TMPDIR"]) == tmp_path.resolve()
    assert env["TEMP"] == env["TMPDIR"]
    assert env["TMP"] == env["TMPDIR"]


def test_suite_result_accepts_supported_skips_without_hiding_failures() -> None:
    skipped = SuiteResult(
        name="test_optional_asset.py",
        passed=5,
        skipped=1,
        collected=6,
        seconds=0.1,
        returncode=0,
    )
    failed = SuiteResult(
        name="test_failure.py",
        passed=5,
        collected=6,
        seconds=0.1,
        returncode=1,
    )

    assert skipped.ok is True
    assert failed.ok is False
    assert _count_skipped("5 passed, 1 skipped in 0.10s") == 1


def test_basetemp_is_created_beneath_the_canonical_parent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    alias = tmp_path / "alias"
    alias.mkdir()
    monkeypatch.setattr(
        "tools.run_tests.tempfile.gettempdir",
        lambda: str(alias / ".."),
    )

    basetemp = _create_basetemp_root()

    assert basetemp.parent == tmp_path.resolve()
    assert basetemp.is_dir()
