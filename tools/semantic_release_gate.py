"""Summarize canonical semantic-journey evidence for an Alpha candidate.

The gate never runs providers and never upgrades old reports by inference.  It
only counts canonical evidence envelopes emitted by journey drivers.  Default
matching includes the dirty-worktree fingerprint, so reports from two
different uncommitted states cannot satisfy one release gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.semantic_journey_evidence import (
    JOURNEY_IDS,
    SCHEMA,
    code_identity,
    evidence_from_report,
)


DEFAULT_ROOTS = (ROOT / "runtime" / "e2e_reports",)
MANUAL_REQUIRED = frozenset({"J3", "J4", "J5", "J7"})


def discover_evidence(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    evidence: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for base in paths:
        candidates = [base] if base.is_file() else base.rglob("*.json") if base.is_dir() else []
        for path in candidates:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                item = evidence_from_report(raw)
                if item is None:
                    continue
                key = (str(item.get("journey_id")), str(item.get("report_path")))
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(item)
            except Exception as exc:
                # Ordinary legacy reports have no canonical envelope and are
                # ignored above.  A report that claims the schema but is
                # malformed is retained as an instrument error.
                try:
                    claims_schema = f'"{SCHEMA}"' in path.read_text(
                        encoding="utf-8", errors="ignore"
                    )
                except OSError:
                    claims_schema = False
                if claims_schema:
                    rejected.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return evidence, rejected


def evaluate_gate(
    evidence: list[dict[str, Any]],
    *,
    identity: dict[str, Any],
    required_repeats: int,
    require_manual: bool,
) -> dict[str, Any]:
    matching = [
        item
        for item in evidence
        if item.get("code_identity", {}).get("commit_sha") == identity["commit_sha"]
        and item.get("code_identity", {}).get("workspace_fingerprint")
        == identity["workspace_fingerprint"]
        and item.get("test_level") in {"L3", "L4"}
        and item.get("status") == "passed"
        and not item.get("failed_assertions")
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in matching:
        grouped[str(item["journey_id"])].append(item)
    rows: list[dict[str, Any]] = []
    for journey_id in sorted(JOURNEY_IDS):
        items = sorted(grouped.get(journey_id, []), key=lambda item: str(item.get("finished_at") or ""))
        automatic = [item for item in items if item.get("test_level") == "L3"]
        manual_ok = any(
            item.get("test_level") == "L4" or item.get("manual_acceptance") == "passed"
            for item in items
        )
        manual_needed = require_manual and journey_id in MANUAL_REQUIRED
        passed = len(automatic) >= required_repeats and (manual_ok or not manual_needed)
        rows.append(
            {
                "journey_id": journey_id,
                "status": "passed" if passed else "missing",
                "automatic_passes": len(automatic),
                "required_repeats": required_repeats,
                "manual_required": manual_needed,
                "manual_passed": manual_ok,
                "latest_report": str(items[-1].get("report_path") or "") if items else "",
            }
        )
    return {
        "schema": "amadeus.semantic-release-gate.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "code_identity": identity,
        "required_repeats": required_repeats,
        "manual_required_for": sorted(MANUAL_REQUIRED) if require_manual else [],
        "status": "passed" if all(row["status"] == "passed" for row in rows) else "incomplete",
        "journeys": rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", action="append", default=[])
    parser.add_argument("--required-repeats", type=int, default=1)
    parser.add_argument("--require-manual", action="store_true")
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="print an incomplete baseline without returning a failing exit code",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.required_repeats < 1:
        raise SystemExit("--required-repeats must be positive")
    roots = [Path(value).resolve() for value in args.evidence_dir] or list(DEFAULT_ROOTS)
    evidence, rejected = discover_evidence(roots)
    report = evaluate_gate(
        evidence,
        identity=code_identity(ROOT),
        required_repeats=args.required_repeats,
        require_manual=bool(args.require_manual),
    )
    report["evidence_roots"] = [str(path) for path in roots]
    report["canonical_evidence_count"] = len(evidence)
    report["rejected_canonical_reports"] = rejected
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    complete = report["status"] == "passed" and not rejected
    return 0 if complete or args.allow_incomplete else 1


if __name__ == "__main__":
    raise SystemExit(main())
