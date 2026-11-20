"""Automated P0 control-plane acceptance against a real backend process.

Work order §12.3 lists nine acceptance items. Seven of them are statements
about server-owned truth — counts, projection fields, distinguishable states,
available actions, pin behaviour, the empty state, and restart recovery — so
they can be asserted over the WebSocket protocol instead of being read off a
screen. This harness does that, leaving only the two genuinely visual items
(panel switching and the Electron details jump) for a real-machine session.

The run boots the backend three times against one isolated ledger:

1. empty ledger  -> the empty state is real, not mock;
2. seeded ledger -> counts, fields, states, actions and pin behaviour;
3. same ledger   -> the projection is identical after a restart.

No LLM, no provider and no network are involved: the ledger is seeded
directly through WorkLedgerStore, so the run is deterministic and cheap
enough for CI.

    python tools/e2e_work_acceptance.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_real_work_conversation import (  # noqa: E402
    WsProbe,
    _free_port,
    _start_server,
    _stop_server,
    _utc_stamp,
    _wait_for_health,
)

from agent_host.work_ledger_store import WorkLedgerStore  # noqa: E402
from agent_host.work_ledger_types import CompletionDecision  # noqa: E402

RUNTIME = ROOT / "runtime"
REPORT_DIR = RUNTIME / "e2e_reports"

# The five presentation states a user must be able to tell apart (§12.3 item 5).
# A seeded in-flight attempt has no live provider process, so the backend
# reconciles it to "orphaned" on boot — that is the honest state, and asserting
# it is more valuable than pretending the attempt is still running.
DISTINGUISHABLE = ("orphaned", "process_ended", "needs_review", "accepted", "archived")


@dataclass
class Check:
    item: str
    name: str
    ok: bool = False
    detail: str = ""
    evidence: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class Report:
    started_at: str = field(default_factory=_utc_stamp)
    checks: list[Check] = field(default_factory=list)
    snapshots: dict[str, Any] = field(default_factory=dict)

    def record(
        self,
        item: str,
        name: str,
        ok: bool,
        detail: str = "",
        evidence: Any = None,
    ) -> Check:
        check = Check(item=item, name=name, ok=ok, detail=detail, evidence=evidence)
        self.checks.append(check)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {item} {name}" + (f" — {detail}" if detail else ""))
        return check

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def _seed_ledger(db_path: Path, workspace: Path) -> dict[str, Any]:
    """Write one WorkItem per presentation state and return the expected truth."""

    store = WorkLedgerStore(db_path)
    try:
        project = store.create_or_get_project(str(workspace))
        expected: dict[str, Any] = {"states": {}, "running": 0, "needs_attention": 0}

        def _item(title: str, *, mode: str, branch: str) -> Any:
            return store.create_work_item(
                project.project_id,
                title=title,
                goal=f"{title} goal",
                workspace_mode=mode,
                workspace_path=str(workspace),
                branch=branch,
            )

        # 1. an attempt left in flight by a dead process: the backend must
        # reconcile it to orphaned rather than keep claiming it runs.
        running = _item("Interrupted task", mode="worktree", branch="feat/running")
        store.create_attempt(
            running.work_item_id, provider="codex", task="running", mode="agent"
        )
        expected["states"]["orphaned"] = running.work_item_id
        expected["needs_attention"] += 1

        # 2. needs_review: the process ended and the ledger wants a decision.
        review = _item("Review ready task", mode="worktree", branch="feat/review")
        review_attempt = store.create_attempt(
            review.work_item_id, provider="codex", task="review", mode="agent"
        )
        store.update_attempt(review_attempt.attempt_id, execution_status="succeeded")
        store.record_completion(
            review.work_item_id,
            CompletionDecision(
                execution_status="succeeded",
                completeness="complete",
                attention="none",
                work_item_state="review_ready",
                rationale="seeded review_ready",
                terminal=True,
            ),
            attempt_id=review_attempt.attempt_id,
        )
        expected["states"]["needs_review"] = review.work_item_id

        # 3. process_ended with attention: a failed attempt needing the user.
        failed = _item("Failed task", mode="local", branch="")
        failed_attempt = store.create_attempt(
            failed.work_item_id, provider="codex", task="failed", mode="agent"
        )
        store.update_attempt(failed_attempt.attempt_id, execution_status="failed")
        store.record_completion(
            failed.work_item_id,
            CompletionDecision(
                execution_status="failed",
                completeness="incomplete",
                attention="error",
                work_item_state="open",
                rationale="seeded failure",
                terminal=True,
            ),
            attempt_id=failed_attempt.attempt_id,
        )
        expected["states"]["process_ended"] = failed.work_item_id
        expected["needs_attention"] += 1

        # 4. accepted and 5. archived: terminal, must require Reopen.
        accepted = _item("Accepted task", mode="worktree", branch="feat/accepted")
        accepted_attempt = store.create_attempt(
            accepted.work_item_id, provider="codex", task="accepted", mode="agent"
        )
        store.update_attempt(accepted_attempt.attempt_id, execution_status="succeeded")
        store.record_completion(
            accepted.work_item_id,
            CompletionDecision(
                execution_status="succeeded",
                completeness="complete",
                attention="none",
                work_item_state="accepted",
                rationale="seeded accepted",
                terminal=True,
            ),
            attempt_id=accepted_attempt.attempt_id,
            source="user",
        )
        expected["states"]["accepted"] = accepted.work_item_id

        archived = _item("Archived task", mode="local", branch="")
        archived_attempt = store.create_attempt(
            archived.work_item_id, provider="codex", task="archived", mode="agent"
        )
        store.update_attempt(archived_attempt.attempt_id, execution_status="succeeded")
        store.record_completion(
            archived.work_item_id,
            CompletionDecision(
                execution_status="succeeded",
                completeness="complete",
                attention="none",
                work_item_state="archived",
                rationale="seeded archived",
                terminal=True,
            ),
            attempt_id=archived_attempt.attempt_id,
            source="user",
        )
        expected["states"]["archived"] = archived.work_item_id
        return expected
    finally:
        store.close()


def _items_by_id(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id")): item for item in snapshot.get("items") or []}


def _comparable(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The parts of a projection that a restart must reproduce exactly."""
    return {
        "revision": snapshot.get("revision"),
        "selectedWorkItemId": snapshot.get("selectedWorkItemId"),
        "focusMode": snapshot.get("focusMode"),
        "counts": snapshot.get("counts"),
        "items": [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "state",
                    "execution",
                    "completion",
                    "attention",
                    "workspaceMode",
                    "workspacePath",
                    "branch",
                    "isolation",
                    "attemptId",
                    "artifactCount",
                    "canRetry",
                    "canResume",
                )
            }
            for item in sorted(
                snapshot.get("items") or [], key=lambda entry: str(entry.get("id"))
            )
        ],
    }


