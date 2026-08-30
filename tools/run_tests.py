from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "tests"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.test_discovery import (
    collected_by_file,
    declared_module_tests,
    uncollected_declarations,
)


def _canonical_host_path(value: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(os.fspath(value)))
    return Path(os.path.normpath(os.path.realpath(os.path.abspath(expanded))))


def _create_basetemp_root() -> Path:
    # Resolve the parent before creating a child. Windows can preserve a
    # junction/8.3 spelling when realpath is first called on a nested path.
    parent = _canonical_host_path(tempfile.gettempdir())
    created = tempfile.mkdtemp(
        prefix=f"amadeus-pytest-{os.getpid()}-",
        dir=str(parent),
    )
    return _canonical_host_path(created)


def _suite_env() -> dict[str, str]:
    """Run every suite against the same flags, whatever the developer's .env says.

    Worktree isolation asks the Host to provision a workspace, which requires a
    registered project; a unit test's temp directory is not one, so with the
    flag on in .env every suite that creates a WorkItem fails closed under R11
    -- correctly, but for a reason that has nothing to do with what it was
    testing. Inheriting it also made the regression result depend on which
    machine ran it, which is the one thing a regression may not do.

    Only flags that change control-plane behaviour belong here, and only while
    the suite that owns them sets its own value. Worktree and AUIP experiment
    suites patch the imported settings around their cases, so pinning the
    process defaults here costs no coverage. It does prevent a developer's
    `.env` from silently changing unrelated baseline assertions.
    """

    env = os.environ.copy()
    env["WORK_WORKTREE_ISOLATION"] = "0"
    # Keep the deterministic runner on the promoted product baseline. Suites
    # exercising the retired experiment modes pass an explicit constructor
    # value or patch settings inside their own case.
    env["AUIP_APPSESSION_ROLE_BRANCH_MODE"] = "b2"
    env["AUIP_ACTION_PROVIDER"] = ""
    env["AUIP_ACTION_MODEL"] = ""
    env["AUIP_ACTION_REASONING_EFFORT"] = "none"
    # GitHub's Windows image may publish TEMP through an 8.3 alias such as
    # RUNNER~1 while filesystem APIs return the long spelling. Give every
    # suite the canonical spelling of the same directory so path assertions
    # exercise identity and are not coupled to whichever spelling an API
    # happened to return.
    raw_temp = next(
        (
            str(env.get(name) or "").strip()
            for name in ("TMPDIR", "TEMP", "TMP")
            if str(env.get(name) or "").strip()
        ),
        tempfile.gettempdir(),
    )
    canonical_temp = str(_canonical_host_path(raw_temp))
    for name in ("TMPDIR", "TEMP", "TMP"):
        env[name] = canonical_temp
    # A suite should behave the same whether it happens to add ROOT to
    # sys.path itself or imports a project package directly.  Python starts an
    # absolute test script with tests/ as sys.path[0], so make the repository
    # import root explicit once in the runner instead of patching every file.
    existing_pythonpath = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(ROOT), existing_pythonpath) if value
    )
    return env


@dataclass
class SuiteResult:
    name: str
    passed: int
    collected: int
    seconds: float
    returncode: int
    skipped: int = 0

    @property
    def ok(self) -> bool:
        # Pytest exit status is authoritative for non-failing outcomes. A
        # model-less checkout may legitimately skip an optional local-asset
        # assertion, so requiring every collected node to say "passed" would
        # turn supported absence into a false regression.
        return self.returncode == 0 and self.collected > 0


def _count_passed(output: str) -> int:
    matches = re.findall(r"(?:^|\s)(\d+) passed(?:,|\s|$)", output)
    return int(matches[-1]) if matches else 0


def _count_skipped(output: str) -> int:
    matches = re.findall(r"(?:^|\s)(\d+) skipped(?:,|\s|$)", output)
    return int(matches[-1]) if matches else 0


def _excuse_import_blocked(missing: list[str]) -> list[str]:
    """Excuse uncollected declarations whose test module cannot even import.

    Tier-gated tests (pytest.importorskip for voice / local-model deps) are
    legitimately absent from collection in model-less environments. A module
    that imports fine but still yields no collected node remains a violation
    -- that is the naming-drift case the invariant exists for.
    """
    if not missing:
        return missing
    by_file: dict[str, list[str]] = {}
    for node_id in missing:
        by_file.setdefault(node_id.split("::", 1)[0], []).append(node_id)
    excused: list[str] = []
    for path, node_ids in by_file.items():
        probe = (
            "import importlib.util, sys;"
            f"spec = importlib.util.spec_from_file_location('tier_probe', r'{(ROOT / path).resolve()}');"
            "module = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(module)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=_suite_env(),
        )
        if result.returncode != 0 and (
            "ModuleNotFoundError" in result.stderr or "Skipped" in result.stderr
        ):
            print(
                f"[run_tests] {path}: skipped module (dependency missing), "
                f"declarations excused",
                file=sys.stderr,
            )
            continue
        excused.extend(node_ids)
    return excused


