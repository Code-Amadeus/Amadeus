"""Run an isolated, real-model conversation through Amadeus and Codex.

The probe starts its own backend on an unused port, uses the normal WebSocket
chat API, and records Provider/WorkObserver events.  Speech synthesis is
replaced by a queue drainer in that child process; model calls and task routing
remain real.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TERMINAL_PROVIDER_STATUSES = {
    "done",
    "succeeded",
    "completed",
    "error",
    "failed",
    "cancelled",
    "canceled",
    "denied",
}


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(url: str, *, method: str = "GET", timeout: float = 2.0) -> dict[str, Any]:
    request = Request(url, method=method, headers={"User-Agent": "amadeus-e2e-probe"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _safe_excerpt(value: Any, limit: int = 500) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, list):
        return [_safe_excerpt(item, limit) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): _safe_excerpt(item, limit) for key, item in value.items()}
    return value


@dataclass
class EventRecord:
    elapsed_s: float
    method: str
    params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "elapsed_s": round(self.elapsed_s, 3),
            "method": self.method,
            "params": _safe_excerpt(self.params),
        }


@dataclass
class ProbeState:
    started_at: float = field(default_factory=time.monotonic)
    events: list[EventRecord] = field(default_factory=list)
    responses: dict[str, asyncio.Future] = field(default_factory=dict)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)


class WsProbe:
    def __init__(
        self,
        uri: str,
        *,
        subprotocols: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.uri = uri
        self.subprotocols = tuple(subprotocols or ())
        self.ws = None
        self.state = ProbeState()
        self.reader_task: asyncio.Task | None = None

    async def __aenter__(self) -> "WsProbe":
        import websockets

        # Local CUDA TTS can keep the application loop busy long enough for
        # the protocol heartbeat to race a normal event write.  Probe requests
        # and waits already have bounded timeouts, so disable transport pings
        # instead of turning a healthy long narration into a false disconnect.
        self.ws = await websockets.connect(
            self.uri,
            max_size=8 * 1024 * 1024,
            ping_interval=None,
            subprotocols=list(self.subprotocols) or None,
        )
        self.reader_task = asyncio.create_task(self._reader(), name="e2e-ws-reader")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self.reader_task is not None:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)

    async def _reader(self) -> None:
        assert self.ws is not None
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if message.get("type") == "res":
                    future = self.state.responses.pop(str(message.get("id") or ""), None)
                    if future is not None and not future.done():
                        future.set_result(message.get("params") or {})
                    continue
                if message.get("type") != "evt":
                    continue
                record = EventRecord(
                    elapsed_s=time.monotonic() - self.state.started_at,
                    method=str(message.get("method") or ""),
                    params=message.get("params") if isinstance(message.get("params"), dict) else {},
                )
                async with self.state.changed:
                    self.state.events.append(record)
                    self.state.changed.notify_all()
        finally:
            error = ConnectionError("backend WebSocket closed before the probe completed")
            for future in self.state.responses.values():
                if not future.done():
                    future.set_exception(error)
            async with self.state.changed:
                self.state.changed.notify_all()

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        assert self.ws is not None
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.state.responses[request_id] = future
        await self.ws.send(
            json.dumps(
                {"type": "req", "id": request_id, "method": method, "params": params},
                ensure_ascii=False,
            )
        )
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.state.responses.pop(request_id, None)
        if result.get("error"):
            raise RuntimeError(f"{method} failed: {result['error']}")
        return result

    async def wait_event(
        self,
        predicate: Callable[[EventRecord], bool],
        *,
        timeout: float,
        after: int = 0,
        description: str,
    ) -> EventRecord:
        deadline = time.monotonic() + timeout
        while True:
            for record in self.state.events[after:]:
                if predicate(record):
                    return record
            if self.reader_task is not None and self.reader_task.done():
                raise ConnectionError(
                    f"backend WebSocket closed while waiting for {description}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {description}")
            async with self.state.changed:
                await asyncio.wait_for(self.state.changed.wait(), timeout=remaining)


def _provider_status(record: EventRecord) -> str:
    if record.method == "provider.result":
        return str(record.params.get("status") or "").strip().lower()
    payload = record.params.get("payload") if isinstance(record.params.get("payload"), dict) else {}
    return str(payload.get("status") or "").strip().lower()


def _server_env(isolation_dir: Path, workspace: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AMADEUS_HEADLESS": "1",
            "AMADEUS_E2E_NO_TTS": "1",
            "AMADEUS_PRE_TRANSLATION_ENABLED": "0",
            "AMADEUS_SESSION_DIR": str(isolation_dir / "sessions"),
            "AMADEUS_WORK_LEDGER_PATH": str(isolation_dir / "work_ledger.sqlite3"),
            "WORK_PROJECT_ALLOWLIST": str(workspace),
            "CODEX_APP_SERVER_PROVIDER_ENABLED": "1",
            "DIRECT_CODEX_PROVIDER_ENABLED": "0",
            "VTS_ENABLED": "0",
            "WAKE_ENABLED": "0",
            "AEC_REALTIME_ENABLED": "0",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def _write_fixture(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=False)
    (workspace / "SPEC.md").write_text(
        """# E2E task queue fixture