async def _check_empty_state(probe: WsProbe, report: Report) -> None:
    snapshot = (await probe.request("work.list", {})).get("work") or {}
    items = snapshot.get("items") or []
    counts = snapshot.get("counts") or {}
    ok = (
        not items
        and int(counts.get("running") or 0) == 0
        and int(counts.get("active") or 0) == 0
        and not str(snapshot.get("selectedWorkItemId") or "")
    )
    report.record(
        "12.3.8",
        "empty ledger shows a real empty state",
        ok,
        "" if ok else f"expected no items, got {len(items)} and counts {counts}",
        evidence={"counts": counts, "item_count": len(items)},
    )


async def _check_seeded(
    probe: WsProbe, report: Report, expected: dict[str, Any]
) -> dict[str, Any]:
    response = await probe.request("work.list", {})
    snapshot = response.get("work") or {}
    items = _items_by_id(snapshot)
    counts = snapshot.get("counts") or {}

    # §12.3 item 1 — the dock header counts.
    ok = int(counts.get("running") or 0) == expected["running"] and int(
        counts.get("needsAttention") or 0
    ) == expected["needs_attention"]
    report.record(
        "12.3.1",
        "N running / M need you match the ledger",
        ok,
        ""
        if ok
        else f"expected running={expected['running']} needsAttention={expected['needs_attention']}, got {counts}",
        evidence=counts,
    )

    # A task whose process is gone must never still read as running.
    orphaned = items.get(str(expected["states"].get("orphaned") or ""), {})
    reconciled = (
        str(orphaned.get("execution") or "") == "orphaned"
        and str(orphaned.get("attention") or "none") != "none"
    )
    report.record(
        "12.3.9a",
        "an interrupted attempt is reconciled to orphaned, not left running",
        reconciled,
        ""
        if reconciled
        else f"execution={orphaned.get('execution')!r} attention={orphaned.get('attention')!r}",
        evidence={
            "execution": orphaned.get("execution"),
            "attention": orphaned.get("attention"),
        },
    )

    # §12.3 item 4 — every task carries cwd, branch, isolation, selection reason.
    missing: list[str] = []
    for item_id, item in items.items():
        for key in ("workspacePath", "isolation", "selectionReason"):
            if not str(item.get(key) or "").strip():
                missing.append(f"{item.get('title')}:{key}")
        if item.get("workspaceMode") == "worktree" and not str(item.get("branch") or ""):
            missing.append(f"{item.get('title')}:branch")
    report.record(
        "12.3.4",
        "each task exposes cwd, branch, isolation and selection reason",
        not missing,
        "" if not missing else f"missing: {', '.join(missing[:6])}",
        evidence={"missing": missing},
    )

    # §12.3 item 5 — the five states are present and mutually distinguishable.
    seen = {
        label: items.get(str(expected["states"].get(label) or ""), {})
        for label in DISTINGUISHABLE
    }
    signatures = {
        label: (item.get("state"), item.get("execution"), item.get("attention"))
        for label, item in seen.items()
    }
    absent = [label for label, item in seen.items() if not item]
    collisions = len(signatures.values()) != len(set(signatures.values()))
    report.record(
        "12.3.5",
        "interrupted / process ended / needs review / accepted / archived are distinct",
        not absent and not collisions,
        ""
        if not absent and not collisions
        else f"absent={absent} collisions={collisions}",
        evidence={label: list(value) for label, value in signatures.items()},
    )

    # §12.3 item 6 — terminal tasks continue; accepted/archived need Reopen first.
    failures: list[str] = []
    ended = seen.get("process_ended") or {}
    if not (ended.get("canRetry") or ended.get("canResume")):
        failures.append("failed task offers neither Retry nor Resume")
    for label in ("accepted", "archived"):
        item = seen.get(label) or {}
        if item.get("canRetry"):
            failures.append(f"{label} task offers Retry without Reopen")
    report.record(
        "12.3.6",
        "terminal tasks continue; accepted/archived require Reopen",
        not failures,
        "; ".join(failures),
        evidence={
            label: {
                "state": (seen.get(label) or {}).get("state"),
                "canRetry": (seen.get(label) or {}).get("canRetry"),
                "canResume": (seen.get(label) or {}).get("canResume"),
            }
            for label in ("process_ended", "accepted", "archived")
        },
    )

    # §12.3 item 3 — a pinned workspace focus is not stolen by other tasks.
    await _check_pin(probe, report, snapshot)
    return (await probe.request("work.list", {})).get("work") or {}


