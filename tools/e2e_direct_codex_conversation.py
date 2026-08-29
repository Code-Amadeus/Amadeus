"""Exercise a Codex transport through the normal Amadeus server/chat entrypoint.

The probe uses one ignored disposable runtime root with ordinary inherited
Windows ACLs, enables exactly one Codex transport through shipping bootstrap,
uses only the Codex transport and never passes a cwd in a DELEGATE request. It covers Project
create, amend, one-off Scratch, and a host restart.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from importlib import metadata as importlib_metadata
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_host.work_ledger_store import WorkLedgerStore
from tools.e2e_real_work_conversation import (
    WsProbe,
    _free_port,
    _http_json,
    _provider_status,
    _stop_server,
    _wait_for_health,
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _source_status() -> str:
    return _git(ROOT, "status", "--porcelain=v1", "--untracked-files=all")


def _resolve_cli(value: str) -> str:
    configured = str(value or "").strip()
    if not configured:
        raise RuntimeError("a Direct Codex CLI path is required")
    candidate = Path(configured).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    discovered = shutil.which(configured)
    if discovered:
        return str(Path(discovered).resolve())
    raise RuntimeError(f"Direct Codex CLI was not found: {configured}")


def _cli_preflight(cli: str) -> dict[str, Any]:
    checks: dict[str, Any] = {"cli": cli, "version": "", "authenticated": False}
    for args, key in ((["--version"], "version"), (["login", "status"], "login")):
        result = subprocess.run(
            [cli, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
        if result.returncode:
            raise RuntimeError(f"Codex {key} preflight failed with {result.returncode}")
        if key == "version":
            checks["version"] = (result.stdout or result.stderr).strip().splitlines()[0]
        else:
            checks["authenticated"] = True
    return checks


def _sdk_preflight() -> dict[str, Any]:
    return {
        "transport": "app-server",
        "sdk_version": importlib_metadata.version("openai-codex"),
        "runtime_version": importlib_metadata.version("openai-codex-cli-bin"),
    }


def _initialize_project(project: Path) -> None:
    project.mkdir(parents=True, exist_ok=False)
    _git(project, "init", "--quiet")
    _git(project, "config", "user.email", "amadeus-e2e@example.invalid")
    _git(project, "config", "user.name", "Amadeus E2E")
    (project / "README.md").write_text(
        "# Disposable Direct Codex full-host probe\n",
        encoding="utf-8",
    )
    _git(project, "add", "README.md")
    _git(project, "commit", "--quiet", "-m", "seed disposable project")


def _create_sandbox_accessible_root(parent: Path) -> Path:
    """Create a disposable root with ordinary inherited ACLs on Windows.

    Python's TemporaryDirectory intentionally tightens its root permissions.
    The native Codex Windows sandbox runs under a restricted identity and
    cannot enter those roots, even when `-C` points at a child repository.
    PowerShell New-Item preserves the parent's ordinary inherited ACLs, which
    is the same caller-workspace precondition used by the shipping contract.
    """

    candidate = (parent / f"direct-full-host-{uuid.uuid4().hex[:10]}").resolve()
    if os.name != "nt":
        candidate.mkdir(parents=False, exist_ok=False)
        return candidate
    env = os.environ.copy()
    env["AMADEUS_E2E_ROOT_TO_CREATE"] = str(candidate)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "New-Item -ItemType Directory -Path $env:AMADEUS_E2E_ROOT_TO_CREATE -ErrorAction Stop | Out-Null",
        ],
        cwd=str(parent),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or not candidate.is_dir():
        raise RuntimeError(
            result.stderr.strip() or f"failed to create sandbox-accessible root: {candidate}"
        )
    return candidate


def _server_env(
    isolation: Path,
    project: Path,
    scratch: Path,
    worktrees: Path,
    *,
    cli: str,
    transport: str,
    chat_provider: str,
    worktree_isolation: bool,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AMADEUS_HEADLESS": "1",
            "AMADEUS_E2E_NO_TTS": "1",
            "AMADEUS_PRE_TRANSLATION_ENABLED": "0",
            "AMADEUS_SESSION_DIR": str(isolation / "sessions"),
            "AMADEUS_WORK_LEDGER_PATH": str(isolation / "work_ledger.sqlite3"),
            "WORK_PROJECT_ALLOWLIST": str(project),
            "WORK_SCRATCH_ROOT": str(scratch),
            "WORK_WORKTREE_ROOT": str(worktrees),
            "WORK_WORKTREE_ISOLATION": "1" if worktree_isolation else "0",
            "CODEX_APP_SERVER_PROVIDER_ENABLED": (
                "1" if transport == "app-server" else "0"
            ),
            "CODEX_APP_SERVER_APPROVAL_MODE": "host",
            "DIRECT_CODEX_PROVIDER_ENABLED": (
                "1" if transport == "direct" else "0"
            ),
            "DIRECT_CODEX_CLI_PATH": cli,
            "DIRECT_CODEX_CLI_PREFIX_ARGS": "",
            "DIRECT_CODEX_PREFLIGHT_TIMEOUT_S": "15",
            "DELEGATE_INTENT_ATTRIBUTE": "1",
            "DELEGATE_FOCUS_INTENT": "1",
            "VTS_ENABLED": "0",
            "WAKE_ENABLED": "0",
            "AEC_REALTIME_ENABLED": "0",
            "LLM_PROVIDER": chat_provider,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def _start_server(
    port: int,
    isolation: Path,
    project: Path,
    scratch: Path,
    worktrees: Path,
    log_path: Path,
    *,
    cli: str,
    transport: str,
    chat_provider: str,
    worktree_isolation: bool,
) -> tuple[subprocess.Popen, Any]:
    log_handle = log_path.open("a", encoding="utf-8", newline="\n")
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "server.app", "--port", str(port)],
        cwd=str(ROOT),
        env=_server_env(
            isolation,
            project,
            scratch,
            worktrees,
            cli=cli,
            transport=transport,
            chat_provider=chat_provider,
            worktree_isolation=worktree_isolation,
        ),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    return process, log_handle


def _work_projection(value: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value.get("work"), dict):
        return dict(value["work"])
    if isinstance(value.get("projection"), dict):
        return dict(value["projection"])
    return dict(value)


def _availability(value: dict[str, Any], provider_id: str) -> dict[str, Any]:
    rows = value.get("provider_availability")
    if not isinstance(rows, list):
        return {}
    return next(
        (
            dict(item)
            for item in rows
            if isinstance(item, dict) and item.get("provider_id") == provider_id
        ),
        {},
    )


async def _wait_for_codex_bootstrap(
    port: int,
    process: subprocess.Popen,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Health comes up before late bootstrap wiring; wait for Provider truth."""

    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"isolated backend exited during Provider bootstrap with {process.returncode}"
            )
        try:
            latest = await asyncio.to_thread(
                _http_json,
                f"http://127.0.0.1:{port}/runtime/status",
                timeout=5.0,
            )
            availability = latest.get("provider", {}).get("availability") or []
            if any(
                isinstance(item, dict)
                and item.get("provider_id") == "codex"
                and item.get("ready") is True
                and item.get("registered") is True
                for item in availability
            ):
                return latest
        except Exception:
            pass
        await asyncio.sleep(0.25)
    raise TimeoutError(
        "backend health was up but Codex bootstrap did not become ready"
    )


