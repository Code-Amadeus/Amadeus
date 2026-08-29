"""Canonical evidence records for product-semantic journeys.

An E2E report is not automatically end-to-end evidence.  This module gives
all journey drivers one small, provider-neutral envelope that says exactly
which product journey ran, at which evidence level, against which code, and
which hard assertions passed.  It deliberately records a dirty-worktree
fingerprint in addition to HEAD: a report from uncommitted code must never be
mistaken for evidence about the clean commit.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "amadeus.semantic-journey-evidence.v1"
JOURNEY_IDS = frozenset({f"J{index}" for index in range(1, 8)})
TEST_LEVELS = frozenset({"L0", "L1", "L2", "L3", "L4"})
MANUAL_STATES = frozenset({"pending", "passed", "failed", "not-applicable"})
PASSING_STATUSES = frozenset({"passed"})


class EvidenceError(ValueError):
    """An evidence envelope is incomplete or internally inconsistent."""


def _run_git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise EvidenceError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def code_identity(root: Path) -> dict[str, Any]:
    """Return an identity that distinguishes clean and dirty executions."""

    checkout = root.resolve()
    commit_sha = _run_git(checkout, "rev-parse", "HEAD").decode().strip()
    status = _run_git(checkout, "status", "--porcelain=v1", "-z")
    fingerprint = hashlib.sha256()
    fingerprint.update(b"amadeus-worktree-v1\0")
    fingerprint.update(commit_sha.encode("ascii", errors="replace"))
    fingerprint.update(b"\0status\0")
    fingerprint.update(status)
    if status:
        fingerprint.update(b"\0diff\0")
        fingerprint.update(_run_git(checkout, "diff", "--binary", "HEAD", "--", "."))
        untracked = _run_git(
            checkout,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        for raw_name in sorted(value for value in untracked.split(b"\0") if value):
            fingerprint.update(b"\0untracked\0")
            fingerprint.update(raw_name)
            path = checkout / raw_name.decode("utf-8", errors="surrogateescape")
            if path.is_file():
                fingerprint.update(b"\0")
                fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
    return {
        "commit_sha": commit_sha,
        "workspace_dirty": bool(status),
        "workspace_fingerprint": fingerprint.hexdigest(),
    }


def _assertion_rows(
    checks: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(checks, Mapping):
        iterator: Iterable[tuple[str, Any]] = checks.items()
        for name, passed in iterator:
            rows.append({"name": str(name), "passed": bool(passed)})
    elif isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        for index, raw in enumerate(checks):
            if not isinstance(raw, Mapping):
                raise EvidenceError(f"assertion {index} must be an object")
            name = str(raw.get("name") or "").strip()
            if not name:
                raise EvidenceError(f"assertion {index} has no name")
            passed = raw.get("passed") if "passed" in raw else raw.get("ok")
            rows.append(
                {
                    "name": name,
                    "passed": bool(passed),
                    **(
                        {"errors": [str(item) for item in raw.get("errors", [])]}
                        if raw.get("errors")
                        else {}
                    ),
                }
            )
    else:
        raise EvidenceError("checks must be a mapping or a sequence of objects")
    if not rows:
        raise EvidenceError("at least one hard assertion is required")
    names = [row["name"] for row in rows]
    if len(names) != len(set(names)):
        raise EvidenceError("hard assertion names must be unique")
    return rows


def build_evidence(
    *,
    root: Path,
    journey_id: str,
    status: str,
    test_level: str,
    provider: str,
    model: str,
    report_path: str | Path,
    isolation_root: str | Path,
    checks: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    started_at: str,
    finished_at: str | None = None,
    artifact_hashes: Mapping[str, str] | None = None,
    ledger_ids: Mapping[str, Any] | None = None,
    manual_acceptance: str = "pending",
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """Build and validate a canonical evidence envelope."""

    assertions = _assertion_rows(checks)
    finished = finished_at or datetime.now(timezone.utc).isoformat()
    evidence = {
        "schema": SCHEMA,
        "journey_id": str(journey_id),
        "status": str(status),
        "test_level": str(test_level),
        "provider": str(provider or "").strip(),
        "model": str(model or "").strip(),
        "started_at": str(started_at or "").strip(),
        "finished_at": str(finished or "").strip(),
        "code_identity": code_identity(root),
        "report_path": str(Path(report_path).resolve()),
        "isolation_root": str(Path(isolation_root).resolve()),
        "hard_assertions": assertions,
        "failed_assertions": [
            row["name"] for row in assertions if row["passed"] is not True
        ],
        "artifact_hashes": dict(artifact_hashes or {}),
        "ledger_ids": dict(ledger_ids or {}),
        "manual_acceptance": str(manual_acceptance),
        "notes": [str(note) for note in notes if str(note).strip()],
    }
    validate_evidence(evidence)
    return evidence


def validate_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("schema") != SCHEMA:
        raise EvidenceError(f"schema must be {SCHEMA}")
    journey_id = str(value.get("journey_id") or "")
    if journey_id not in JOURNEY_IDS:
        raise EvidenceError(f"unknown journey_id: {journey_id!r}")
    test_level = str(value.get("test_level") or "")
    if test_level not in TEST_LEVELS:
        raise EvidenceError(f"invalid test_level: {test_level!r}")
    manual = str(value.get("manual_acceptance") or "")
    if manual not in MANUAL_STATES:
        raise EvidenceError(f"invalid manual_acceptance: {manual!r}")
    for key in ("provider", "model", "started_at", "finished_at", "report_path"):
        if not str(value.get(key) or "").strip():
            raise EvidenceError(f"{key} is required")
    identity = value.get("code_identity")
    if not isinstance(identity, Mapping):
        raise EvidenceError("code_identity is required")
    for key in ("commit_sha", "workspace_fingerprint"):
        if not str(identity.get(key) or "").strip():
            raise EvidenceError(f"code_identity.{key} is required")
    assertions = value.get("hard_assertions")
    normalized = _assertion_rows(assertions) if isinstance(assertions, Sequence) else []
    failed = [row["name"] for row in normalized if not row["passed"]]
    declared_failed = [str(item) for item in value.get("failed_assertions", [])]
    if failed != declared_failed:
        raise EvidenceError("failed_assertions does not match hard_assertions")
    if str(value.get("status") or "") in PASSING_STATUSES and failed:
        raise EvidenceError("passed evidence cannot contain failed hard assertions")
    return dict(value)


def evidence_from_report(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract an embedded envelope or accept a standalone envelope."""

    candidate: Any = value
    if value.get("schema") != SCHEMA:
        candidate = value.get("semantic_evidence")
    if not isinstance(candidate, Mapping):
        return None
    return validate_evidence(candidate)


def write_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    validated = validate_evidence(evidence)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