async def _check_pin(
    probe: WsProbe, report: Report, snapshot: dict[str, Any]
) -> None:
    target = str(snapshot.get("selectedWorkItemId") or "")
    if not target:
        report.record("12.3.3", "pinned focus is not stolen", False, "no selectable task")
        return
    pinned = await probe.request(
        "work.focus",
        {
            "work_item_id": target,
            "focus_mode": "pinned",
            "revision": snapshot.get("revision"),
        },
    )
    pinned_snapshot = pinned.get("work") or {}
    if str(pinned_snapshot.get("workspaceFocusMode") or "") != "pinned":
        report.record(
            "12.3.3",
            "pinned focus is not stolen",
            False,
            f"focus did not pin: {pinned_snapshot.get('workspaceFocusMode')!r}",
            evidence=pinned_snapshot.get("workspaceFocusMode"),
        )
        return
    # Select a different task: a pinned workspace must survive it.
    others = [
        str(item.get("id"))
        for item in snapshot.get("items") or []
        if str(item.get("id")) != target
    ]
    after = pinned_snapshot
    if others:
        # Selecting is view-only. Sending focus_mode here would be an explicit
        # unpin command, which is a different user action entirely.
        after = (
            await probe.request(
                "work.focus",
                {
                    "work_item_id": others[0],
                    "revision": pinned_snapshot.get("revision"),
                },
            )
        ).get("work") or {}
    held = str(after.get("workspaceFocusWorkItemId") or "") == str(
        pinned_snapshot.get("workspaceFocusWorkItemId") or ""
    ) and str(after.get("workspaceFocusMode") or "") == "pinned"
    report.record(
        "12.3.3",
        "pinned workspace survives another task being selected",
        held,
        ""
        if held
        else f"workspace focus moved to {after.get('workspaceFocusWorkItemId')!r} mode={after.get('workspaceFocusMode')!r}",
        evidence={
            "pinned_to": pinned_snapshot.get("workspaceFocusWorkItemId"),
            "after": after.get("workspaceFocusWorkItemId"),
            "mode": after.get("workspaceFocusMode"),
        },
    )


