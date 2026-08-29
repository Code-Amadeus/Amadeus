"""Exercise status and native steer through the real Amadeus Chat/Host path."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
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


async def _send_chat(
    probe: WsProbe,
    *,
    session_id: str,
    label: str,
    text: str,
    chat_provider: str,
    timeout_s: float,
) -> tuple[str, int, Any]:
    after = len(probe.state.events)
    turn_id = f"codex-control-{label}-{uuid.uuid4().hex}"
    await probe.request(
        "chat.send",
        {
            "text": text,
            "provider": chat_provider,
            "session_id": session_id,
            "turn_id": turn_id,
            "source": "e2e_codex_app_server_control",
        },
    )
    complete = await probe.wait_event(
        lambda event: event.method == "chat.complete"
        and str(event.params.get("turn_id") or "") == turn_id,
        timeout=timeout_s,
        after=after,
        description=f"{label} chat completion",
    )
    return turn_id, after, complete


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    report_dir = (
        Path(args.report_dir).expanduser().resolve()
        if str(args.report_dir or "").strip()
        else RUNTIME / "e2e_reports"
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    run_name = f"codex_app_server_control_{_stamp()}_{uuid.uuid4().hex[:6]}"
    report_path = report_dir / f"{run_name}.json"
    log_path = report_dir / f"{run_name}.server.log"
    temporary_parent = (RUNTIME / "e2e_workspaces").resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    temporary_root = _create_sandbox_accessible_root(temporary_parent)
    isolation = temporary_root / "state"
    project = temporary_root / "project"
    scratch = temporary_root / "scratch"
    worktrees = temporary_root / "worktrees"
    isolation.mkdir()
    _initialize_project(project)
    (project / "slow_gate.py").write_text(
        "from pathlib import Path\n"
        "import time\n"
        "deadline = time.monotonic() + 300\n"
        "while time.monotonic() < deadline and not Path('release.flag').exists():\n"
        "    time.sleep(0.2)\n"
        "print('released' if Path('release.flag').exists() else 'timed-out')\n",
        encoding="utf-8",
    )
    from tools.e2e_direct_codex_conversation import _git

    _git(project, "add", "slow_gate.py")
    _git(project, "commit", "--quiet", "-m", "seed active-control gate")
    ledger_path = isolation / "work_ledger.sqlite3"
    with WorkLedgerStore(ledger_path) as store:
        project_record = store.create_or_get_project(project, name="control-lab")

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
        session_id = f"codex-control-{uuid.uuid4().hex}"
        async with WsProbe(f"ws://127.0.0.1:{port}/ws") as probe:
            await probe.request(
                "session.create",
                {"session_id": session_id, "title": "Codex active control probe"},
            )
            create_turn, create_after, create_complete = await _send_chat(
                probe,
                session_id=session_id,
                label="create",
                chat_provider=args.chat_provider,
                timeout_s=args.chat_timeout,
                text=(
                    "切换到 control-lab 项目并交给 codex 做：先在项目根运行 "
                    "python slow_gate.py，必须等它结束；然后创建 control.txt，"
                    "内容恰好是 ORIGINAL 加一个换行，并读取验证。"
                ),
            )
            created = await probe.wait_event(
                lambda event: event.method == "provider.event"
                and event.params.get("provider") == "codex"
                and event.params.get("type") == "run.created",
                timeout=args.chat_timeout,
                after=create_after,
                description="Codex active-control run.created",
            )
            run_id = str(created.params.get("run_id") or "")
            tool_started = await probe.wait_event(
                lambda event: event.method == "provider.event"
                and event.params.get("run_id") == run_id
                and event.params.get("type") == "tool.call"
                and "slow_gate.py"
                in str(event.params.get("payload", {}).get("input", {})),
                timeout=args.activity_timeout,
                after=create_after,
                description="Codex active-control first tool",
            )

            _status_turn, status_after, status_complete = await _send_chat(
                probe,
                session_id=session_id,
                label="status",
                chat_provider=args.chat_provider,
                timeout_s=args.chat_timeout,
                text="刚才那项工作现在做到哪了？只查当前状态，不要启动、重试或修改任务。",
            )
            status_created_runs = [
                event
                for event in probe.state.events[status_after:]
                if event.method == "provider.event"
                and event.params.get("type") == "run.created"
            ]

            amend_turn, amend_after, amend_complete = await _send_chat(
                probe,
                session_id=session_id,
                label="amend",
                chat_provider=args.chat_provider,
                timeout_s=args.chat_timeout,
                text=(
                    "改一下刚才正在跑的任务：等待结束后不要写 ORIGINAL，"
                    "把 control.txt 的完整内容改成 STEERED 加一个换行并读取验证。"
                    "仍然交给 codex，这是同一项工作的运行中修改。"
                ),
            )
            steer = await probe.wait_event(
                lambda event: event.method == "provider.event"
                and event.params.get("run_id") == run_id
                and event.params.get("type") == "run.status"
                and event.params.get("payload", {}).get("stage") == "steer_queued",
                timeout=args.activity_timeout,
                after=amend_after,
                description="Codex native steer queued",
            )
            amend_created_runs = [
                event
                for event in probe.state.events[amend_after:]
                if event.method == "provider.event"
                and event.params.get("type") == "run.created"
            ]
            (project / "release.flag").write_text("release\n", encoding="utf-8")
            terminal = await probe.wait_event(
                lambda event: event.method == "provider.result"
                and event.params.get("run_id") == run_id,
                timeout=args.provider_timeout,
                after=create_after,
                description="Codex steered terminal",
            )
            await asyncio.sleep(1.5)
            work = _work_projection(await probe.request("work.list", {}))
            events = [event.to_dict() for event in probe.state.events]

        items = work.get("items") if isinstance(work.get("items"), list) else []
        with WorkLedgerStore(ledger_path) as store:
            ledger_items = store.list_work_items(limit=20)
            attempts = (
                store.list_attempts(ledger_items[0].work_item_id)
                if len(ledger_items) == 1
                else []
            )
        content = (
            (project / "control.txt").read_text(encoding="utf-8")
            if (project / "control.txt").is_file()
            else ""
        )
        checks = {
            "initial_run_started_once": bool(run_id),
            "status_was_host_read_only": not status_created_runs,
            "status_reply_is_visible": bool(
                str(status_complete.params.get("full_text") or "").strip()
            ),
            "native_steer_was_queued": int(
                steer.params.get("payload", {}).get("revision") or 0
            )
            >= 1,
            "amend_did_not_start_second_run": not amend_created_runs,
            "one_work_item_one_attempt": len(ledger_items) == 1 and len(attempts) == 1,
            "steered_result_is_current": content == "STEERED\n",
            "original_result_was_not_written": content != "ORIGINAL\n",
            "terminal_succeeded": _provider_status(terminal) == "done",
            "ui_projection_succeeded": len(items) == 1
            and items[0].get("execution") == "succeeded",
        }
        report.update(
            {
                "status": "passed" if all(checks.values()) else "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "checks": checks,
                "run_id": run_id,
                "project_id": project_record.project_id,
                "turn_ids": {
                    "create": create_turn,
                    "amend": amend_turn,
                },
                "chat_replies": {
                    "create": str(create_complete.params.get("full_text") or ""),
                    "status": str(status_complete.params.get("full_text") or ""),
                    "amend": str(amend_complete.params.get("full_text") or ""),
                },
                "first_tool_elapsed_s": round(tool_started.elapsed_s, 3),
                "control_text": content,
                "event_methods": sorted({event.get("method") for event in events}),
            }
        )
        report["semantic_evidence"] = build_evidence(
            root=ROOT,
            journey_id="J3",
            status=str(report["status"]),
            test_level="L3",
            provider="codex",
            model=str(args.chat_provider),
            report_path=report_path,
            isolation_root=temporary_root,
            checks=checks,
            started_at=str(report["started_at"]),
            finished_at=str(report["finished_at"]),
            ledger_ids={
                "work_item_ids": [item.work_item_id for item in ledger_items],
                "attempt_ids": [attempt.attempt_id for attempt in attempts],
                "provider_run_ids": [run_id],
            },
            manual_acceptance="pending",
            notes=(
                "Default Codex App Server carrier: natural status query plus native same-run steer.",
            ),
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
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return exit_code, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-provider", default="deepseek")
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument("--chat-timeout", type=float, default=240.0)
    parser.add_argument("--activity-timeout", type=float, default=300.0)
    parser.add_argument("--provider-timeout", type=float, default=600.0)
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
