"""Tests for the repository's pytest-discovery invariant."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import run_tests
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


@pytest.fixture
def runner_suites(monkeypatch, tmp_path: Path):
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    paths = [test_dir / f"test_{index}.py" for index in range(7)]
    for path in paths:
        path.write_text("def test_example(): pass\n", encoding="utf-8")
    collection = {
        f"tests/{path.name}": [f"tests/{path.name}::test_example"]
        for path in paths
    }
    executed = []

    def run_suite(path, *, collected, basetemp_root):
        executed.append(path)
        return SuiteResult(path.name, 1, collected, 0.01, 0)

    monkeypatch.setattr(run_tests, "ROOT", tmp_path)
    monkeypatch.setattr(run_tests, "TEST_DIR", test_dir)
    monkeypatch.setattr(run_tests, "_collect_tests", lambda: (collection, "", 0))
    monkeypatch.setattr(run_tests, "_run_suite", run_suite)
    return paths, collection, executed


def test_shards_cover_every_suite_once_and_default_runs_all(runner_suites):
    paths, _, executed = runner_suites
    for index in range(3):
        assert run_tests.main(["--shard-count", "3", "--shard-index", str(index)]) == 0
    assert sorted(executed) == paths
    executed.clear()
    assert run_tests.main([]) == 0
    assert executed == paths


def test_shard_checks_discovery_even_in_another_partition(runner_suites):
    _, collection, executed = runner_suites
    collection.pop("tests/test_1.py")
    assert run_tests.main(["--shard-count", "2", "--shard-index", "0"]) == 1
    assert executed == []


def test_shard_propagates_suite_failure(monkeypatch, runner_suites):
    monkeypatch.setattr(
        run_tests, "_run_suite",
        lambda path, **kwargs: SuiteResult(path.name, 0, kwargs["collected"], 0.01, 1),
    )
    assert run_tests.main(["--shard-count", "2", "--shard-index", "0"]) == 1


@pytest.mark.parametrize("count,index", [(0, 0), (2, -1), (2, 2)])
def test_invalid_shard_arguments_fail(count, index):
    with pytest.raises(SystemExit) as error:
        run_tests.main(["--shard-count", str(count), "--shard-index", str(index)])
    assert error.value.code == 2


def test_empty_shard_fails(runner_suites):
    assert run_tests.main(["--shard-count", "8", "--shard-index", "7"]) == 1