Implement a tiny in-memory task queue in `task_queue.py`.

- `create(task_id, payload)` is idempotent for identical input.
- Reusing an id with different input raises `ValueError`.
- Valid states are queued -> running -> succeeded/failed.
- Invalid transitions raise `ValueError`.
- Add `unittest` coverage in `test_task_queue.py`.
- Write a short implementation and test summary to `RESULT.md`.
- This routing probe is write-only: do not execute the generated code or any shell command.
""",
        encoding="utf-8",
    )


def _preflight() -> dict[str, Any]:
    from tools.e2e_direct_codex_conversation import _sdk_preflight

    return _sdk_preflight()


async def _wait_for_health(port: int, process: subprocess.Popen, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"isolated backend exited during startup with code {process.returncode}")
        try:
            result = await asyncio.to_thread(_http_json, f"http://127.0.0.1:{port}/health")
            if result.get("status") == "ok":
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError("isolated backend did not become healthy")


async def _stop_server(port: int, process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        await asyncio.to_thread(
            _http_json,
            f"http://127.0.0.1:{port}/shutdown",
            method="POST",
            timeout=3.0,
        )
    except Exception:
        pass
    try:
        await asyncio.to_thread(process.wait, 15.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 8.0)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, 5.0)


def _start_server(port: int, isolation_dir: Path, workspace: Path, log_path: Path) -> tuple[subprocess.Popen, Any]:
    log_handle = log_path.open("w", encoding="utf-8", newline="\n")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "server.app", "--port", str(port)],
        cwd=ROOT,
        env=_server_env(isolation_dir, workspace),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    return process, log_handle


def _workspace_evidence(workspace: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for name in ("task_queue.py", "test_task_queue.py", "RESULT.md"):
        path = workspace / name
        evidence[name] = {
            "exists": path.is_file(),
            "bytes": path.stat().st_size if path.is_file() else 0,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "",
        }
    return evidence


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    stamp = _utc_stamp()
    run_id = f"real_conversation_{stamp}_{uuid.uuid4().hex[:6]}"
    report_dir = Path(args.report_dir).resolve() if args.report_dir else RUNTIME / "e2e_reports"
    isolation_dir = RUNTIME / "e2e_isolated" / run_id
    workspace = RUNTIME / "e2e_workspaces" / run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    isolation_dir.mkdir(parents=True, exist_ok=False)
    _write_fixture(workspace)
    log_path = report_dir / f"{run_id}.server.log"
    report_path = report_dir / f"{run_id}.json"
    report: dict[str, Any] = {
        "schema": "amadeus.real-work-conversation-e2e.v1",
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": args.provider,
        "paths": {
            "report": str(report_path),
            "server_log": str(log_path),
            "workspace": str(workspace),
            "isolation_dir": str(isolation_dir),
        },
        "checks": {},
        "events": [],
    }
    process: subprocess.Popen | None = None
    log_handle = None
    probe: WsProbe | None = None
    exit_code = 1
    try:
        report["preflight"] = _preflight()
        port = _free_port()
        report["port"] = port
        process, log_handle = _start_server(port, isolation_dir, workspace, log_path)
        await _wait_for_health(port, process, timeout=args.startup_timeout)

        session_id = f"e2e-{uuid.uuid4().hex}"
        first_turn_id = f"e2e-start-{uuid.uuid4().hex}"
        status_turn_id = f"e2e-status-{uuid.uuid4().hex}"
        prompt_path = workspace.as_posix()
        task_prompt = (
            "请把这项工作作为后台编码任务交给 Codex 执行，不要在主对话里直接编写答案。"
            "这是路由协议验收：你输出的 DELEGATE 标签必须明确包含 provider=\"codex\"，"
            f"并将 cwd 属性原样设置为 \"{prompt_path}\"。"
            "task 属性里要保留完整执行要求：读取 SPEC.md，按规格创建 task_queue.py、"
            "test_task_queue.py 和 RESULT.md；这是分发探针，不要执行代码或任何命令。"
            "不要访问或修改该目录之外的任何文件。现在只需自然地确认任务已经开始。"
        )

        async with WsProbe(f"ws://127.0.0.1:{port}/ws") as probe:
            await probe.request(
                "chat.send",
                {
                    "text": task_prompt,
                    "provider": args.provider,
                    "session_id": session_id,
                    "turn_id": first_turn_id,
                    "source": "e2e_real_conversation",
                },
            )
            first_complete = await probe.wait_event(
                lambda event: event.method == "chat.complete"
                and event.params.get("turn_id") == first_turn_id,
                timeout=args.chat_timeout,
                description="initial real-model chat completion",
            )
            run_created = await probe.wait_event(
                lambda event: event.method == "provider.event"
                and event.params.get("provider") == "codex"
                and event.params.get("type") == "run.created",
                timeout=args.dispatch_timeout,
                description="Codex run.created from the model's DELEGATE",
            )
            codex_run_id = str(run_created.params.get("run_id") or "")

            terminal_before_status = any(
                event.method == "provider.result"
                and event.params.get("run_id") == codex_run_id
                for event in probe.state.events
            )
            status_event_index = len(probe.state.events)
            await probe.request(
                "chat.send",
                {
                    "text": "刚才那个任务现在到哪一步了？只汇报当前状态，不要重试、继续或新建任何任务。",
                    "provider": args.provider,
                    "session_id": session_id,
                    "turn_id": status_turn_id,
                    "source": "e2e_real_conversation",
                },
            )
            status_complete = await probe.wait_event(
                lambda event: event.method == "chat.complete"
                and event.params.get("turn_id") == status_turn_id,
                timeout=args.chat_timeout,
                after=status_event_index,
                description="status follow-up chat completion",
            )

            terminal_result = await probe.wait_event(
                lambda event: event.method == "provider.result"
                and event.params.get("run_id") == codex_run_id,
                timeout=args.provider_timeout,
                description="terminal Codex result",
            )
            try:
                terminal_observer = await probe.wait_event(
                    lambda event: event.method == "chat.observer_decision"
                    and bool(event.params.get("terminal")),
                    timeout=args.observer_timeout,
                    description="terminal WorkObserver report",
                )
            except TimeoutError:
                terminal_observer = None

            await asyncio.sleep(args.settle_seconds)
            work_snapshot = await probe.request("work.list", {})
            events = list(probe.state.events)

        created_run_events = [
            event
            for event in events
            if event.method == "provider.event"
            and event.params.get("provider") == "codex"
            and event.params.get("type") == "run.created"
        ]
        created_run_ids = {
            str(event.params.get("run_id") or "")
            for event in created_run_events
            if str(event.params.get("run_id") or "")
        }
        observer_decisions = [event for event in events if event.method == "chat.observer_decision"]
        progress_decisions = [event for event in observer_decisions if not bool(event.params.get("terminal"))]
        tts_events = [event for event in events if event.method.startswith("tts.")]
        work_projection = (
            work_snapshot.get("work")
            if isinstance(work_snapshot.get("work"), dict)
            else work_snapshot.get("projection")
            if isinstance(work_snapshot.get("projection"), dict)
            else work_snapshot
        )
        items = (
            work_projection.get("items")
            if isinstance(work_projection.get("items"), list)
            else []
        )
        selected = (
            work_projection.get("selected")
            if isinstance(work_projection.get("selected"), dict)
            else {}
        )
        status_text = str(status_complete.params.get("full_text") or "")
        terminal_status = _provider_status(terminal_result)
        evidence = _workspace_evidence(workspace)

        checks = {
            "initial_chat_completed": bool(first_complete.params.get("full_text")),
            "model_dispatched_codex": bool(created_run_ids),
            "status_query_sent_while_provider_active": not terminal_before_status,
            "status_query_completed": bool(status_text),
            "status_query_did_not_delegate": "[DELEGATE" not in status_text.upper(),
            "single_codex_run_after_status_query": len(created_run_ids) == 1,
            "single_work_item": len(items) == 1,
            "single_attempt": int(selected.get("attemptNumber") or 0) == 1,
            "observer_emitted_progress_report": bool(progress_decisions),
            "observer_emitted_terminal_report": terminal_observer is not None,
            "tts_was_bypassed": not tts_events,
            "provider_succeeded": terminal_status in {"done", "succeeded", "completed"},
            "workspace_artifacts_created": all(item["exists"] for item in evidence.values()),
        }
        required = [
            "initial_chat_completed",
            "model_dispatched_codex",
            "status_query_sent_while_provider_active",
            "status_query_completed",
            "status_query_did_not_delegate",
            "single_codex_run_after_status_query",
            "single_work_item",
            "single_attempt",
            "observer_emitted_progress_report",
            "observer_emitted_terminal_report",
            "tts_was_bypassed",
            "provider_succeeded",
            "workspace_artifacts_created",
        ]
        report.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "passed" if all(checks[name] for name in required) else "failed",
                "checks": checks,
                "conversation": {
                    "initial_reply": _safe_excerpt(first_complete.params.get("full_text") or "", 1200),
                    "status_reply": _safe_excerpt(status_text, 1200),
                },
                "provider_result": _safe_excerpt(terminal_result.params, 2000),
                "work_snapshot": _safe_excerpt(work_snapshot, 2000),
                "workspace_evidence": evidence,
                "counts": {
                    "events": len(events),
                    "codex_run_created_events": len(created_run_events),
                    "unique_codex_runs_created": len(created_run_ids),
                    "observer_decisions": len(observer_decisions),
                    "progress_observer_decisions": len(progress_decisions),
                    "tts_events": len(tts_events),
                },
                "events": [event.to_dict() for event in events],
            }
        )
        exit_code = 0 if report["status"] == "passed" else 1
    except Exception as exc:
        report.update(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "events": [event.to_dict() for event in probe.state.events] if probe else [],
            }
        )
    finally:
        if process is not None:
            await _stop_server(int(report.get("port") or 0), process)
        if log_handle is not None:
            log_handle.close()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
            report["paths"]["workspace_cleaned"] = True
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(isolation_dir, ignore_errors=True)
    return exit_code, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Exercise real Amadeus chat routing, Codex work, status follow-up, and Observer reports."
    )
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "deepseek"))
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--chat-timeout", type=float, default=180.0)
    parser.add_argument("--dispatch-timeout", type=float, default=90.0)
    parser.add_argument("--provider-timeout", type=float, default=600.0)
    parser.add_argument("--observer-timeout", type=float, default=90.0)
    parser.add_argument("--settle-seconds", type=float, default=3.0)
    parser.add_argument("--keep-workspace", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    exit_code, report = asyncio.run(_run(args))
    print(json.dumps({
        "status": report.get("status"),
        "report": report.get("paths", {}).get("report"),
        "server_log": report.get("paths", {}).get("server_log"),
        "checks": report.get("checks", {}),
        "error": report.get("error", ""),
    }, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
