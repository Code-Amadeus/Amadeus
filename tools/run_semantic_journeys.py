"""Run the implemented Alpha semantic Journeys without hiding missing ones.

The runner is intentionally a thin command registry.  Journey drivers keep
ownership of isolation and assertions; this file only selects them, preserves
their exit codes, and points the release gate at their canonical evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REPORTS = ROOT / "runtime" / "e2e_reports"
EXTERNAL_REPORTS = Path(tempfile.gettempdir()).resolve() / "amadeus_semantic_journeys"


@dataclass(frozen=True)
class JourneyCommand:
    journey_id: str
    title: str
    implemented: bool
    reason: str
    build: Callable[[argparse.Namespace, Path], list[str]] | None = None


def _project(mode: str) -> Callable[[argparse.Namespace, Path], list[str]]:
    def build(args: argparse.Namespace, report_dir: Path) -> list[str]:
        return [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            str(ROOT / "tools" / "probes" / "probe_project_provider_matrix.py"),
            mode,
            "--execution-provider",
            "codex",
            "--provider",
            args.provider,
            "--report-dir",
            str(report_dir),
        ]

    return build


def _active_steer(args: argparse.Namespace, report_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(ROOT / "tools" / "e2e_codex_app_server_control.py"),
        "--chat-provider",
        args.provider,
        "--report-dir",
        str(report_dir),
    ]


def _failure_recovery(args: argparse.Namespace, report_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(ROOT / "tools" / "e2e_routing_matrix.py"),
        "--scenario",
        "J6_failure_recovery_journey",
        "--mode",
        "real",
        "--long-silence",
        "--provider",
        args.provider,
        "--execution-provider",
        "codex",
        "--report-dir",
        str(report_dir),
    ]


def _permission_attention(args: argparse.Namespace, report_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(ROOT / "tools" / "e2e_codex_app_server_permission.py"),
        "--chat-provider",
        args.provider,
        "--report-dir",
        str(report_dir),
    ]


def _auip_experience(args: argparse.Namespace, report_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(ROOT / "tools" / "e2e_auip_semantic_journey.py"),
        "--model",
        args.model,
        "--report-dir",
        str(report_dir),
    ]


JOURNEYS = {
    "J1": JourneyCommand(
        "J1",
        "Project natural reference and current-source authority",
        True,
        "default Codex App Server through the provider-neutral Project matrix",
        _project("--history-canary"),
    ),
    "J2": JourneyCommand(
        "J2",
        "Session Draft promotion and fresh-Session boundary",
        True,
        "default Codex App Server; Keep as Project uses the shipping WS action",
        _project("--promotion-canary"),
    ),
    "J3": JourneyCommand(
        "J3",
        "in-flight report, amendment, steer, and final truth",
        True,
        "default Codex App Server; native same-run steer with Host read-only status",
        _active_steer,
    ),
    "J4": JourneyCommand(
        "J4",
        "permission Allow/Deny and resume",
        True,
        "default Codex App Server; native approval pauses and resumes the same run through Host authority",
        _permission_attention,
    ),
    "J5": JourneyCommand(
        "J5",
        "Browser/OpenClaw and return to Project",
        True,
        "real Browser/OpenClaw plus default Codex App Server return leg",
        _project("--cross-domain-canary"),
    ),
    "J6": JourneyCommand(
        "J6",
        "semantic silence, provider death, restart, and retry",
        True,
        "explicit 145-second default Codex recovery carrier; provider process killed in isolation",
        _failure_recovery,
    ),
    "J7": JourneyCommand(
        "J7",
        "AUIP natural control, verified participation, and bounded collapse",
        True,
        "real model + Chromium + restricted WebSocket + one continuous AppSession",
        _auip_experience,
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--journey", action="append", choices=sorted(JOURNEYS))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "deepseek"))
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-root", default="")
    return parser


def _inventory() -> list[dict[str, object]]:
    return [
        {
            "journey_id": item.journey_id,
            "title": item.title,
            "implemented": item.implemented,
            "reason": item.reason,
        }
        for item in JOURNEYS.values()
    ]


def main() -> int:
    args = _parser().parse_args()
    if args.list:
        print(json.dumps({"journeys": _inventory()}, ensure_ascii=False, indent=2))
        return 0
    selected = args.journey or []
    if not selected:
        print("select at least one --journey, or use --list", file=sys.stderr)
        return 2
    if not 1 <= args.repeat <= 3:
        print("--repeat must be between 1 and 3", file=sys.stderr)
        return 2
    unavailable = [JOURNEYS[item] for item in selected if not JOURNEYS[item].implemented]
    if unavailable:
        print(
            json.dumps(
                {
                    "status": "not_implemented",
                    "journeys": [
                        {"journey_id": item.journey_id, "reason": item.reason}
                        for item in unavailable
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    external = Path(args.report_root).resolve() if args.report_root else EXTERNAL_REPORTS
    started = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, object]] = []
    for journey_id in selected:
        spec = JOURNEYS[journey_id]
        for repetition in range(1, args.repeat + 1):
            if journey_id in {"J1", "J2", "J5"}:
                report_dir = external / journey_id
            else:
                report_dir = RUNTIME_REPORTS / "semantic_journeys" / journey_id
            report_dir.mkdir(parents=True, exist_ok=True)
            assert spec.build is not None
            command = spec.build(args, report_dir)
            row: dict[str, object] = {
                "journey_id": journey_id,
                "repeat": repetition,
                "command": command,
                "report_dir": str(report_dir),
            }
            if args.dry_run:
                row["status"] = "dry-run"
                row["returncode"] = None
            else:
                result = subprocess.run(command, cwd=str(ROOT), check=False)
                row["returncode"] = result.returncode
                row["status"] = "passed" if result.returncode == 0 else "failed"
            results.append(row)
            if row["status"] == "failed":
                break
    summary = {
        "schema": "amadeus.semantic-journey-runner.v1",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "dry-run"
            if args.dry_run
            else "passed"
            if all(row["status"] == "passed" for row in results)
            else "failed"
        ),
        "results": results,
        "release_gate_command": [
            sys.executable,
            str(ROOT / "tools" / "semantic_release_gate.py"),
            "--evidence-dir",
            str(RUNTIME_REPORTS),
            "--evidence-dir",
            str(external),
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] in {"passed", "dry-run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
