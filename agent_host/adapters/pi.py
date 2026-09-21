"""Optional native Pi RPC adapter using the existing Host Provider contract."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from agent_host.pi_rpc import PiCommandRejected, PiRpcClient, PiTransportError
from agent_host.provider_catalog import PI_MANIFEST
from agent_host.provider_identity import with_parent_conversation_context
from agent_host.provider_progress import split_progress_stream, with_progress_contract
from agent_host.provider_types import (
    ProviderActivityEvidence, ProviderEvent, ProviderInputDelivery,
    ProviderPermissionResponse, ProviderRunResult, ProviderSessionHandle,
)
from config import settings

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "pi_runtime"
PI_VERSION = "0.86.1"
PI_AUTH_CHECK_TIMEOUT_S = 10

_PI_MODEL_CREDENTIALS = {
    "DEEPSEEK_API_KEY": "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY": "OPENAI_API_KEY",
    "GEMINI_API_KEY": "GEMINI_API_KEY",
}

_RUNTIME_DIAGNOSTIC = (
    "Run the normal desktop npm ci, or repair the pinned Pi runtime with "
    "npm ci --ignore-scripts --prefix agent_host/pi_runtime."
)
_AUTH_DIAGNOSTIC = (
    "Configure the selected Pi model connection in Settings -> Models (the default "
    "uses DEEPSEEK_API_KEY), then restart the backend."
)


class PiStartupUnavailable(RuntimeError):
    def __init__(self, reason: str, *, authentication: str = "unknown",
                 diagnostic: str = _RUNTIME_DIAGNOSTIC):
        self.availability = {"provider_id": "pi", "ready": False,
            "registered": False, "reason": reason,
            "authentication": authentication, "diagnostic": diagnostic}
        super().__init__(reason)


@dataclass
class _Run:
    client: Any
    emit: Any
    request: Any
    session: ProviderSessionHandle | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    submitted: bool = False
    ready: bool = False
    cancelled: bool = False
    cancel_confirmed: bool = False
    stop_reason: str = ""
    text: str = ""
    final_text: str = ""
    progress_pending: str = ""
    milestones: int = 0
    tools: set[str] = field(default_factory=set)
    permissions: dict[str, dict] = field(default_factory=dict)


class PiAdapter:
    provider_id = "pi"
    manifest = PI_MANIFEST

    def __init__(self, *, client_factory=PiRpcClient, node_path=None,
                 agent_dir=None, model_provider=None, model=None, timeout=None,
                 startup_probe=subprocess.run):
        self.client_factory = client_factory
        self.startup_probe = startup_probe
        self.node_path = node_path or settings.PI_NODE_PATH
        self.agent_dir = Path(agent_dir or settings.PI_AGENT_DIR).expanduser().resolve()
        self.model_provider = settings.PI_MODEL_PROVIDER if model_provider is None else model_provider
        self.model = settings.PI_MODEL if model is None else model
        self.timeout = settings.PI_TIMEOUT_S if timeout is None else timeout
        self._runs: dict[str, _Run] = {}
        self._attached: set[str] = set()

    def require_startup_ready(self):
        if not shutil.which(self.node_path):
            raise PiStartupUnavailable("pi_node_unavailable")
        package = RUNTIME_DIR / "node_modules/@earendil-works/pi-coding-agent/package.json"
        if not package.is_file() or json.loads(package.read_text(encoding="utf-8")).get("version") != PI_VERSION:
            raise PiStartupUnavailable("pi_runtime_missing_or_wrong_version")
        web_package = RUNTIME_DIR / "node_modules/pi-simple-web-tools/package.json"
        if not web_package.is_file() or json.loads(web_package.read_text(encoding="utf-8")).get("version") != "0.1.0":
            raise PiStartupUnavailable("pi_web_component_missing_or_wrong_version")
        extensions = json.loads(settings.PI_EXTENSIONS_JSON)
        if not isinstance(extensions, list) or any(not isinstance(item, str)
                or not Path(item).expanduser().is_file() for item in extensions):
            raise PiStartupUnavailable("pi_extension_configuration_invalid")
        command = [self.node_path, str(self._cli_path()), "auth", "check",
            "--provider", self.model_provider, "--model", self.model,
            "--json", "--no-refresh"]
        try:
            checked = self.startup_probe(command, cwd=str(RUNTIME_DIR),
                env=self._child_environment(), capture_output=True, text=True,
                encoding="utf-8", timeout=PI_AUTH_CHECK_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError):
            raise PiStartupUnavailable("pi_authentication_check_failed",
                diagnostic=_AUTH_DIAGNOSTIC) from None
        try:
            result = json.loads(str(checked.stdout or "").strip())
        except (TypeError, json.JSONDecodeError):
            result = {}
        if checked.returncode != 0 or result.get("status") != "ready":
            reason = {
                "credentials_not_configured": "pi_model_credentials_unavailable",
                "provider_not_found": "pi_model_provider_unavailable",
                "invalid_state": "pi_authentication_configuration_invalid",
            }.get(str(result.get("reason") or ""), "pi_authentication_check_failed")
            raise PiStartupUnavailable(reason, authentication="unavailable",
                diagnostic=_AUTH_DIAGNOSTIC)
        self._startup_readiness = {"authentication": str(result.get("authType") or "available"),
            "version": PI_VERSION,
            "diagnostic": "Pinned Pi runtime and selected model credentials are available; remote access is checked on the first request."}

    @staticmethod
    def _cli_path() -> Path:
        return RUNTIME_DIR / "node_modules/@earendil-works/pi-coding-agent/dist/cli.js"

    def _child_environment(self, *, cwd: Path | None = None, access: str = "none") -> dict[str, str]:
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(self.agent_dir),
            "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0", "PI_OFFLINE": "1"}
        # config.settings owns process-over-desktop-over-dotenv precedence. Pi is
        # a trusted child-process boundary, so pass those same effective model
        # credentials without copying them into command-line arguments.
        for env_key, setting_name in _PI_MODEL_CREDENTIALS.items():
            value = str(getattr(settings, setting_name, "") or "").strip()
            if value:
                env[env_key] = value
        if cwd is not None:
            env.update({"AMADEUS_PI_WORKSPACE": str(cwd), "AMADEUS_PI_ACCESS": access,
                "AMADEUS_PI_PYTHON": sys.executable,
                "AMADEUS_PI_WEB_HELPER": str(RUNTIME_DIR.parent / "pi_web_tools.py"),
                "PI_WEB_TOOLS_CONFIG": str(self.agent_dir / "web-tools.json")})
        return env

    def configuration(self):
        return {"provider_id": "pi", "transport": "native_rpc", "version": PI_VERSION,
            "model_provider": self.model_provider, "model": self.model}

    def _command(self, request):
        command = [self.node_path, str(self._cli_path()),
            "--mode", "rpc", "--no-extensions", "--no-prompt-templates", "--no-themes",
            "--no-approve", "-e", str(RUNTIME_DIR / "amadeus.ts")]
        if self.model_provider:
            command += ["--provider", self.model_provider]
        if self.model:
            command += ["--model", self.model]
        command += ["--session-dir", str(self.agent_dir / "sessions")]
        # The Host, not a workspace settings file, chooses extra native code.
        for extension in json.loads(settings.PI_EXTENSIONS_JSON):
            command += ["-e", str(Path(extension).expanduser().resolve())]
        return command

    async def run(self, request, run_id, emit):
        cwd = Path(request.cwd).resolve() if request.cwd else self.agent_dir / "workspace"
        attached = request.session
        owned_session = os.path.normcase(str(Path(attached.session_id).resolve())) if attached else ""
        if attached and (attached.provider != "pi" or attached.scope != "interaction"):
            return ProviderRunResult(status="error", error="Pi native session identity is invalid")
        if run_id in self._runs or (owned_session and owned_session in self._attached):
            return ProviderRunResult(status="error", error="Pi session already has an active owner")
        if not request.cwd:
            cwd.mkdir(parents=True, exist_ok=True)
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        access = request.requirements.workspace_access if request.requirements else "none"
        env = self._child_environment(cwd=cwd, access=access)
        client = self.client_factory(self._command(request), cwd=str(cwd), env=env)
        run = _Run(client, emit, request)
        self._runs[run_id] = run
        if owned_session:
            self._attached.add(owned_session)
        try:
            await client.start()
            if attached:
                path = Path(attached.session_id)
                if not path.is_absolute() or not path.is_file():
                    raise PiCommandRejected("Pi saved session is unavailable")
                # Native restore also restores cwd. It may not override the
                # workspace already selected and accepted by the Host.
                try:
                    with path.open(encoding="utf-8") as saved:
                        header = json.loads(saved.readline())
                    same_cwd = os.path.normcase(str(Path(header["cwd"]).resolve())) == os.path.normcase(str(cwd))
                except (ValueError, KeyError, TypeError):
                    same_cwd = False
                if not same_cwd:
                    raise PiCommandRejected("Pi saved session workspace does not match the Host")
                switched = await client.request("switch_session", sessionPath=str(path))
                if switched.get("cancelled"):
                    raise PiCommandRejected("Pi session restoration was cancelled")
            state = await client.request("get_state")
            session_file = str(state.get("sessionFile") or "")
            if not session_file or (attached and Path(session_file).resolve() != Path(attached.session_id).resolve()):
                raise PiCommandRejected("Pi did not restore the requested native session")
            if not attached:
                session_key = os.path.normcase(str(Path(session_file).resolve()))
                if session_key in self._attached:
                    raise PiCommandRejected("Pi native session already has an active owner")
                self._attached.add(session_key)
                owned_session = session_key
            run.session = attached or ProviderSessionHandle("pi", session_file, "interaction")
            if state.get("isStreaming") or state.get("pendingMessageCount"):
                raise PiCommandRejected("Pi session is not idle")
            await self._emit(run_id, run, "session.opened", session=run.session)
            async with run.lock:
                if run.cancelled:
                    return ProviderRunResult(status="cancelled", session=run.session)
                task = with_progress_contract(with_parent_conversation_context(
                    request.task, metadata=request.metadata, execution_provider="pi"),
                    presentation_locale=request.metadata.get("presentation_locale"))
                run.submitted = True
                try:
                    await client.request("prompt", message=task)
                except PiCommandRejected:
                    run.submitted = False
                    raise
                run.ready = True
            async with asyncio.timeout(self.timeout):
                while not run.settled.is_set():
                    event = await client.next_event()
                    await self._event(run_id, run, event)
            # An abort response may follow agent_settled. Let the control owner
            # consume that acknowledgement before disposing its transport.
            async with run.lock:
                run.ready = False
                return ProviderRunResult(status="cancelled" if run.cancel_confirmed or run.stop_reason == "aborted"
                    else "error" if run.stop_reason in {"", "error"} else "done",
                    result=run.final_text or run.text.strip(), session=run.session,
                    error="Pi did not complete an assistant response" if run.stop_reason in {"", "error"} else None,
                    activity_evidence=ProviderActivityEvidence(terminal_observed=True,
                        execution_items=len(run.tools), progress_milestones=run.milestones))
        except (PiCommandRejected, PiTransportError, OSError, TimeoutError) as exc:
            return ProviderRunResult(status="orphaned" if run.submitted else "error",
                error=str(exc) if isinstance(exc, (PiTransportError, PiCommandRejected)) else "Pi process or deadline failed",
                result=run.text, session=run.session,
                metadata={"result_type": "transport_outcome_unknown", "runtime_resumable": False}
                if run.submitted else {})
        finally:
            run.ready = False
            for key in list(run.permissions):
                await self._emit(run_id, run, "permission.expired", {"request_id": key})
            await client.close()
            self._runs.pop(run_id, None)
            if owned_session:
                self._attached.discard(owned_session)
            run.done.set()

    async def _emit(self, run_id, run, kind, payload=None, **kwargs):
        await run.emit(ProviderEvent(provider="pi", run_id=run_id,
            type=kind, payload=payload or {}, **kwargs))

    async def _event(self, run_id, run, event):
        kind = event.get("type")
        if kind == "message_update":
            delta = event.get("assistantMessageEvent") or {}
            if delta.get("type") == "text_delta":
                visible, milestones, run.progress_pending = split_progress_stream(
                    run.progress_pending, str(delta.get("delta") or ""))
                run.text += visible
                if visible:
                    await self._emit(run_id, run, "assistant.delta", {"text": visible})
                for milestone in milestones:
                    run.milestones += 1
                    await self._emit(run_id, run, "semantic.progress", milestone)
        elif kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                run.stop_reason = str(message.get("stopReason") or "")
                text = "".join(block.get("text", "") for block in message.get("content", [])
                    if block.get("type") == "text")
                run.final_text, _, _ = split_progress_stream("", text, final=True)
        elif kind in {"tool_execution_start", "tool_execution_end"}:
            key = str(event.get("toolCallId") or "")
            run.tools.add(key)
            await self._emit(run_id, run, "tool.call" if kind.endswith("start") else "tool.result",
                {"tool": event.get("toolName"), "tool_call_id": key,
                    "ok": not event.get("isError", False),
                    "raw": {"input": event.get("args", {})} if kind.endswith("start")
                    else {"output": event.get("result", {})}})
        elif kind == "extension_ui_request":
            key = str(event.get("id") or "")
            if event.get("method") == "confirm":
                run.permissions[key] = event
                await self._emit(run_id, run, "permission.requested", {"permissionRequest": {
                    "request_id": key, "capability": "provider.tool.execute", "action": "execute_tool",
                    "reason": str(event.get("title", "")) + "\n" + str(event.get("message", "")),
                    "options": ["allow_once", "deny"], "retryRequired": False, "diagnosticOnly": False}})
            elif event.get("method") in {"select", "input", "editor"}:
                # No generic form owner exists in Provider permission responses.
                # Refuse rather than fabricating a choice or leaving Pi blocked.
                await run.client.send({"type": "extension_ui_response", "id": key, "cancelled": True})
        elif kind == "extension_error":
            raise PiTransportError("Pi extension failed; execution outcome requires inspection")
        elif kind == "agent_settled":
            visible, milestones, run.progress_pending = split_progress_stream(run.progress_pending, "", final=True)
            run.text += visible
            run.milestones += len(milestones)
            run.settled.set()

    async def append_input(self, run_id, text):
        run = self._runs.get(run_id)
        if run is None:
            return ProviderInputDelivery("rejected", "not_found")
        async with run.lock:
            if not run.ready or run.cancelled or run.settled.is_set():
                return ProviderInputDelivery("rejected", "run_not_active")
            try:
                await run.client.request("steer", message=text)
            except PiCommandRejected:
                return ProviderInputDelivery("rejected", "pi_input_rejected")
            except PiTransportError:
                return ProviderInputDelivery("unknown", "pi_input_acknowledgement_lost")
            try:
                state = await run.client.request("get_state")
                if not state.get("isStreaming") and state.get("pendingMessageCount", 0):
                    # Pi can acknowledge a queue after its last turn settled.
                    # Do not call that delivery to this execution or resend it.
                    return ProviderInputDelivery("unknown", "pi_input_queued_after_settlement")
            except (PiCommandRejected, PiTransportError):
                return ProviderInputDelivery("unknown", "pi_input_settlement_unknown")
            return ProviderInputDelivery("delivered")

    async def resolve_permission(self, run_id, response: ProviderPermissionResponse):
        run = self._runs.get(run_id)
        if run is None or run.cancelled or response.request_id not in run.permissions:
            return {"accepted": False, "reason": "permission_request_not_pending"}
        await run.client.send({"type": "extension_ui_response", "id": response.request_id,
            "confirmed": response.allow})
        run.permissions.pop(response.request_id, None)
        return {"accepted": True}

    async def cancel(self, run_id):
        run = self._runs.get(run_id)
        if run is None:
            return {"confirmed": False, "cancelled": False, "reason": "not_found"}
        async with run.lock:
            if run.settled.is_set():
                return {"confirmed": True, "cancelled": False, "reason": "already_finished"}
            run.cancelled = True
            if not run.submitted:
                run.cancel_confirmed = True
                return {"confirmed": True, "cancelled": True}
            try:
                for key in list(run.permissions):
                    await run.client.send({"type": "extension_ui_response", "id": key, "confirmed": False})
                await run.client.request("clear_queue")
                await run.client.request("abort")
            except (PiCommandRejected, PiTransportError):
                return {"confirmed": False, "cancelled": False, "reason": "pi_abort_unconfirmed"}
            run.cancel_confirmed = True
            return {"confirmed": True, "cancelled": True, "session": run.session}

    async def close(self):
        runs = tuple(self._runs.items())
        await asyncio.gather(*(self.cancel(key) for key, _ in runs), return_exceptions=True)
        await asyncio.gather(*(run.client.close() for _, run in runs), return_exceptions=True)