async def _chat_run(
    probe: WsProbe,
    *,
    session_id: str,
    text: str,
    chat_provider: str,
    chat_timeout: float,
    provider_timeout: float,
    label: str,
) -> dict[str, Any]:
    after = len(probe.state.events)
    turn_id = f"direct-host-{label}-{uuid.uuid4().hex}"
    await probe.request(
        "chat.send",
        {
            "text": text,
            "provider": chat_provider,
            "session_id": session_id,
            "turn_id": turn_id,
            "source": "e2e_direct_codex_conversation",
        },
    )
    complete = await probe.wait_event(
        lambda event: event.method == "chat.complete"
        and event.params.get("turn_id") == turn_id,
        timeout=chat_timeout,
        after=after,
        description=f"{label} chat completion",
    )
    created = await probe.wait_event(
        lambda event: event.method == "provider.event"
        and event.params.get("provider") == "codex"
        and event.params.get("type") == "run.created",
        timeout=chat_timeout,
        after=after,
        description=f"{label} Direct Codex run.created",
    )
    run_id = str(created.params.get("run_id") or "")
    terminal = await probe.wait_event(
        lambda event: event.method == "provider.result"
        and event.params.get("run_id") == run_id,
        timeout=provider_timeout,
        after=after,
        description=f"{label} Direct Codex terminal result",
    )
    await asyncio.sleep(1.5)
    return {
        "label": label,
        "turn_id": turn_id,
        "run_id": run_id,
        "reply": str(complete.params.get("full_text") or ""),
        "terminal_status": _provider_status(terminal),
        "provider_result": terminal.params,
        "event_start": after,
        "event_end": len(probe.state.events),
    }


