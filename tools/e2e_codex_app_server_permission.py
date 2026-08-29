"""Verify Codex native approval -> Host permission -> same-turn continuation."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.work_ledger_store import WorkLedgerStore
from tools.e2e_direct_codex_conversation import (
    _create_sandbox_accessible_root,
    _free_port,
    _initialize_project,
    _remove_tree,
    _sdk_preflight,
    _start_server,
    _stop_server,
    _wait_for_codex_bootstrap,
    _wait_for_health,
    _work_projection,
)
from tools.e2e_real_work_conversation import WsProbe, _provider_status
from tools.semantic_journey_evidence import build_evidence


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    report_dir = (
        Path(args.report_dir).expanduser().resolve()
        if str(args.report_dir or "").strip()
        else RUNTIME / "e2e_reports" / "semantic_journeys" / "J4"
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    run_name = f"codex_app_server_permission_{_stamp()}_{uuid.uuid4().hex[:6]}"
    report_path = report_dir / f"{run_name}.json"
    log_path = report_dir / f"{run_name}.server.log"
    temporary_parent = (RUNTIME / "e2e_workspaces").resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    temporary_root = _create_sandbox_accessible_root(temporary_parent)
    isolation = temporary_root / "state"
    project = temporary_root / "project"
    # Keep the approval target outside both the turn cwd and the App Server's
    # repository cwd. A sibling probe directory is isolated and removable but
    # cannot inherit workspace-write authority by accident.
    external = ROOT.parent / f".amadeus-approval-probe-{uuid.uuid4().hex}"
    scratch = temporary_root / "scratch"
    worktrees = temporary_root / "worktrees"
    isolation.mkdir()
    external.mkdir()
    _initialize_project(project)
    target = external / "approved.txt"
    ledger_path = isolation / "work_ledger.sqlite3"
    with WorkLedgerStore(ledger_path) as store:
        store.create_or_get_project(project, name="approval-lab")

    report: dict[str, Any] = {
        "status": "error",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "report": str(report_path),
        "log": str(log_path),
        "temporary_root": str(temporary_root),
        "checks": {},
    }
    process = None
    log_handle = None
    exit_code = 2
    port = 0
    try:
        report["preflight"] = _sdk_preflight()
        port = _free_port()
        process, log_handle = _start_server(
            port,
            isolation,
            project,
            scratch,
            worktrees,
            log_path,
            cli="",
            transport="app-server",
            chat_provider=args.chat_provider,
            worktree_isolation=False,
        )
        await _wait_for_health(port, process, timeout=args.startup_timeout)
        await _wait_for_codex_bootstrap(port, process, timeout=args.startup_timeout)
        session_id = f"codex-permission-{uuid.uuid4().hex}"
        async with WsProbe(f"ws://127.0.0.1:{port}/ws") as probe:
            await probe.request(
                "session.create",
                {"session_id": session_id, "title": "Codex permission probe"},
            )
            after = len(probe.state.events)
            started = await probe.request(
                "provider.run",
                {
                    "provider": "codex",
                    "task": (
                        "Use a shell command to write exactly APPROVED followed by a newline "
                        f"to the absolute path {target}. This path is outside the current "
                        "project, so request approval and wait for the answer. Then read the "
                        "same file and report its exact content."
                    ),
                    "cwd": str(project),
                    "mode": "agent",
                    "metadata": {
                        "source": "e2e_codex_app_server_permission",
                        "session_id": session_id,
                    },
                    "requirements": {
                        "task_kind": "workspace_mutation",
                        "workspace_access": "write",
                        "workspace_ownership": "caller",
                        "interaction": "bidirectional",
                        "preferred_provider": "codex",
                        "preference_policy": "require",
                    },
                },
            )
            run = started.get("run") if isinstance(started.get("run"), dict) else {}
            run_id = str(run.get("run_id") or "")
            work_binding = started.get("work") if isinstance(started.get("work"), dict) else {}
            work_item_id = str(work_binding.get("work_item_id") or "")
            attempt_id = str(work_binding.get("attempt_id") or "")
            native_request_ids: set[str] = set()
            permission_ids: list[str] = []
            permission_resolutions: list[dict[str, Any]] = []
            permission_events: list[dict[str, Any]] = []
            loop = asyncio.get_running_loop()
            deadline = loop.time() + args.provider_timeout
            terminal = None

            while terminal is None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Codex permission continuation did not terminate")

                def is_next_control_event(event: Any) -> bool:
                    if event.params.get("run_id") != run_id:
                        return False
                    if event.method == "provider.result":
                        return True
                    if (
                        event.method != "provider.event"
                        or event.params.get("type") != "permission.requested"
                    ):
                        return False
                    request = event.params.get("payload", {}).get("permissionRequest", {})
                    request_id = str(request.get("request_id") or "")
                    return bool(request_id) and request_id not in native_request_ids

                event = await probe.wait_event(
                    is_next_control_event,
                    timeout=min(args.approval_timeout, remaining),
                    after=after,
                    description="Codex terminal or next native approval request",
                )
                if event.method == "provider.result":
                    terminal = event
                    break

                request = event.params.get("payload", {}).get("permissionRequest", {})
                native_request_id = str(request.get("request_id") or "")
                native_request_ids.add(native_request_id)
                permission_events.append(event.to_dict())
                if len(permission_events) > args.max_approvals:
                    raise RuntimeError(
                        f"approval request limit exceeded ({args.max_approvals})"
                    )

                projection = _work_projection(await probe.request("work.list", {}))
                selected = (
                    projection.get("selected")
                    if isinstance(projection.get("selected"), dict)
                    else {}
                )
                permission_id = str(selected.get("pendingPermissionRequestId") or "")
                if not permission_id:
                    raise RuntimeError("native approval had no actionable Host permission card")
                resolution = await probe.request(
                    "work.permission.resolve",
                    {
                        "permissionRequestId": permission_id,
                        "workItemId": work_item_id,
                        "attemptId": attempt_id,
                        "revision": str(projection.get("revision") or ""),
                        "decision": "allow_once",
                    },
                )
                permission_ids.append(permission_id)
                permission_resolutions.append(resolution)
                report["permission_request_ids"] = list(permission_ids)
                report["permission_resolutions"] = list(permission_resolutions)
                if resolution.get("ok") is not True:
                    raise RuntimeError(
                        "permission resolution rejected: "
                        + str(resolution.get("error") or "unknown_error")
                    )

            final_projection = _work_projection(await probe.request("work.list", {}))
            events = [event.to_dict() for event in probe.state.events[after:]]

        with WorkLedgerStore(ledger_path) as store:
            items = store.list_work_items(limit=20)
            attempts = store.list_attempts(work_item_id) if work_item_id else []
            permissions = store.list_permission_requests(
                work_item_id,
                attempt_id=attempt_id,
            ) if work_item_id and attempt_id else []
        selected_final = (
            final_projection.get("selected")
            if isinstance(final_projection.get("selected"), dict)
            else {}
        )
        content = target.read_text(encoding="utf-8") if target.is_file() else ""
        provider_runs = {
            str(event.get("params", {}).get("run_id") or "")
            for event in events
            if event.get("method") == "provider.event"
            and event.get("params", {}).get("type") == "run.created"
        }
        checks = {
            "native_requests_became_actionable_cards": bool(permission_ids)
            and len(permission_ids) == len(permission_events)
            and all(
                event.get("params", {})
                .get("payload", {})
                .get("permissionRequest", {})
                .get("diagnosticOnly")
                is False
                for event in permission_events
            ),
            "host_decisions_were_accepted": bool(permission_resolutions)
            and all(result.get("ok") is True for result in permission_resolutions),
            "same_run_continued": provider_runs == {run_id},
            "same_work_item_attempt": len(items) == 1 and len(attempts) == 1,
            "permissions_are_durable_allowed": len(permissions) == len(permission_ids)
            and all(permission.status == "allowed" for permission in permissions),
            "approved_effect_happened": content == "APPROVED\n",
            "terminal_succeeded": _provider_status(terminal) == "done",
            "card_closed_after_resolution": not str(
                selected_final.get("pendingPermissionRequestId") or ""
            ),
        }
        report.update(
            {
                "status": "passed" if all(checks.values()) else "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "checks": checks,
                "run_id": run_id,
                "work_item_id": work_item_id,
                "attempt_id": attempt_id,
                "permission_request_ids": permission_ids,
                "native_permission_request_ids": sorted(native_request_ids),
                "target_content": content,
            }
        )
        report["semantic_evidence"] = build_evidence(
            root=ROOT,
            journey_id="J4",
            status=str(report["status"]),
            test_level="L3",
            provider="codex",
            model="official-runtime",
            report_path=report_path,
            isolation_root=temporary_root,
            checks=checks,
            started_at=str(report["started_at"]),
            finished_at=str(report["finished_at"]),
            ledger_ids={
                "work_item_ids": [work_item_id] if work_item_id else [],
                "attempt_ids": [attempt_id] if attempt_id else [],
                "provider_run_ids": [run_id] if run_id else [],
            },
            manual_acceptance="pending",
            notes="Official Codex approval callback, Host decision, same native turn continuation.",
        )
        exit_code = 0 if report["status"] == "passed" else 1
    except Exception as exc:
        report.update(
            {
                "status": "error",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        if process is not None:
            try:
                await _stop_server(port, process)
            except Exception:
                pass
        if log_handle is not None:
            log_handle.close()
        if not args.keep_temp:
            try:
                _remove_tree(temporary_root)
                report["cleanup"] = {"removed": not temporary_root.exists()}
            except Exception as exc:
                report["cleanup"] = {"removed": False, "error": str(exc)}
                exit_code = 2
        try:
            _remove_tree(external)
            report.setdefault("cleanup", {})["external_removed"] = not external.exists()
        except Exception as exc:
            report.setdefault("cleanup", {})["external_removed"] = False
            report.setdefault("cleanup", {})["external_error"] = str(exc)
            exit_code = 2
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return exit_code, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-provider", default="deepseek")
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument("--approval-timeout", type=float, default=300.0)
    parser.add_argument("--provider-timeout", type=float, default=600.0)
    parser.add_argument("--max-approvals", type=int, default=8)
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--keep-temp", action="store_true")
    exit_code, report = asyncio.run(_run(parser.parse_args()))
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "report": report.get("report"),
                "checks": report.get("checks"),
                "cleanup": report.get("cleanup"),
                "error": report.get("error", ""),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