async def _boot(port: int, isolation: Path, workspace: Path, log: Path):
    process, handle = _start_server(port, isolation, workspace, log)
    await _wait_for_health(port, process)
    return process, handle


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    report = Report()
    run_id = f"work_acceptance_{_utc_stamp()}"
    isolation = RUNTIME / "e2e_isolated" / run_id
    workspace = RUNTIME / "e2e_workspaces" / run_id
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    isolation.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    ledger = isolation / "work_ledger.sqlite3"
    uri = ""

    try:
        # Phase 1 — empty ledger.
        port = _free_port()
        uri = f"ws://127.0.0.1:{port}/ws"
        print("phase 1: empty ledger")
        process, handle = await _boot(port, isolation, workspace, isolation / "boot1.log")
        try:
            async with WsProbe(uri) as probe:
                await _check_empty_state(probe, report)
        finally:
            await _stop_server(port, process)
            handle.close()

        # Phase 2 — seeded ledger.
        print("phase 2: seeded ledger")
        expected = _seed_ledger(ledger, workspace)
        port = _free_port()
        uri = f"ws://127.0.0.1:{port}/ws"
        process, handle = await _boot(port, isolation, workspace, isolation / "boot2.log")
        try:
            async with WsProbe(uri) as probe:
                before_restart = await _check_seeded(probe, report, expected)
        finally:
            await _stop_server(port, process)
            handle.close()
        report.snapshots["before_restart"] = _comparable(before_restart)

        # Phase 3 — restart recovery.
        print("phase 3: restart recovery")
        port = _free_port()
        uri = f"ws://127.0.0.1:{port}/ws"
        process, handle = await _boot(port, isolation, workspace, isolation / "boot3.log")
        try:
            async with WsProbe(uri) as probe:
                after_restart = (await probe.request("work.list", {})).get("work") or {}
        finally:
            await _stop_server(port, process)
            handle.close()
        report.snapshots["after_restart"] = _comparable(after_restart)

        same = report.snapshots["before_restart"] == report.snapshots["after_restart"]
        detail = ""
        if not same:
            before = report.snapshots["before_restart"]
            after = report.snapshots["after_restart"]
            drifted = [key for key in before if before[key] != after.get(key)]
            detail = f"drifted: {', '.join(drifted)}"
        report.record(
            "12.3.9",
            "history, focus, artifacts and actions survive a restart",
            same,
            detail,
        )
    finally:
        if args.keep_workspace:
            print(f"kept isolation dir: {isolation}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)

    payload = {
        "schema": "amadeus.e2e.work_acceptance.v1",
        "run_id": run_id,
        "started_at": report.started_at,
        "finished_at": _utc_stamp(),
        "status": "passed" if report.ok else "failed",
        "automated_items": sorted({check.item for check in report.checks}),
        "manual_items": {
            "12.3.2": "Current / Needs you / History panel switching (UI)",
            "12.3.7": "Electron Open details lands on the same WorkItem (UI)",
        },
        "checks": [check.to_dict() for check in report.checks],
        "snapshots": report.snapshots,
    }
    report_path = REPORT_DIR / f"{run_id}.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nreport: {report_path}")
    return (0 if report.ok else 1), payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="keep the isolated ledger and server logs for inspection",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    started = time.monotonic()
    code, payload = asyncio.run(_run(args))
    passed = sum(1 for check in payload["checks"] if check["ok"])
    total = len(payload["checks"])
    print(
        f"{payload['status']}: {passed}/{total} automated P0 acceptance checks "
        f"in {time.monotonic() - started:.1f}s "
        f"({len(payload['manual_items'])} items remain manual)"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
