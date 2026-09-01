"""Thin non-interactive Codex CLI provider.

The adapter owns process transport and native JSONL mapping only.  Amadeus
continues to own workspace selection, Task/Attempt identity, cancellation
truth, UI projection, and user acceptance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from config import settings
from agent_host.provider_catalog import DIRECT_CODEX_MANIFEST
from agent_host.provider_progress import (
    split_progress_milestones,
    with_progress_contract,
)
from agent_host.provider_authoring import (
    requires_auip_authoring,
    with_host_authoring_capabilities,
)
from agent_host.provider_identity import with_parent_conversation_context
from agent_host.mcp_connections import (
    McpConnectionSpec,
    codex_mcp_config_overrides,
    load_mcp_connections,
    mcp_provider_environment,
)
from agent_host.provider_types import (
    EmitProviderEvent,
    ProviderEvent,
    ProviderRunRequest,
    ProviderRunResult,
)


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization)\b"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")
_KEY_SECRET_RE = re.compile(r"\b(?:sk|rk|pk|api)[-_][A-Za-z0-9_-]{8,}\b", re.I)


class DirectCodexStartupUnavailable(RuntimeError):
    """The configured CLI cannot safely be advertised as runnable."""

    def __init__(self, availability: dict[str, Any]) -> None:
        self.availability = dict(availability)
        reason = str(availability.get("reason") or "startup_preflight_failed")
        super().__init__(f"Direct Codex startup preflight failed: {reason}")


class DirectCodexAdapter:
    provider_id = "codex"
    manifest = DIRECT_CODEX_MANIFEST

    def __init__(
        self,
        *,
        cli_path: str | None = None,
        prefix_args: Iterable[str] | None = None,
        timeout_s: float | None = None,
        silence_warn_s: float | None = None,
        ignore_user_config: bool | None = None,
        stderr_cap_bytes: int | None = None,
        mcp_connections: tuple[McpConnectionSpec, ...] | None = None,
    ) -> None:
        self.cli_path = str(cli_path or settings.DIRECT_CODEX_CLI_PATH or "codex")
        if prefix_args is None:
            raw_prefix = str(settings.DIRECT_CODEX_CLI_PREFIX_ARGS or "").strip()
            self.prefix_args = tuple(shlex.split(raw_prefix, posix=True)) if raw_prefix else ()
        else:
            self.prefix_args = tuple(str(value) for value in prefix_args if str(value))
        self.timeout_s = max(
            1.0,
            float(timeout_s if timeout_s is not None else settings.DIRECT_CODEX_TIMEOUT_S),
        )
        self.silence_warn_s = max(
            0.0,
            float(
                silence_warn_s
                if silence_warn_s is not None
                else settings.DIRECT_CODEX_EVENT_SILENCE_WARN_S
            ),
        )
        self.ignore_user_config = (
            bool(settings.DIRECT_CODEX_IGNORE_USER_CONFIG)
            if ignore_user_config is None
            else bool(ignore_user_config)
        )
        self.stderr_cap_bytes = max(
            1024,
            int(
                stderr_cap_bytes
                if stderr_cap_bytes is not None
                else settings.DIRECT_CODEX_STDERR_CAP_BYTES
            ),
        )
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._startup_readiness: dict[str, Any] | None = None
        self._mcp_connections = (
            tuple(mcp_connections)
            if mcp_connections is not None
            else load_mcp_connections()
        )
        self._mcp_environment = mcp_provider_environment(
            self._mcp_connections,
            "codex",
        )

    def startup_readiness(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Check executable transport and existing authentication without mutating either."""

        timeout = max(
            1.0,
            float(
                timeout_s
                if timeout_s is not None
                else settings.DIRECT_CODEX_PREFLIGHT_TIMEOUT_S
            ),
        )
        base = {
            "provider_id": self.provider_id,
            "configured": True,
            "ready": False,
            "registered": False,
            "cli": self.cli_path,
            "version": "",
            "authentication": "unknown",
            "reason": "startup_preflight_failed",
            "diagnostic": "",
        }
        version = self._startup_probe(("--version",), timeout_s=timeout)
        if not version["ok"]:
            snapshot = {
                **base,
                "reason": str(version["reason"]),
                "diagnostic": str(version["diagnostic"]),
            }
            self._startup_readiness = snapshot
            return dict(snapshot)

        authentication = self._startup_probe(("login", "status"), timeout_s=timeout)
        if not authentication["ok"]:
            snapshot = {
                **base,
                "version": str(version["diagnostic"]).splitlines()[0][:160],
                "authentication": "unavailable",
                "reason": "authentication_unavailable",
                "diagnostic": str(authentication["diagnostic"]),
            }
            self._startup_readiness = snapshot
            return dict(snapshot)

        snapshot = {
            **base,
            "ready": True,
            "version": str(version["diagnostic"]).splitlines()[0][:160],
            "authentication": "available",
            "reason": "ready",
            "diagnostic": "",
        }
        self._startup_readiness = snapshot
        return dict(snapshot)

    def require_startup_ready(self) -> dict[str, Any]:
        snapshot = self.startup_readiness()
        if snapshot.get("ready") is not True:
            raise DirectCodexStartupUnavailable(snapshot)
        return snapshot

    def _startup_probe(
        self,
        args: tuple[str, ...],
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        command = [self.cli_path, *self.prefix_args, *args]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                check=False,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "reason": "preflight_timeout",
                "diagnostic": f"Codex preflight exceeded {timeout_s:g}s",
            }
        except OSError as exc:
            return {
                "ok": False,
                "reason": "cli_start_failed",
                "diagnostic": self._redact_text(str(exc), limit=800),
            }
        output = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if str(part or "").strip()
        )
        diagnostic = self._redact_text(output, limit=800)
        return {
            "ok": result.returncode == 0,
            "reason": "ready" if result.returncode == 0 else "cli_probe_failed",
            "diagnostic": diagnostic or f"Codex exited with code {result.returncode}",
        }

    async def run(
        self,
        request: ProviderRunRequest,
        run_id: str,
        emit: EmitProviderEvent,
    ) -> ProviderRunResult:
        cwd = self._validated_cwd(request.cwd)
        if cwd is None:
            return ProviderRunResult(
                status="error",
                error="Direct Codex requires an existing caller-supplied workspace",
            )

        sandbox = self._sandbox_for(request)
        mutation_required = bool(
            request.requirements is not None
            and request.requirements.task_kind == "workspace_mutation"
            and request.requirements.workspace_access == "write"
        )
        workspace_before = (
            await asyncio.to_thread(self._git_workspace_fingerprint, cwd)
            if mutation_required
            else None
        )
        command = self._build_command(cwd, sandbox=sandbox)
        try:
            process = await self._spawn(command, cwd)
        except (FileNotFoundError, OSError) as exc:
            return ProviderRunResult(
                status="error",
                error=f"Direct Codex CLI could not start: {exc}",
                metadata={
                    "codex": {
                        "cli": self.cli_path,
                        "cwd": str(cwd),
                        "sandbox": sandbox,
                    }
                },
            )

        self._processes[run_id] = process
        stderr_task = asyncio.create_task(
            self._read_bounded_stderr(process.stderr),
            name=f"codex-stderr:{run_id}",
        )
        stream_state: dict[str, Any] = {
            "thread_id": "",
            "final_message": "",
            "usage": {},
            "turn_completed": False,
            "failure": "",
            "native_events": 0,
            "tool_failures": 0,
            "semantic_seen": set(),
        }
        timed_out = False
        try:
            await self._write_prompt(
                process,
                with_progress_contract(
                    with_host_authoring_capabilities(
                        with_parent_conversation_context(
                            request.task,
                            metadata=request.metadata,
                            execution_provider=self.provider_id,
                        ),
                        require_auip_preparation=requires_auip_authoring(
                            (request.metadata or {}).get("source")
                        ),
                        authoring_skill_path=str(
                            (request.metadata or {}).get("auip_authoring_skill_path")
                            or ""
                        ),
                    ),
                    presentation_locale=(request.metadata or {}).get("presentation_locale"),
                ),
            )
            timeout_s = self._request_timeout(request)
            try:
                await asyncio.wait_for(
                    self._consume_stdout(process, run_id, emit, stream_state, request),
                    timeout=timeout_s,
                )
                return_code = await process.wait()
            except asyncio.TimeoutError:
                timed_out = True
                await self._terminate_process(process)
                return_code = process.returncode if process.returncode is not None else -1
        except asyncio.CancelledError:
            await self._terminate_process(process)
            await asyncio.gather(stderr_task, return_exceptions=True)
            raise
        except Exception as exc:
            await self._terminate_process(process)
            diagnostics, diagnostics_truncated = await stderr_task
            return ProviderRunResult(
                status="error",
                error=f"Direct Codex transport failed: {exc}",
                metadata={
                    "codex": {
                        "cwd": str(cwd),
                        "sandbox": sandbox,
                        "diagnostics": diagnostics,
                        "diagnostics_truncated": diagnostics_truncated,
                    }
                },
            )
        finally:
            self._processes.pop(run_id, None)

        diagnostics, diagnostics_truncated = await stderr_task
        workspace_after = (
            await asyncio.to_thread(self._git_workspace_fingerprint, cwd)
            if mutation_required
            else None
        )
        workspace_changed = (
            workspace_before != workspace_after
            if workspace_before is not None and workspace_after is not None
            else None
        )
        metadata = {
            "codex": {
                "thread_id": stream_state["thread_id"],
                "usage": dict(stream_state["usage"]),
                "exit_code": return_code,
                "cwd": str(cwd),
                "sandbox": sandbox,
                "native_events": int(stream_state["native_events"]),
                "tool_failures": int(stream_state["tool_failures"]),
                "diagnostics": diagnostics,
                "diagnostics_truncated": diagnostics_truncated,
                "isolated_user_config": self.ignore_user_config,
                "workspace_changed": workspace_changed,
            }
        }
        if timed_out:
            return ProviderRunResult(
                status="error",
                error=f"Direct Codex timed out after {self._request_timeout(request):g}s",
                metadata=metadata,
            )
        failure = str(stream_state.get("failure") or "").strip()
        if return_code != 0:
            return ProviderRunResult(
                status="error",
                error=failure or diagnostics or f"Codex exited with code {return_code}",
                metadata=metadata,
            )
        if failure:
            return ProviderRunResult(status="error", error=failure, metadata=metadata)
        if stream_state.get("turn_completed") is not True:
            return ProviderRunResult(
                status="error",
                error="Codex exited without a turn.completed event",
                metadata=metadata,
            )
        if mutation_required and workspace_changed is False:
            return ProviderRunResult(
                status="error",
                error=(
                    "Codex completed without an observable workspace change "
                    "for a workspace mutation task"
                ),
                metadata=metadata,
            )
        return ProviderRunResult(
            status="done",
            result=str(stream_state.get("final_message") or ""),
            metadata=metadata,
        )

    async def cancel(self, run_id: str) -> dict[str, Any]:
        process = self._processes.get(str(run_id or ""))
        if process is None:
            return {"confirmed": False, "cancelled": False, "reason": "process_not_found"}
        confirmed = await self._terminate_process(process)
        return {
            "confirmed": confirmed,
            "cancelled": confirmed,
            "reason": "process_tree_stopped" if confirmed else "process_still_running",
        }

    def _build_command(self, cwd: Path, *, sandbox: str) -> list[str]:
        command = [
            self.cli_path,
            *self.prefix_args,
        ]
        for override in codex_mcp_config_overrides(self._mcp_connections):
            command.extend(("--config", override))
        command.extend(("exec", "--json", "--ephemeral", "--color", "never"))
        if self.ignore_user_config:
            command.append("--ignore-user-config")
        command.extend(["--sandbox", sandbox, "-C", str(cwd), "-"])
        return command

    @staticmethod
    def _sandbox_for(request: ProviderRunRequest) -> str:
        requirements = request.requirements
        if requirements is not None and requirements.workspace_access == "write":
            return "workspace-write"
        return "read-only"

    def _request_timeout(self, request: ProviderRunRequest) -> float:
        raw = request.metadata.get("direct_codex_timeout_s")
        try:
            return max(1.0, float(raw)) if raw not in (None, "") else self.timeout_s
        except (TypeError, ValueError):
            return self.timeout_s

    @staticmethod
    def _validated_cwd(value: str | None) -> Path | None:
        if not value:
            return None
        try:
            path = Path(value).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        return path if path.is_dir() else None

    async def _spawn(self, command: list[str], cwd: Path) -> asyncio.subprocess.Process:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if self._mcp_environment:
            kwargs["env"] = {**os.environ, **self._mcp_environment}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        return await asyncio.create_subprocess_exec(*command, **kwargs)

    @staticmethod
    async def _write_prompt(process: asyncio.subprocess.Process, task: str) -> None:
        if process.stdin is None:
            raise RuntimeError("Direct Codex stdin is unavailable")
        process.stdin.write(str(task or "").encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        try:
            await process.stdin.wait_closed()
        except (AttributeError, BrokenPipeError, ConnectionResetError):
            pass

    async def _consume_stdout(
        self,
        process: asyncio.subprocess.Process,
        run_id: str,
        emit: EmitProviderEvent,
        state: dict[str, Any],
        request: ProviderRunRequest,
    ) -> None:
        if process.stdout is None:
            raise RuntimeError("Direct Codex stdout is unavailable")
        stalled_at = 0.0
        last_event_at = time.time()
        line_task: asyncio.Task[bytes] | None = None
        try:
            while True:
                line_task = asyncio.create_task(process.stdout.readline())
                if self.silence_warn_s > 0:
                    while not line_task.done():
                        done, _pending = await asyncio.wait(
                            {line_task},
                            timeout=self.silence_warn_s,
                        )
                        if done:
                            break
                        now = time.time()
                        if stalled_at <= 0:
                            stalled_at = now
                        await emit(
                            ProviderEvent(
                                provider=self.provider_id,
                                run_id=run_id,
                                type="run.status",
                                payload={
                                    "status": "running",
                                    "liveness": "stalled",
                                    "stage": "waiting_for_codex",
                                    "silence_s": round(now - last_event_at, 1),
                                    "elapsed_s": round(now - stalled_at, 1),
                                    "observed_at": now,
                                    "reason": "provider_event_silence",
                                },
                                metadata=self._event_metadata({}, state),
                            )
                        )
                line = await line_task
                line_task = None
                if not line:
                    break
                now = time.time()
                if stalled_at > 0:
                    await emit(
                        ProviderEvent(
                            provider=self.provider_id,
                            run_id=run_id,
                            type="run.status",
                            payload={
                                "status": "running",
                                "liveness": "active",
                                "stage": "codex_event_resumed",
                                "recovered": True,
                                "stall_duration_s": round(now - stalled_at, 1),
                                "last_provider_event_at": now,
                                "observed_at": now,
                            },
                            metadata=self._event_metadata({}, state),
                        )
                    )
                    stalled_at = 0.0
                last_event_at = now
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    native = json.loads(text)
                except json.JSONDecodeError:
                    await emit(
                        ProviderEvent(
                            provider=self.provider_id,
                            run_id=run_id,
                            type="codex.event",
                            payload={"native_type": "malformed_jsonl", "text": self._bounded(text, 800)},
                            metadata=self._event_metadata({}, state),
                        )
                    )
                    continue
                if not isinstance(native, dict):
                    continue
                state["native_events"] = int(state.get("native_events") or 0) + 1
                for event in self._map_event(native, run_id, state, request):
                    await emit(event)
        finally:
            if line_task is not None and not line_task.done():
                line_task.cancel()
                await asyncio.gather(line_task, return_exceptions=True)

    def _map_event(
        self,
        native: dict[str, Any],
        run_id: str,
        state: dict[str, Any],
        request: ProviderRunRequest,
    ) -> list[ProviderEvent]:
        native_type = str(native.get("type") or "").strip()
        if native_type == "thread.started":
            state["thread_id"] = str(native.get("thread_id") or "").strip()
            return [self._event(run_id, "run.status", {"status": "running", "stage": "thread_started"}, native, state)]
        if native_type == "turn.started":
            return [self._event(run_id, "run.status", {"status": "running", "stage": "turn_started"}, native, state)]
        if native_type == "turn.completed":
            usage = native.get("usage") if isinstance(native.get("usage"), dict) else {}
            state["usage"] = dict(usage)
            state["turn_completed"] = True
            return [self._event(run_id, "run.status", {"status": "running", "stage": "turn_completed", "usage": dict(usage)}, native, state)]
        if native_type in {"turn.failed", "error"}:
            message = self._error_message(native)
            state["failure"] = message
            return [self._event(run_id, "run.failed", {"message": message}, native, state)]
        if not native_type.startswith("item."):
            return [self._event(run_id, "codex.event", {"native_type": native_type or "unknown"}, native, state)]

        item = native.get("item") if isinstance(native.get("item"), dict) else {}
        item_type = str(item.get("type") or "unknown").strip().lower()
        phase = native_type.rsplit(".", 1)[-1]
        status = str(item.get("status") or phase).strip().lower()
        if item_type == "reasoning":
            return []
        if item_type == "agent_message":
            text = str(item.get("text") or "")
            assistant_text, milestones = split_progress_milestones(text)
            events: list[ProviderEvent] = []
            seen = state.setdefault("semantic_seen", set())
            for milestone in milestones:
                summary = str(milestone.get("summary") or "")
                key = f"{milestone.get('milestone')}:{' '.join(summary.casefold().split())}"
                if not key or key in seen:
                    continue
                seen.add(key)
                events.append(
                    self._event(
                        run_id,
                        "semantic.progress",
                        {**milestone, "source": "direct_codex_provider_progress"},
                        native,
                        state,
                    )
                )
            if assistant_text:
                state["final_message"] = assistant_text
                events.append(
                    self._event(
                        run_id,
                        "assistant.delta",
                        {"text": assistant_text},
                        native,
                        state,
                    )
                )
            return events

        if item_type == "file_change":
            changes = self._bounded_changes(item.get("changes"))
            if phase == "started":
                return [
                    self._event(
                        run_id,
                        "tool.call",
                        {"tool": "file_change", "item_id": str(item.get("id") or ""), "changes": changes},
                        native,
                        state,
                    )
                ]
            ok = status in {"completed", "done", "success"}
            if not ok:
                state["tool_failures"] = int(state.get("tool_failures") or 0) + 1
            events = [
                self._event(
                    run_id,
                    "tool.result",
                    {"tool": "file_change", "item_id": str(item.get("id") or ""), "ok": ok, "status": status, "changes": changes},
                    native,
                    state,
                )
            ]
            if ok:
                for change in changes:
                    events.append(
                        self._event(
                            run_id,
                            "artifact.created",
                            {"artifact_type": "file", **change},
                            native,
                            state,
                        )
                    )
            return events

        if item_type == "command_execution":
            command = self._bounded(str(item.get("command") or ""), 1200)
            if phase == "started":
                return [
                    self._event(
                        run_id,
                        "tool.call",
                        {"tool": "command_execution", "item_id": str(item.get("id") or ""), "command": command},
                        native,
                        state,
                    )
                ]
            exit_code = item.get("exit_code")
            ok = status in {"completed", "done", "success"} and exit_code in (None, 0)
            if not ok:
                state["tool_failures"] = int(state.get("tool_failures") or 0) + 1
            return [
                self._event(
                    run_id,
                    "tool.result",
                    {
                        "tool": "command_execution",
                        "item_id": str(item.get("id") or ""),
                        "ok": ok,
                        "status": status,
                        "exit_code": exit_code,
                        "output": self._bounded(str(item.get("aggregated_output") or ""), 2000),
                    },
                    native,
                    state,
                )
            ]

        if phase == "started":
            return [
                self._event(
                    run_id,
                    "tool.call",
                    {"tool": item_type, "item_id": str(item.get("id") or "")},
                    native,
                    state,
                )
            ]
        ok = status not in {"failed", "error", "cancelled", "canceled", "declined"}
        if not ok:
            state["tool_failures"] = int(state.get("tool_failures") or 0) + 1
        return [
            self._event(
                run_id,
                "tool.result",
                {
                    "tool": item_type,
                    "item_id": str(item.get("id") or ""),
                    "ok": ok,
                    "status": status,
                },
                native,
                state,
            )
        ]

    def _event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        native: dict[str, Any],
        state: dict[str, Any],
    ) -> ProviderEvent:
        return ProviderEvent(
            provider=self.provider_id,
            run_id=run_id,
            type=event_type,
            payload=payload,
            metadata=self._event_metadata(native, state),
        )

    @staticmethod
    def _event_metadata(native: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        item = native.get("item") if isinstance(native.get("item"), dict) else {}
        return {
            "codex": {
                "native_type": str(native.get("type") or ""),
                "thread_id": str(state.get("thread_id") or ""),
                "item_id": str(item.get("id") or ""),
                "item_type": str(item.get("type") or ""),
                "status": str(item.get("status") or ""),
            }
        }

    @classmethod
    def _bounded_changes(cls, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        changes: list[dict[str, str]] = []
        for raw in value[:32]:
            if not isinstance(raw, dict):
                continue
            path = cls._bounded(str(raw.get("path") or ""), 2048)
            kind = cls._bounded(str(raw.get("kind") or "change"), 80)
            if path:
                changes.append({"path": path, "kind": kind})
        return changes

    @classmethod
    def _error_message(cls, native: dict[str, Any]) -> str:
        error = native.get("error")
        if isinstance(error, dict):
            value = error.get("message") or error.get("error") or error
        else:
            value = error or native.get("message") or "Codex turn failed"
        return cls._bounded(str(value), 2000)

    async def _read_bounded_stderr(
        self,
        stream: asyncio.StreamReader | None,
    ) -> tuple[str, bool]:
        if stream is None:
            return "", False
        buffer = bytearray()
        total = 0
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            total += len(chunk)
            buffer.extend(chunk)
            if len(buffer) > self.stderr_cap_bytes:
                del buffer[: len(buffer) - self.stderr_cap_bytes]
        text = self._redact_text(
            buffer.decode("utf-8", errors="replace").strip(),
            limit=self.stderr_cap_bytes,
        )
        return text, total > self.stderr_cap_bytes

    @classmethod
    def _redact_text(cls, value: str, *, limit: int) -> str:
        text = str(value or "").strip()
        text = _BEARER_RE.sub("Bearer [REDACTED]", text)
        text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
        text = _KEY_SECRET_RE.sub("[REDACTED]", text)
        return cls._bounded(text, limit)

    @staticmethod
    def _git_workspace_fingerprint(cwd: Path) -> str | None:
        """Hash Git-visible workspace state without retaining file contents."""

        try:
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            return None
        if status.returncode != 0:
            return None
        digest = hashlib.sha256()
        digest.update(status.stdout)
        diff: subprocess.Popen[bytes] | None = None
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
            digest.update(head.stdout if head.returncode == 0 else b"NO_HEAD")
            diff = subprocess.Popen(
                ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if diff.stdout is not None:
                while True:
                    chunk = diff.stdout.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
            diff.wait(timeout=30)
        except (OSError, subprocess.SubprocessError):
            if diff is not None and diff.poll() is None:
                diff.kill()
                try:
                    diff.wait(timeout=5)
                except subprocess.SubprocessError:
                    pass
            digest.update(b"GIT_DIFF_UNAVAILABLE")

        root = cwd.resolve()
        for entry in status.stdout.split(b"\0"):
            if not entry.startswith(b"?? "):
                continue
            relative = entry[3:].decode("utf-8", errors="surrogateescape")
            try:
                target = (root / relative).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    continue
                digest.update(relative.encode("utf-8", errors="surrogateescape"))
                with target.open("rb") as handle:
                    while True:
                        chunk = handle.read(65536)
                        if not chunk:
                            break
                        digest.update(chunk)
            except (OSError, RuntimeError, ValueError):
                continue
        return digest.hexdigest()

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> bool:
        if process.returncode is not None:
            return True
        if os.name == "nt" and process.pid:
            kwargs: dict[str, Any] = {
                "stdout": asyncio.subprocess.DEVNULL,
                "stderr": asyncio.subprocess.DEVNULL,
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
            }
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    **kwargs,
                )
                await asyncio.wait_for(killer.wait(), timeout=10.0)
            except (OSError, asyncio.TimeoutError):
                pass
        else:
            try:
                process.terminate()
            except ProcessLookupError:
                return True
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                return False
        return process.returncode is not None

    @staticmethod
    def _bounded(value: str, limit: int) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."