def _file_evidence(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.is_file(),
        "bytes": path.stat().st_size if path.is_file() else 0,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "",
        "text": path.read_text(encoding="utf-8", errors="replace") if path.is_file() else "",
    }


def _remove_tree(root: Path) -> None:
    def onerror(function, path, _exc_info) -> None:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    if root.exists():
        shutil.rmtree(root, onerror=onerror)


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    stamp = _utc_stamp()
    run_name = f"direct_codex_full_host_{stamp}_{uuid.uuid4().hex[:6]}"
    report_dir = (
        Path(args.report_dir).resolve()
        if args.report_dir
        else RUNTIME / "e2e_reports"
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{run_name}.json"
    log_path = report_dir / f"{run_name}.server.log"
    # Codex's Windows workspace sandbox cannot write under the user's
    # AppData\Local\Temp tree even when that directory is its cwd. Keep the
    # disposable nested Git repositories under Amadeus's ignored runtime tree,
    # which is inside the granted workspace boundary but outside tracked source.
    temporary_parent = (RUNTIME / "e2e_workspaces").resolve()
    temporary_parent.mkdir(parents=True, exist_ok=True)
    temporary_root = _create_sandbox_accessible_root(temporary_parent)
    isolation = temporary_root / "state"
    project = temporary_root / "project"
    scratch = temporary_root / "scratch"
    worktrees = temporary_root / "worktrees"
    isolation.mkdir()
    _initialize_project(project)
    source_before = _source_status()
    transport = str(args.transport or "direct").strip().lower()
    cli = _resolve_cli(args.codex_cli) if transport == "direct" else ""
    report: dict[str, Any] = {
        "schema": "amadeus.codex-full-host-e2e.v2",
        "transport": transport,
        "run_name": run_name,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "paths": {
            "report": str(report_path),
            "server_log": str(log_path),
            "temporary_root": str(temporary_root),
            "temporary_parent": str(temporary_parent),
        },
        "checks": {},
        "phase": "preflight",
        "turns": [],
    }
    process: subprocess.Popen | None = None
    probe: WsProbe | None = None
    log_handle = None
    exit_code = 1
    try:
        report["preflight"] = (
            _sdk_preflight() if transport == "app-server" else _cli_preflight(cli)
        )
        report["phase"] = "seed-ledger"
        ledger_path = isolation / "work_ledger.sqlite3"
        with WorkLedgerStore(ledger_path) as store:
            project_record = store.create_or_get_project(project, name="direct-host")
        report["project_id"] = project_record.project_id

        port = _free_port()
        report["phase"] = "first-bootstrap"
        process, log_handle = _start_server(
            port,
            isolation,
            project,
            scratch,
            worktrees,
            log_path,
            cli=cli,
            transport=transport,
            chat_provider=args.chat_provider,
            worktree_isolation=args.worktree_isolation,
        )
        await _wait_for_health(port, process, timeout=args.startup_timeout)
        runtime_before = await _wait_for_codex_bootstrap(
            port,
            process,
            timeout=args.startup_timeout,
        )
        session_id = f"direct-full-host-{uuid.uuid4().hex}"
        report["phase"] = "project-create"
        async with WsProbe(f"ws://127.0.0.1:{port}/ws") as probe:
            providers_before = await probe.request("provider.list", {})
            await probe.request(
                "session.create",
                {"session_id": session_id, "title": "Direct Codex full-host probe"},
            )
            create_turn = await _chat_run(
                probe,
                session_id=session_id,
                chat_provider=args.chat_provider,
                chat_timeout=args.chat_timeout,
                provider_timeout=args.provider_timeout,
                label="project-create",
                text=(
                    "切换到 direct-host 项目，并明确交给 codex 完成这个编码任务："
                    "在项目根目录创建 direct_host.txt，内容必须恰好是 phase-one 加一个换行。"
                    "不要在主对话里直接做，也不要要求我提供路径。"
                ),
            )
            report["turns"].append(create_turn)
            first_work = _work_projection(await probe.request("work.list", {}))
            report["phase"] = "project-amend"
            amend_turn = await _chat_run(
                probe,
                session_id=session_id,
                chat_provider=args.chat_provider,
                chat_timeout=args.chat_timeout,
                provider_timeout=args.provider_timeout,
                label="project-amend",
                text=(
                    "把刚才任务里的 direct_host.txt 改成内容恰好为 phase-two 加一个换行，"
                    "仍然明确交给 codex；这是修改已有成果，必须使用 amend 语义。"
                ),
            )
            report["turns"].append(amend_turn)
            amended_work = _work_projection(await probe.request("work.list", {}))
            report["phase"] = "scratch-one-off"
            scratch_turn = await _chat_run(
                probe,
                session_id=session_id,
                chat_provider=args.chat_provider,
                chat_timeout=args.chat_timeout,
                provider_timeout=args.provider_timeout,
                label="scratch-one-off",
                text=(
                    "另外做一个一次性的独立草稿任务，不在任何项目里做：明确交给 codex "
                    "创建 scratch_note.txt，内容必须恰好是 scratch-only 加一个换行。"
                ),
            )
            report["turns"].append(scratch_turn)
            final_work = _work_projection(await probe.request("work.list", {}))
            first_events = [event.to_dict() for event in probe.state.events]

        report["phase"] = "restart-bootstrap"
        await _stop_server(port, process)
        process = None
        if log_handle is not None:
            log_handle.close()
            log_handle = None

        restart_port = _free_port()
        process, log_handle = _start_server(
            restart_port,
            isolation,
            project,
            scratch,
            worktrees,
            log_path,
            cli=cli,
            transport=transport,
            chat_provider=args.chat_provider,
            worktree_isolation=args.worktree_isolation,
        )
        await _wait_for_health(restart_port, process, timeout=args.startup_timeout)
        runtime_after = await _wait_for_codex_bootstrap(
            restart_port,
            process,
            timeout=args.startup_timeout,
        )
        async with WsProbe(f"ws://127.0.0.1:{restart_port}/ws") as probe:
            providers_after = await probe.request("provider.list", {})
            await probe.request("session.load", {"session_id": session_id})
            restored_work = _work_projection(await probe.request("work.list", {}))
            if transport == "app-server":
                report["phase"] = "post-restart-amend"
                restart_amend_turn = await _chat_run(
                    probe,
                    session_id=session_id,
                    chat_provider=args.chat_provider,
                    chat_timeout=args.chat_timeout,
                    provider_timeout=args.provider_timeout,
                    label="post-restart-amend",
                    text=(
                        "继续刚才 direct_host.txt 的任务，仍然交给 codex："
                        "把文件完整内容改成 phase-three 加一个换行并读取验证。"
                        "这是同一个 WorkItem 的 amend，不要新建任务。"
                    ),
                )
                report["turns"].append(restart_amend_turn)
            restarted_work = _work_projection(await probe.request("work.list", {}))
            selected_after_restart = (
                restarted_work.get("selected")
                if isinstance(restarted_work.get("selected"), dict)
                else {}
            )
            provider_neutral_diff = await probe.request(
                "provider.diff",
                {
                    "attempt_id": str(selected_after_restart.get("attemptId") or ""),
                    "run_id": str(selected_after_restart.get("currentRunId") or ""),
                },
            )
            provider_neutral_status = await probe.request(
                "provider.status",
                {
                    "run_id": str(selected_after_restart.get("currentRunId") or ""),
                    "cwd": str(selected_after_restart.get("workspacePath") or ""),
                },
            )

        report["phase"] = "evidence"
        await _stop_server(restart_port, process)
        process = None
        if log_handle is not None:
            log_handle.close()
            log_handle = None

        with WorkLedgerStore(ledger_path) as store:
            items = store.list_work_items(limit=20)
            item_evidence = []
            for item in items:
                attempts = store.list_attempts(item.work_item_id)
                completion = store.latest_completion(item.work_item_id)
                item_evidence.append(
                    {
                        "work_item_id": item.work_item_id,
                        "project_id": item.project_id,
                        "workspace_path": item.workspace_path,
                        "workspace_mode": item.workspace_mode,
                        "metadata": dict(item.metadata),
                        "attempts": [
                            {
                                "provider": attempt.provider,
                                "status": attempt.execution_status,
                                "run_id": attempt.provider_run_id,
                                "metadata": dict(attempt.metadata),
                            }
                            for attempt in attempts
                        ],
                        "completion": (
                            {
                                "execution": completion.execution_status,
                                "completeness": completion.completeness,
                                "attention": completion.attention,
                                "rationale": completion.rationale,
                            }
                            if completion is not None
                            else {}
                        ),
                    }
                )

        project_items = [
            item
            for item in item_evidence
            if str(item.get("project_id") or "") == project_record.project_id
        ]
        created_project_item = next(
            (
                item
                for item in project_items
                if not str(item.get("metadata", {}).get("related_work_item_id") or "")
            ),
            project_items[0] if project_items else {},
        )
        amend_project_item = next(
            (
                item
                for item in project_items
                if str(item.get("metadata", {}).get("related_work_item_id") or "")
            ),
            {},
        )
        scratch_item = next(
            (
                item
                for item in item_evidence
                if Path(item["workspace_path"]).resolve() != project.resolve()
                and str(Path(item["workspace_path"]).resolve()).lower().startswith(
                    str(scratch.resolve()).lower()
                )
            ),
            {},
        )
        scratch_workspace = Path(str(scratch_item.get("workspace_path") or ""))
        project_workspace = Path(
            str(created_project_item.get("workspace_path") or project)
        ).resolve()
        project_file = _file_evidence(project_workspace / "direct_host.txt")
        scratch_file = _file_evidence(scratch_workspace / "scratch_note.txt")
        project_attempts = created_project_item.get("attempts") or []
        project_session_ids = [
            str(
                attempt.get("metadata", {})
                .get("provider_session", {})
                .get("session_id")
                or ""
            )
            for attempt in project_attempts
            if isinstance(attempt, dict)
        ]
        project_session_ids = [value for value in project_session_ids if value]
        terminal_ok = all(
            turn["terminal_status"] in {"done", "succeeded", "completed"}
            for turn in report["turns"]
        )
        first_items = first_work.get("items") if isinstance(first_work.get("items"), list) else []
        amended_items = (
            amended_work.get("items") if isinstance(amended_work.get("items"), list) else []
        )
        final_items = final_work.get("items") if isinstance(final_work.get("items"), list) else []
        availability_before = _availability(providers_before, "codex")
        availability_after = _availability(providers_after, "codex")
        event_methods = {event.get("method") for event in first_events}
        completion_truth_matches = bool(item_evidence)
        turn_by_run = {turn["run_id"]: turn for turn in report["turns"]}
        for item in item_evidence:
            attempts = item.get("attempts") or []
            run_id = str(attempts[-1].get("run_id") or "") if attempts else ""
            turn = turn_by_run.get(run_id, {})
            tool_failures = int(
                turn.get("provider_result", {})
                .get("metadata", {})
                .get("codex", {})
                .get("tool_failures", 0)
                or 0
            )
            expected_attention = "conflict" if tool_failures else "review"
            completion_truth_matches = completion_truth_matches and (
                item.get("completion", {}).get("execution") == "succeeded"
                and item.get("completion", {}).get("attention") == expected_attention
            )
        expected_turn_count = 4 if transport == "app-server" else 3
        expected_item_count = 2 if transport == "app-server" else 3
        if transport == "app-server":
            amend_continuity = (
                len(first_items) == 1
                and len(amended_items) == 1
                and len(project_items) == 1
                and len(project_attempts) == 3
                and len(project_session_ids) == 3
                and len(set(project_session_ids)) == 1
            )
        else:
            amend_continuity = (
                len(first_items) == 1
                and len(amended_items) == 2
                and len(created_project_item.get("attempts") or []) == 1
                and len(amend_project_item.get("attempts") or []) == 1
                and str(
                    amend_project_item.get("metadata", {}).get("related_work_item_id")
                    or ""
                )
                == str(created_project_item.get("work_item_id") or "")
            )
        checks = {
            "bootstrap_registered_ready_codex": (
                "codex" in providers_before.get("providers", [])
                and availability_before.get("ready") is True
                and availability_before.get("registered") is True
            ),
            "codex_attempt_diff_is_host_owned": (
                isinstance(provider_neutral_diff.get("diff"), dict)
                and provider_neutral_diff["diff"].get("source") == "work_ledger"
                and bool(provider_neutral_diff["diff"].get("changed_files", []))
                and (
                    transport != "app-server"
                    or (
                        "direct_host.txt"
                        in provider_neutral_diff["diff"].get("changed_files", [])
                        and "phase-three"
                        in str(provider_neutral_diff["diff"].get("patch") or "")
                    )
                )
            ),
            "codex_workspace_status_is_host_owned": (
                isinstance(provider_neutral_status.get("status"), dict)
                and provider_neutral_status["status"].get("success") is True
            ),
            "all_real_codex_runs_succeeded": terminal_ok
            and len(report["turns"]) == expected_turn_count,
            "project_create_wrote_real_file": project_file["exists"],
            "amend_matches_transport_continuity_contract": amend_continuity,
            "amend_content_is_current": project_file["text"]
            == ("phase-three\n" if transport == "app-server" else "phase-two\n"),
            "scratch_created_independent_work_item": len(final_items)
            == expected_item_count,
            "scratch_is_under_host_root": bool(scratch_item),
            "scratch_file_is_real": scratch_file["text"] == "scratch-only\n",
            "all_attempts_are_codex": bool(item_evidence)
            and all(
                attempt["provider"] == "codex"
                for item in item_evidence
                for attempt in item["attempts"]
            ),
            "ledger_completion_succeeded": bool(item_evidence)
            and all(
                item.get("completion", {}).get("execution") == "succeeded"
                for item in item_evidence
            ),
            "completion_truth_matches_tool_evidence": completion_truth_matches,
            "project_git_ownership": Path(
                _git(project_workspace, "rev-parse", "--show-toplevel")
            ).resolve()
            == project_workspace,
            "host_worktree_policy_applied": (
                created_project_item.get("workspace_mode") == "worktree"
                and created_project_item.get("metadata", {})
                .get("workspace_allocation", {})
                .get("backend")
                == "host-git-worktree"
                and project_workspace != project.resolve()
            )
            if args.worktree_isolation
            else (
                created_project_item.get("workspace_mode") == "local"
                and project_workspace == project.resolve()
            ),
            "project_checkout_isolated": (
                not (project / "direct_host.txt").exists()
                if args.worktree_isolation
                else project_file["exists"]
            ),
            "scratch_git_ownership": bool(scratch_item)
            and Path(
                _git(scratch_workspace, "rev-parse", "--show-toplevel")
            ).resolve()
            == scratch_workspace.resolve(),
            "canonical_provider_events_visible": "provider.event" in event_methods
            and "provider.result" in event_methods,
            "ui_projection_visible": "work.updated" in event_methods
            and len(final_items) == expected_item_count
            and all(item.get("execution") == "succeeded" for item in final_items),
            "restart_registered_ready_codex": (
                "codex" in providers_after.get("providers", [])
                and availability_after.get("ready") is True
                and availability_after.get("registered") is True
            ),
            "restart_restored_project_focus": (
                str(restored_work.get("destinationLabel") or "") == "direct-host"
                and str(restarted_work.get("destinationLabel") or "") == "direct-host"
                and str(restored_work.get("destinationProjectId") or "")
                == project_record.project_id
                and str(restarted_work.get("destinationProjectId") or "")
                == project_record.project_id
            ),
            "runtime_status_exposes_availability": any(
                item.get("provider_id") == "codex" and item.get("ready") is True
                for item in (runtime_before.get("provider", {}).get("availability") or [])
                if isinstance(item, dict)
            )
            and any(
                item.get("provider_id") == "codex" and item.get("ready") is True
                for item in (runtime_after.get("provider", {}).get("availability") or [])
                if isinstance(item, dict)
            ),
            "source_checkout_untouched": _source_status() == source_before,
        }
        report.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "passed" if all(checks.values()) else "failed",
                "phase": "complete",
                "checks": checks,
                "provider_availability_before": availability_before,
                "provider_availability_after": availability_after,
                "work": {
                    "after_create": first_work,
                    "after_amend": amended_work,
                    "after_scratch": final_work,
                    "after_restart_restore": restored_work,
                    "after_restart": restarted_work,
                },
                "provider_neutral_read_models": {
                    "diff": provider_neutral_diff,
                    "status": provider_neutral_status,
                },
                "ledger": item_evidence,
                "files": {
                    "project": project_file,
                    "scratch": scratch_file,
                },
                "events": first_events,
            }
        )
        exit_code = 0 if report["status"] == "passed" else 1
    except Exception as exc:
        if probe is not None:
            report["events_at_failure"] = [
                event.to_dict() for event in probe.state.events
            ]
        report.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        exit_code = 2
    finally:
        if process is not None:
            try:
                await _stop_server(int(locals().get("restart_port") or locals().get("port") or 0), process)
            except Exception:
                pass
        if log_handle is not None:
            log_handle.close()
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not args.keep_temp:
            try:
                _remove_tree(temporary_root)
                report["cleanup"] = {
                    "temporary_root_removed": not temporary_root.exists(),
                    "recoverable": False,
                }
            except Exception as exc:
                report["cleanup"] = {
                    "temporary_root_removed": False,
                    "recoverable": True,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                exit_code = 2
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return exit_code, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport",
        choices=("direct", "app-server"),
        default="direct",
    )
    parser.add_argument(
        "--codex-cli",
        default=os.environ.get("DIRECT_CODEX_REAL_CLI_PATH", ""),
    )
    parser.add_argument(
        "--chat-provider",
        default=os.environ.get("LLM_PROVIDER", "deepseek"),
    )
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--chat-timeout", type=float, default=180.0)
    parser.add_argument("--provider-timeout", type=float, default=900.0)
    parser.add_argument("--worktree-isolation", action="store_true")
    parser.add_argument("--keep-temp", action="store_true")
    return parser


def main() -> int:
    exit_code, report = asyncio.run(_run(_parser().parse_args()))
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "report": report.get("paths", {}).get("report"),
                "server_log": report.get("paths", {}).get("server_log"),
                "checks": report.get("checks", {}),
                "cleanup": report.get("cleanup", {}),
                "error": report.get("error", ""),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