def _collect_tests() -> tuple[dict[str, list[str]], str, int]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            str(TEST_DIR),
        ],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=_suite_env(),
    )
    output = "\n".join(value for value in (proc.stdout, proc.stderr) if value)
    node_ids = [
        line.strip()
        for line in proc.stdout.splitlines()
        if "::" in line and line.lstrip().startswith("tests")
    ]
    return collected_by_file(node_ids), output, proc.returncode


def _run_suite(
    path: Path,
    *,
    collected: int,
    basetemp_root: Path,
) -> SuiteResult:
    print(f"=== {path.name} ===", flush=True)
    start = time.perf_counter()
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "pytest",
            "-q",
            "--disable-warnings",
            "--basetemp",
            str(basetemp_root / path.stem),
            str(path),
        ],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=_suite_env(),
    )
    elapsed = time.perf_counter() - start

    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)

    return SuiteResult(
        name=path.name,
        passed=_count_passed(proc.stdout),
        collected=collected,
        seconds=elapsed,
        returncode=proc.returncode,
        skipped=_count_skipped(proc.stdout),
    )


def _print_summary(results: list[SuiteResult]) -> None:
    print("\n=== test summary ===")
    print(
        f"{'suite':<36} {'pass':>5} {'skip':>5} {'all':>5} "
        f"{'sec':>7} {'status':>8}"
    )
    print(
        f"{'-' * 36} {'-' * 5:>5} {'-' * 5:>5} {'-' * 5:>5} "
        f"{'-' * 7:>7} {'-' * 8:>8}"
    )
    for result in results:
        status = "PASS" if result.ok else f"FAIL({result.returncode})"
        print(
            f"{result.name:<36} {result.passed:>5} {result.skipped:>5} "
            f"{result.collected:>5} {result.seconds:>7.2f} {status:>8}"
        )
    total_passed = sum(result.passed for result in results)
    total_skipped = sum(result.skipped for result in results)
    total_collected = sum(result.collected for result in results)
    total_seconds = sum(result.seconds for result in results)
    failed = [result.name for result in results if not result.ok]
    print(
        f"{'-' * 36} {'-' * 5:>5} {'-' * 5:>5} {'-' * 5:>5} "
        f"{'-' * 7:>7} {'-' * 8:>8}"
    )
    print(
        f"{'total':<36} {total_passed:>5} {total_skipped:>5} "
        f"{total_collected:>5} {total_seconds:>7.2f} "
        f"{'PASS' if not failed else 'FAIL':>8}"
    )
    if failed:
        print("failed suites: " + ", ".join(failed))


def main() -> int:
    suites = sorted(TEST_DIR.glob("test_*.py"))
    if not suites:
        print(f"no test_*.py files found under {TEST_DIR}", file=sys.stderr)
        return 1

    collection, collection_output, collection_rc = _collect_tests()
    if collection_rc != 0:
        print("pytest collection failed:", file=sys.stderr)
        print(collection_output, file=sys.stderr)
        return 1
    declared = declared_module_tests(TEST_DIR)
    node_ids = [node_id for values in collection.values() for node_id in values]
    missing = uncollected_declarations(declared, node_ids)
    missing = _excuse_import_blocked(missing)
    if missing:
        print("declared tests missing from pytest collection:", file=sys.stderr)
        for node_id in missing:
            print(f"- {node_id}", file=sys.stderr)
        return 1
    # A fixed repository-local --basetemp made the next full run ask pytest to
    # delete the previous run's Windows directory.  Antivirus/indexing or a
    # late file handle can transiently deny that deletion before any test is
    # executed.  Every invocation now owns a fresh OS-temp namespace; cleanup
    # is best-effort because stale diagnostic files must not falsify results.
    basetemp_root = _create_basetemp_root()
    try:
        results = [
            _run_suite(
                path,
                collected=len(collection.get(path.relative_to(ROOT).as_posix(), [])),
                basetemp_root=basetemp_root,
            )
            for path in suites
        ]
    finally:
        shutil.rmtree(basetemp_root, ignore_errors=True)
    _print_summary(results)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
