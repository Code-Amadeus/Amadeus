"""Thin mapping from the official Codex SDK to Amadeus Provider contracts.

The SDK owns the App Server process, JSON-RPC transport, native thread/turn
state, event routing and execution controls. This module deliberately owns
only the boundary mapping required by :mod:`agent_host.provider_runtime`.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, cast

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
from openai_codex._sandbox import _sandbox_mode
from openai_codex.api import AsyncThread
from openai_codex.async_client import AsyncCodexClient
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import (
    AskForApproval,
    AskForApprovalValue,
    ConfigWriteResponse,
    ReasoningEffort,
    ThreadInjectItemsResponse,
    ThreadResumeParams,
    ThreadStartParams,
)

from agent_host.codex_desktop_provider import (
    CodexDesktopProviderBridge,
    build_codex_desktop_provider_bridge,
    provider_auth_overrides,
)
from agent_host.provider_authoring import (
    requires_auip_authoring,
    with_host_authoring_capabilities,
)
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_handoff import (
    CODEX_HANDOFF_CONVERSATION_CONTRACT,
    codex_handoff_presentation,
    provider_recovery_user_message,
)
from agent_host.mcp_connections import (
    McpConnectionSpec,
    codex_mcp_config_overrides,
    load_mcp_connections,
    mcp_provider_environment,
)
from agent_host.provider_identity import with_main_role_reference
from agent_host.provider_progress import split_progress_stream, with_progress_contract
from agent_host.provider_types import (
    EmitProviderEvent,
    ProviderActivityEvidence,
    ProviderEvent,
    ProviderPermissionResponse,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderSessionHandle,
    ProviderSteerRequest,
)
from config import settings


class CodexAppServerStartupUnavailable(RuntimeError):
    """The official SDK/runtime pair is not available to the Host."""

    def __init__(self, availability: dict[str, Any]) -> None:
        self.availability = dict(availability)
        reason = str(availability.get("reason") or "startup_preflight_failed")
        super().__init__(f"Codex SDK startup preflight failed: {reason}")


class _TurnDeadlineExceeded(TimeoutError):
    """A native turn exceeded the Host's bounded wait and was interrupted."""


_NON_EXECUTION_THREAD_ITEMS = frozenset(
    {
        "userMessage",
        "hookPrompt",
        "agentMessage",
        "plan",
        "reasoning",
        "enteredReviewMode",
        "exitedReviewMode",
        "contextCompaction",
    }
)


@dataclass(slots=True)
class _ActiveTurn:
    run_id: str
    thread_id: str
    turn_id: str
    handle: Any
    loop: asyncio.AbstractEventLoop
    emit: EmitProviderEvent
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_status: str = ""
    cancel_requested: bool = False


@dataclass(slots=True)
class _StreamState:
    final_message: str = ""
    native_events: int = 0
    tool_failures: int = 0
    terminal_observed: bool = False
    progress_pending: str = ""
    plan_pending: dict[str, str] = field(default_factory=dict)
    milestones: set[tuple[str, str]] = field(default_factory=set)
    execution_items: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _PendingApproval:
    request_id: str
    run_id: str
    method: str
    params: dict[str, Any]
    resolved: threading.Event = field(default_factory=threading.Event)
    allow: bool = False
    resolution_reason: str = ""


class _ApprovalAwareAsyncCodexClient(AsyncCodexClient):
    """Official async client with the official sync approval hook exposed."""

    def __init__(self, config: CodexConfig, approval_handler: Callable[..., Any]) -> None:
        # openai-codex 0.147 exposes the handler on CodexClient while its async
        # convenience constructor does not yet forward it. Reuse that public
        # client instead of implementing JSON-RPC or process management here.
        self._sync = CodexClient(config=config, approval_handler=approval_handler)

    async def thread_inject_items(
        self,
        thread_id: str,
        items: list[dict[str, Any]],
    ) -> ThreadInjectItemsResponse:
        """Append model-visible context through the official App Server client."""

        return await self._call_sync(
            self._sync.request,
            "thread/inject_items",
            {"threadId": thread_id, "items": items},
            response_model=ThreadInjectItemsResponse,
        )

    async def config_batch_write(
        self,
        edits: list[dict[str, Any]],
        *,
        file_path: str,
    ) -> ConfigWriteResponse:
        """Merge provider metadata through App Server's config writer."""

        return await self._call_sync(
            self._sync.request,
            "config/batchWrite",
            {
                "edits": edits,
                "filePath": file_path,
                "reloadUserConfig": True,
            },
            response_model=ConfigWriteResponse,
        )


class _ApprovalAwareAsyncCodex(AsyncCodex):
    """Keep the official high-level thread/turn API with approval callbacks."""

    def __init__(self, config: CodexConfig, approval_handler: Callable[..., Any]) -> None:
        super().__init__(config)
        self._client = _ApprovalAwareAsyncCodexClient(config, approval_handler)

    async def thread_start_host_approval(
        self,
        *,
        cwd: str,
        model: str | None,
        model_provider: str | None,
        sandbox: Sandbox,
        service_tier: str | None,
    ) -> AsyncThread:
        """Start a typed official thread whose approvals belong to the Host."""

        await self._ensure_initialized()
        started = await self._client.thread_start(
            ThreadStartParams(
                approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
                approvals_reviewer=None,
                cwd=cwd,
                model=model,
                model_provider=model_provider,
                sandbox=_sandbox_mode(sandbox),
                service_tier=service_tier,
            )
        )
        return AsyncThread(self, started.thread.id)

    async def thread_resume_host_approval(
        self,
        thread_id: str,
        *,
        cwd: str,
        model: str | None,
        model_provider: str | None,
        sandbox: Sandbox,
        service_tier: str | None,
    ) -> AsyncThread:
        """Resume a typed official thread without installing an auto reviewer."""

        await self._ensure_initialized()
        resumed = await self._client.thread_resume(
            thread_id,
            ThreadResumeParams(
                thread_id=thread_id,
                approval_policy=AskForApproval(root=AskForApprovalValue.on_request),
                approvals_reviewer=None,
                cwd=cwd,
                model=model,
                model_provider=model_provider,
                sandbox=_sandbox_mode(sandbox),
                service_tier=service_tier,
            ),
        )
        return AsyncThread(self, resumed.thread.id)

    async def thread_inject_items(
        self,
        thread_id: str,
        items: list[dict[str, Any]],
    ) -> ThreadInjectItemsResponse:
        await self._ensure_initialized()
        client = cast(_ApprovalAwareAsyncCodexClient, self._client)
        return await client.thread_inject_items(thread_id, items)

    async def thread_set_name(self, thread_id: str, name: str) -> Any:
        await self._ensure_initialized()
        return await self._client.thread_set_name(thread_id, name)

    async def config_batch_write(
        self,
        edits: list[dict[str, Any]],
        *,
        file_path: str,
    ) -> ConfigWriteResponse:
        await self._ensure_initialized()
        client = cast(_ApprovalAwareAsyncCodexClient, self._client)
        return await client.config_batch_write(edits, file_path=file_path)


class CodexAppServerAdapter:
    """Expose Codex as a Provider without reimplementing its runtime."""

    provider_id = "codex"
    manifest = CODEX_APP_SERVER_MANIFEST

    def __init__(
        self,
        *,
        codex: Any | None = None,
        codex_factory: Callable[[CodexConfig], Any] | None = None,
        codex_bin: str | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        provider_base_url: str | None = None,
        provider_api_key_env: str | None = None,
        provider_auth_env_file: str | None = None,
        sync_desktop_provider: bool | None = None,
        desktop_config_path: str | None = None,
        approval_mode: str | None = None,
        turn_timeout_s: float | None = None,
        cancel_confirm_timeout_s: float | None = None,
        approval_timeout_s: float | None = None,
        mcp_connections: tuple[McpConnectionSpec, ...] | None = None,
    ) -> None:
        self._injected_codex = codex is not None
        self._codex = codex
        self._codex_factory = codex_factory
        self._codex_bin = str(
            codex_bin
            if codex_bin is not None
            else getattr(settings, "CODEX_APP_SERVER_CODEX_BIN", "")
        ).strip()
        self.model = str(
            model
            if model is not None
            else getattr(settings, "CODEX_APP_SERVER_MODEL", "")
        ).strip()
        self.model_provider = str(
            model_provider
            if model_provider is not None
            else getattr(settings, "CODEX_APP_SERVER_MODEL_PROVIDER", "")
        ).strip().lower()
        self.reasoning_effort_label = str(
            reasoning_effort
            if reasoning_effort is not None
            else getattr(settings, "CODEX_APP_SERVER_REASONING_EFFORT", "")
        ).strip().lower()
        self.reasoning_effort = self._reasoning_effort(self.reasoning_effort_label)
        self.service_tier = self._service_tier(
            service_tier
            if service_tier is not None
            else getattr(settings, "CODEX_APP_SERVER_SERVICE_TIER", "")
        )
        self.provider_base_url = str(
            provider_base_url
            if provider_base_url is not None
            else getattr(settings, "CODEX_APP_SERVER_PROVIDER_BASE_URL", "")
        ).strip().rstrip("/")
        self.provider_api_key_env = str(
            provider_api_key_env
            if provider_api_key_env is not None
            else getattr(settings, "CODEX_APP_SERVER_PROVIDER_API_KEY_ENV", "")
        ).strip()
        project_root = Path(__file__).resolve().parents[2]
        self.provider_auth_env_file = Path(
            provider_auth_env_file
            if provider_auth_env_file is not None
            else getattr(
                settings,
                "CODEX_APP_SERVER_PROVIDER_AUTH_ENV_FILE",
                str(project_root / ".env"),
            )
        ).expanduser()
        sync_requested = bool(
            sync_desktop_provider
            if sync_desktop_provider is not None
            else getattr(settings, "CODEX_APP_SERVER_SYNC_DESKTOP_PROVIDER", True)
        )
        # Injected clients are normally test doubles or externally owned SDK
        # instances. They opt into user-profile writes explicitly.
        self.sync_desktop_provider = sync_requested and (
            not self._injected_codex or sync_desktop_provider is True
        )
        self.desktop_config_path = (
            Path(desktop_config_path).expanduser()
            if desktop_config_path is not None
            else None
        )
        self.approval_mode = self._approval_mode(
            approval_mode
            if approval_mode is not None
            else getattr(settings, "CODEX_APP_SERVER_APPROVAL_MODE", "host")
        )
        self.turn_timeout_s = max(
            1.0,
            float(
                turn_timeout_s
                if turn_timeout_s is not None
                else getattr(settings, "CODEX_APP_SERVER_TURN_TIMEOUT_S", 7200)
            ),
        )
        self.cancel_confirm_timeout_s = max(
            1.0,
            float(
                cancel_confirm_timeout_s
                if cancel_confirm_timeout_s is not None
                else getattr(
                    settings,
                    "CODEX_APP_SERVER_CANCEL_CONFIRM_TIMEOUT_S",
                    30,
                )
            ),
        )
        self.approval_timeout_s = max(
            1.0,
            float(
                approval_timeout_s
                if approval_timeout_s is not None
                else getattr(settings, "CODEX_APP_SERVER_APPROVAL_TIMEOUT_S", 900)
            ),
        )
        self._mcp_connections = (
            tuple(mcp_connections)
            if mcp_connections is not None
            else load_mcp_connections()
        )
        self._active: dict[str, _ActiveTurn] = {}
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._approval_condition = threading.Condition()
        self._sdk_lock = asyncio.Lock()
        self._desktop_provider_lock = asyncio.Lock()
        self._desktop_provider_bridge: CodexDesktopProviderBridge | None = None
        self._desktop_provider_sync_status = "pending" if self.sync_desktop_provider else "disabled"

    def startup_readiness(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Check the published SDK/runtime installation without launching it."""

        del timeout_s
        base = {
            "provider_id": self.provider_id,
            "configured": True,
            "ready": False,
            "registered": False,
            "transport": "openai-codex-sdk",
            "authentication": "delegated_to_codex",
            "model": self.model,
            "model_provider": self.model_provider,
            "reasoning_effort": self.reasoning_effort_label,
            "service_tier": self.service_tier or "",
            "reason": "sdk_unavailable",
            "sdk_version": "",
            "runtime_version": "",
        }
        if self._injected_codex:
            return {**base, "ready": True, "reason": "injected_sdk"}
        try:
            sdk_version = importlib_metadata.version("openai-codex")
            runtime_version = importlib_metadata.version("openai-codex-cli-bin")
        except importlib_metadata.PackageNotFoundError:
            return base
        return {
            **base,
            "ready": True,
            "reason": "ready",
            "sdk_version": sdk_version,
            "runtime_version": runtime_version,
        }

    def require_startup_ready(self) -> dict[str, Any]:
        snapshot = self.startup_readiness()
        if snapshot.get("ready") is not True:
            raise CodexAppServerStartupUnavailable(snapshot)
        return snapshot

    async def close(self) -> None:
        self._release_pending_approvals()
        codex = self._codex
        self._codex = None
        if codex is not None and not self._injected_codex:
            await codex.close()

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
                error="Codex requires an existing Host-owned cwd",
            )

        active: _ActiveTurn | None = None
        state = _StreamState()
        try:
            codex = await self._ensure_codex()
            await self._ensure_desktop_provider_config(codex)
            thread = await self._open_thread(codex, request, cwd)
            task_text = await self._prepare_desktop_handoff(
                codex,
                thread,
                request,
                rename_thread=request.session is None,
            )
            turn = await thread.turn(
                task_text,
                approval_mode=self.approval_mode,
                cwd=str(cwd),
                effort=self.reasoning_effort,
                model=self.model or None,
                sandbox=self._sandbox(request),
                service_tier=self.service_tier,
            )
            active = _ActiveTurn(
                run_id=run_id,
                thread_id=str(thread.id),
                turn_id=str(turn.id),
                handle=turn,
                loop=asyncio.get_running_loop(),
                emit=emit,
            )
            with self._approval_condition:
                self._active[run_id] = active
                self._approval_condition.notify_all()
            terminal_payload = await self._consume_with_deadline(
                active,
                state,
                emit,
                timeout_s=self._request_timeout(request),
            )
            status = self._turn_status(terminal_payload)
            active.terminal_status = status
            active.terminal.set()
            final_message = state.final_message.strip()
            metadata = self._result_metadata(active, state, cwd, status)
            activity_evidence = self._activity_evidence(state)
            if status == "completed":
                return ProviderRunResult(
                    status="done",
                    result=final_message,
                    metadata=metadata,
                    activity_evidence=activity_evidence,
                    session=self._session(active.thread_id),
                )
            if status == "interrupted":
                return ProviderRunResult(
                    status="cancelled",
                    result=final_message,
                    metadata=metadata,
                    activity_evidence=activity_evidence,
                    session=self._session(active.thread_id),
                )
            error = self._turn_error(terminal_payload) or f"Codex turn ended as {status}"
            return ProviderRunResult(
                status="error",
                result=final_message,
                error=error,
                metadata=metadata,
                activity_evidence=activity_evidence,
                session=self._session(active.thread_id),
            )
        except _TurnDeadlineExceeded:
            return ProviderRunResult(
                status="error",
                error=f"Codex turn timed out after {self._request_timeout(request):g}s",
                metadata=(
                    self._result_metadata(active, state, cwd, "timeout")
                    if active is not None
                    else {"codex": {"cwd": str(cwd), "status": "timeout"}}
                ),
                activity_evidence=(
                    self._activity_evidence(state) if active is not None else None
                ),
                session=self._session(active.thread_id) if active is not None else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ProviderRunResult(
                status="error",
                error=f"Codex SDK run failed: {exc}",
                metadata={
                    "codex": {
                        "cwd": str(cwd),
                        "thread_id": active.thread_id if active is not None else "",
                        "turn_id": active.turn_id if active is not None else "",
                    }
                },
                activity_evidence=(
                    self._activity_evidence(state) if active is not None else None
                ),
                session=self._session(active.thread_id) if active is not None else None,
            )
        finally:
            if active is not None:
                self._release_pending_approvals(run_id)
                with self._approval_condition:
                    self._active.pop(run_id, None)

    async def resolve_permission(
        self,
        run_id: str,
        response: ProviderPermissionResponse,
    ) -> dict[str, Any]:
        """Release exactly one native approval callback for this active run."""

        with self._approval_condition:
            pending = self._pending_approvals.get(response.request_id)
            if pending is None:
                return {"accepted": False, "reason": "permission_request_not_pending"}
            if pending.run_id != str(run_id or "").strip():
                return {"accepted": False, "reason": "permission_run_mismatch"}
            pending.allow = bool(response.allow)
            pending.resolution_reason = "user"
            pending.resolved.set()
        return {"accepted": True}

    async def steer(
        self,
        run_id: str,
        request: ProviderSteerRequest,
    ) -> dict[str, Any]:
        active = self._active.get(str(run_id or "").strip())
        if active is None or active.terminal.is_set():
            return {"accepted": False, "reason": "active_turn_not_found"}
        try:
            await active.handle.steer(str(request.task or "").strip())
        except Exception as exc:
            return {"accepted": False, "reason": str(exc) or exc.__class__.__name__}
        return {
            "accepted": True,
            "revision": int(request.revision),
            "safe_boundary": "provider_native",
            "thread_id": active.thread_id,
            "turn_id": active.turn_id,
        }

    async def cancel(self, run_id: str) -> dict[str, Any]:
        active = self._active.get(str(run_id or "").strip())
        if active is None:
            return {"confirmed": False, "cancelled": False, "reason": "not_found"}
        try:
            active.cancel_requested = True
            await active.handle.interrupt()
            await asyncio.wait_for(
                active.terminal.wait(),
                timeout=self.cancel_confirm_timeout_s,
            )
        except asyncio.TimeoutError:
            return {
                "confirmed": False,
                "cancelled": False,
                "reason": "interrupt_not_terminal",
            }
        except Exception as exc:
            return {
                "confirmed": False,
                "cancelled": False,
                "reason": str(exc) or exc.__class__.__name__,
            }
        cancelled = active.terminal_status == "interrupted"
        return {
            "confirmed": True,
            "cancelled": cancelled,
            "reason": "interrupted" if cancelled else "turn_completed_before_interrupt",
            "thread_id": active.thread_id,
            "turn_id": active.turn_id,
        }

    async def _ensure_codex(self) -> Any:
        if self._codex is not None:
            return self._codex
        async with self._sdk_lock:
            if self._codex is None:
                config = CodexConfig(
                    codex_bin=self._codex_bin or None,
                    config_overrides=(
                        *self._provider_config_overrides(),
                        *self._service_tier_config_overrides(),
                        *codex_mcp_config_overrides(self._mcp_connections),
                    ),
                    env=self._codex_process_env_with_provider_key(),
                    client_name="amadeus",
                    client_title="Amadeus",
                )
                self._codex = (
                    self._codex_factory(config)
                    if self._codex_factory is not None
                    else _ApprovalAwareAsyncCodex(config, self._handle_sdk_approval)
                )
        return self._codex

    def _provider_config_overrides(self) -> tuple[str, ...]:
        """Declare a custom Responses provider without owning its runtime."""

        bridge = self._provider_bridge()
        return provider_auth_overrides(bridge) if bridge is not None else ()

    def _service_tier_config_overrides(self) -> tuple[str, ...]:
        """Enable the Codex Fast feature only for this managed runtime."""

        if self.service_tier == "fast":
            return ("features.fast_mode=true",)
        return ()

    def _provider_bridge(self) -> CodexDesktopProviderBridge | None:
        if self._desktop_provider_bridge is not None:
            return self._desktop_provider_bridge
        self._desktop_provider_bridge = build_codex_desktop_provider_bridge(
            provider_id=self.model_provider,
            base_url=self.provider_base_url,
            api_key_env=self.provider_api_key_env,
            project_root=Path(__file__).resolve().parents[2],
            env_file=self.provider_auth_env_file,
            config_path=self.desktop_config_path,
        )
        return self._desktop_provider_bridge

    async def _ensure_desktop_provider_config(self, codex: Any) -> None:
        if not self.sync_desktop_provider or self._desktop_provider_sync_status == "ready":
            return
        async with self._desktop_provider_lock:
            if self._desktop_provider_sync_status == "ready":
                return
            bridge = self._provider_bridge()
            if bridge is None:
                self._desktop_provider_sync_status = "not_applicable"
                return
            if not bridge.needs_write:
                self._desktop_provider_sync_status = "ready"
                return
            batch_write = getattr(codex, "config_batch_write", None)
            if not callable(batch_write):
                self._desktop_provider_sync_status = "unsupported"
                return
            await batch_write(
                [bridge.config_edit()],
                file_path=str(bridge.config_path),
            )
            self._desktop_provider_sync_status = "ready"

    def _codex_process_env_with_provider_key(self) -> dict[str, str] | None:
        runtime_env = dict(self._codex_process_env() or {})
        if self.provider_api_key_env:
            # The App Server obtains the token through the same command-backed
            # contract that makes persisted threads resumable in Desktop. Do
            # not also expose the secret as a long-lived child environment
            # variable; the helper reads the Amadeus env file on demand.
            runtime_env[self.provider_api_key_env] = ""
        runtime_env.update(mcp_provider_environment(self._mcp_connections, "codex"))
        return runtime_env or None

    @staticmethod
    def _codex_process_env(environ: dict[str, str] | None = None) -> dict[str, str] | None:
        """Keep the official Windows runtime away from app-execution aliases.

        A restricted Windows token cannot reliably launch the zero-hop
        ``WindowsApps\\pwsh.exe`` alias.  The official runtime needs an actual
        executable on PATH; approval policy and sandbox scope remain unchanged.
        """

        if os.name != "nt":
            return None
        source = dict(os.environ if environ is None else environ)
        path_key = next((key for key in source if key.upper() == "PATH"), "PATH")
        entries = [value for value in source.get(path_key, "").split(os.pathsep) if value]

        def is_windows_apps(value: str) -> bool:
            normalized = str(Path(value)).replace("\\", "/").rstrip("/").casefold()
            return normalized.endswith("/microsoft/windowsapps")

        real_shell_dirs = [
            value
            for value in entries
            if not is_windows_apps(value) and (Path(value) / "pwsh.exe").is_file()
        ]
        if real_shell_dirs:
            selected = real_shell_dirs[0]
            ordered = [selected, *(value for value in entries if value != selected)]
        else:
            # With no real pwsh installation, removing only the app alias lets
            # Codex select the system Windows PowerShell already present on a
            # standard Windows PATH.
            ordered = [value for value in entries if not is_windows_apps(value)]
        normalized_path = os.pathsep.join(ordered)
        return {path_key: normalized_path} if normalized_path else None

    def _handle_sdk_approval(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Bridge an official SDK callback to the canonical permission lane."""

        source = dict(params or {})
        thread_id = str(source.get("threadId") or "").strip()
        turn_id = str(source.get("turnId") or "").strip()
        with self._approval_condition:
            deadline = time.monotonic() + 5.0
            active = self._active_for_native_turn(thread_id, turn_id)
            while active is None and time.monotonic() < deadline:
                self._approval_condition.wait(timeout=max(0.0, deadline - time.monotonic()))
                active = self._active_for_native_turn(thread_id, turn_id)
            if active is None:
                return self._native_approval_response(method, source, allow=False)
            request_id = self._approval_request_id(method, source)
            pending = _PendingApproval(
                request_id=request_id,
                run_id=active.run_id,
                method=method,
                params=source,
            )
            self._pending_approvals[request_id] = pending

        announced: Future[Any] = asyncio.run_coroutine_threadsafe(
            active.emit(
                self._event(
                    active,
                    "permission.requested",
                    {"permissionRequest": self._permission_payload(method, source, request_id)},
                )
            ),
            active.loop,
        )

        def announcement_done(completed: Future[Any]) -> None:
            try:
                completed.result()
            except Exception:
                # A real publication failure must fail closed, but a slow
                # event bus is not failure. The previous 10-second Future
                # timeout returned `decline` while the UI was still rendering
                # the card, leaving a dead permission_request_not_pending
                # surface. Keep the native request pending until one of the
                # actual lifecycle events below resolves it.
                with self._approval_condition:
                    current = self._pending_approvals.get(request_id)
                    if current is pending and not pending.resolved.is_set():
                        pending.resolution_reason = "announcement_failed"
                        pending.resolved.set()

        announced.add_done_callback(announcement_done)

        wait_finished = pending.resolved.wait(timeout=self.approval_timeout_s)
        with self._approval_condition:
            # A user decision racing the deadline wins if it acquired the
            # pending request first. Otherwise freeze one timeout result and
            # remove the request before any late click can report success.
            if not wait_finished and not pending.resolved.is_set():
                pending.resolution_reason = "approval_timeout"
                pending.allow = False
                pending.resolved.set()
            reason = pending.resolution_reason
            automatic_expiry = reason in {
                "announcement_failed",
                "approval_timeout",
                "run_released",
            }
            if automatic_expiry:
                pending.allow = False
            native_allow = pending.allow
            self._pending_approvals.pop(request_id, None)
        if automatic_expiry:
            expiry: Future[Any] = asyncio.run_coroutine_threadsafe(
                active.emit(
                    self._event(
                        active,
                        "permission.expired",
                        {
                            "request_id": request_id,
                            "decision": "expired",
                            "automatic": True,
                            "reason": reason,
                        },
                    )
                ),
                active.loop,
            )

            def observe_expiry(completed: Future[Any]) -> None:
                try:
                    completed.result()
                except Exception:
                    pass

            expiry.add_done_callback(observe_expiry)

        return self._native_approval_response(method, source, allow=native_allow)

    def _active_for_native_turn(self, thread_id: str, turn_id: str) -> _ActiveTurn | None:
        return next(
            (
                active
                for active in self._active.values()
                if active.thread_id == thread_id and active.turn_id == turn_id
            ),
            None,
        )

    def _release_pending_approvals(self, run_id: str = "") -> None:
        clean_run_id = str(run_id or "").strip()
        with self._approval_condition:
            for pending in tuple(self._pending_approvals.values()):
                if clean_run_id and pending.run_id != clean_run_id:
                    continue
                pending.allow = False
                pending.resolution_reason = "run_released"
                pending.resolved.set()

    @staticmethod
    def _approval_request_id(method: str, params: dict[str, Any]) -> str:
        kind = method.rsplit("/", 2)[-2] if "/" in method else "approval"
        native_id = str(params.get("approvalId") or params.get("itemId") or "request")
        return f"codex:{params.get('turnId') or 'turn'}:{kind}:{native_id}"[:240]

    @staticmethod
    def _permission_payload(
        method: str,
        params: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        scope: list[str] = []
        for candidate in (params.get("cwd"), params.get("grantRoot")):
            value = str(candidate or "").strip()
            if value and value not in scope:
                scope.append(value)
        network = params.get("networkApprovalContext")
        if isinstance(network, dict) and str(network.get("host") or "").strip():
            scope.append(str(network.get("host"))[:256])
        if method == "item/commandExecution/requestApproval":
            capability, action, tool = "shell.execute", "execute_command", "shell"
            fallback = str(params.get("command") or "").strip()
        elif method == "item/fileChange/requestApproval":
            capability, action, tool = "filesystem.write", "apply_file_change", "file_change"
            fallback = str(params.get("grantRoot") or "").strip()
        else:
            capability, action, tool = "sandbox.permissions", "grant_permissions", "sandbox"
            permissions = params.get("permissions")
            fallback = "Codex needs additional bounded sandbox permissions."
            if isinstance(permissions, dict):
                scope.extend(
                    str(value)[:256]
                    for value in permissions
                    if str(value).strip() and str(value) not in scope
                )
        reason = str(params.get("reason") or "").strip()
        if not reason:
            reason = fallback[:320] or "Codex requires explicit approval to continue."
        return {
            "request_id": request_id,
            "capability": capability,
            "action": action,
            "tool": tool,
            "scope": scope[:16],
            "reason": reason[:1000],
            "reversibility": "unknown",
            "options": ["allow_once", "deny"],
            "retryRequired": False,
            "diagnosticOnly": False,
        }

    @staticmethod
    def _native_approval_response(
        method: str,
        params: dict[str, Any],
        *,
        allow: bool,
    ) -> dict[str, Any]:
        if method == "item/permissions/requestApproval":
            return {
                "permissions": dict(params.get("permissions") or {}) if allow else {},
                "scope": "turn",
            }
        return {"decision": "accept" if allow else "decline"}

    async def _open_thread(self, codex: Any, request: ProviderRunRequest, cwd: Path) -> Any:
        sandbox = self._sandbox(request)
        if self.approval_mode is None:
            if request.session is not None:
                if request.session.provider != self.provider_id:
                    raise ValueError("attached Provider Session does not belong to Codex")
                start = getattr(codex, "thread_resume_host_approval", None)
                if callable(start):
                    return await start(
                        request.session.session_id,
                        cwd=str(cwd),
                        model=self.model or None,
                        model_provider=self.model_provider or None,
                        sandbox=sandbox,
                        service_tier=self.service_tier,
                    )
                return await codex.thread_resume(
                    request.session.session_id,
                    approval_mode=None,
                    cwd=str(cwd),
                    model=self.model or None,
                    model_provider=self.model_provider or None,
                    sandbox=sandbox,
                    service_tier=self.service_tier,
                )
            start = getattr(codex, "thread_start_host_approval", None)
            if callable(start):
                return await start(
                    cwd=str(cwd),
                    model=self.model or None,
                    model_provider=self.model_provider or None,
                    sandbox=sandbox,
                    service_tier=self.service_tier,
                )
            return await codex.thread_start(
                approval_mode=None,
                cwd=str(cwd),
                model=self.model or None,
                model_provider=self.model_provider or None,
                sandbox=sandbox,
                service_tier=self.service_tier,
            )
        if request.session is not None:
            if request.session.provider != self.provider_id:
                raise ValueError("attached Provider Session does not belong to Codex")
            return await codex.thread_resume(
                request.session.session_id,
                approval_mode=self.approval_mode,
                cwd=str(cwd),
                model=self.model or None,
                model_provider=self.model_provider or None,
                sandbox=sandbox,
                service_tier=self.service_tier,
            )
        return await codex.thread_start(
            approval_mode=self.approval_mode,
            cwd=str(cwd),
            model=self.model or None,
            model_provider=self.model_provider or None,
            sandbox=sandbox,
            service_tier=self.service_tier,
        )

    async def _consume_stream(
        self,
        active: _ActiveTurn,
        state: _StreamState,
        emit: EmitProviderEvent,
    ) -> dict[str, Any]:
        terminal_payload: dict[str, Any] = {}
        async for notification in active.handle.stream():
            state.native_events += 1
            method = str(getattr(notification, "method", "") or "")
            payload = self._payload_dict(getattr(notification, "payload", None))
            for event in self._map_notification(method, payload, active, state):
                if not active.cancel_requested:
                    await emit(event)
            if method == "turn/completed":
                terminal_payload = payload
                state.terminal_observed = True
                active.terminal_status = self._turn_status(payload)
                active.terminal.set()
        if state.progress_pending:
            visible, milestones, _pending = split_progress_stream(
                state.progress_pending,
                "",
                final=True,
            )
            if visible:
                await emit(self._event(active, "assistant.delta", {"text": visible}))
            for event in self._record_progress_milestones(active, state, milestones):
                await emit(event)
            state.progress_pending = ""
        if not terminal_payload:
            raise RuntimeError("Codex stream ended without turn/completed")
        return terminal_payload

    async def _consume_with_deadline(
        self,
        active: _ActiveTurn,
        state: _StreamState,
        emit: EmitProviderEvent,
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        """Race the SDK stream against a real wall-clock deadline.

        ``asyncio.wait_for`` waits for a cancelled async generator to finish.
        The official stream correctly remains open until a native terminal
        notification, so cancellation alone can accidentally erase the outer
        timeout.  Race without cancelling, interrupt natively at the deadline,
        then give that terminal event one bounded drain window.
        """

        stream_task = asyncio.create_task(
            self._consume_stream(active, state, emit),
            name=f"codex-stream:{active.run_id}",
        )
        completed, _pending = await asyncio.wait(
            {stream_task},
            timeout=max(1.0, float(timeout_s)),
        )
        if stream_task in completed:
            return stream_task.result()

        try:
            await active.handle.interrupt()
        except Exception:
            stream_task.cancel()
            raise _TurnDeadlineExceeded from None
        drained, _pending = await asyncio.wait(
            {stream_task},
            timeout=self.cancel_confirm_timeout_s,
        )
        if stream_task not in drained:
            stream_task.cancel()
        else:
            # Retrieve any stream exception so the task does not become an
            # unobserved background failure. Timeout remains the user truth.
            try:
                stream_task.result()
            except Exception:
                pass
        raise _TurnDeadlineExceeded

    def _map_notification(
        self,
        method: str,
        payload: dict[str, Any],
        active: _ActiveTurn,
        state: _StreamState,
    ) -> list[ProviderEvent]:
        if method == "item/plan/delta":
            item_id = str(payload.get("itemId") or payload.get("item_id") or "")
            if item_id:
                state.plan_pending[item_id] = (
                    state.plan_pending.get(item_id, "")
                    + str(payload.get("delta") or "")
                )[-2400:]
            return []

        if method == "turn/plan/updated":
            explanation = " ".join(str(payload.get("explanation") or "").split())
            rows = payload.get("plan") if isinstance(payload.get("plan"), list) else []
            steps = [
                " ".join(str(row.get("step") or "").split())
                for row in rows
                if isinstance(row, dict) and str(row.get("step") or "").strip()
            ]
            return self._native_plan_milestone(
                active,
                state,
                explanation or "; ".join(steps[:4]),
            )

        if method == "item/agentMessage/delta":
            visible, milestones, pending = split_progress_stream(
                state.progress_pending,
                str(payload.get("delta") or ""),
            )
            state.progress_pending = pending
            events: list[ProviderEvent] = []
            if visible:
                events.append(self._event(active, "assistant.delta", {"text": visible}))
            events.extend(self._record_progress_milestones(active, state, milestones))
            return events

        if method not in {"item/started", "item/completed"}:
            return []
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or "")
        if item_type == "plan":
            if method != "item/completed":
                return []
            text = str(item.get("text") or state.plan_pending.get(item_id) or "")
            state.plan_pending.pop(item_id, None)
            return self._native_plan_milestone(active, state, text)
        if item_type == "agentMessage" and method == "item/completed":
            visible, milestones = self._without_progress_markers(
                str(item.get("text") or "")
            )
            events = self._record_progress_milestones(
                active,
                state,
                milestones,
                dedupe_kind=True,
            )
            phase = str(item.get("phase") or "").strip().lower()
            if phase == "final_answer":
                state.final_message = visible.strip()
                return events
            # Codex commentary is the same bounded, user-facing direction the
            # Slice already shows. Preserve the completed message as an
            # unverified assistant update so the observer may narrate its
            # intent without promoting it to an implementation/result fact.
            if phase == "commentary" and visible.strip():
                events.append(
                    self._event(
                        active,
                        "assistant.update",
                        {
                            "text": visible.strip(),
                            "source": "codex_native_agent_message",
                            "explicit": False,
                            "status": "reported_direction",
                        },
                    )
                )
                return events
            if not state.final_message:
                state.final_message = visible.strip()
            return events
        if item_type in _NON_EXECUTION_THREAD_ITEMS:
            return []
        # Fail closed for native execution evidence. The App Server can add new
        # tool/item variants before this adapter learns how to present them;
        # treating an unknown started/completed item as "no execution" could
        # replay a collaboration, image generation, or other side effect.
        safe_item_type = item_type or "unknownNativeItem"
        state.execution_items.add(
            item_id or f"{safe_item_type}:{state.native_events}"
        )
        name = self._tool_name(safe_item_type, item)
        if method == "item/started":
            return [
                self._event(
                    active,
                    "tool.call",
                    {
                        "name": name,
                        "item_id": item_id,
                        "input": self._tool_input(safe_item_type, item),
                    },
                )
            ]
        success = self._item_succeeded(item)
        if not success:
            state.tool_failures += 1
        result_payload: dict[str, Any] = {
            "name": name,
            "item_id": item_id,
            "success": success,
            "status": str(item.get("status") or ""),
            "output": self._tool_output(item),
        }
        if safe_item_type == "fileChange":
            result_payload["changes"] = self._file_changes(item)
        events = [
            self._event(
                active,
                "tool.result",
                result_payload,
            )
        ]
        if safe_item_type == "fileChange" and success:
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            summary = (
                f"Codex applied {len(changes)} file change"
                f"{'s' if len(changes) != 1 else ''}."
            )
            identity = ("capability", summary)
            if identity not in state.milestones:
                state.milestones.add(identity)
                events.append(
                    self._event(
                        active,
                        "semantic.progress",
                        {
                            "milestone": "capability",
                            "summary": summary,
                            "source": "codex_native_file_change",
                            "explicit": False,
                            "verified": False,
                            "status": "reported",
                        },
                    )
                )
        return events

    def _record_progress_milestones(
        self,
        active: _ActiveTurn,
        state: _StreamState,
        milestones: list[dict[str, Any]],
        *,
        dedupe_kind: bool = False,
    ) -> list[ProviderEvent]:
        events: list[ProviderEvent] = []
        for milestone in milestones:
            summary = str(milestone.get("summary") or "")
            identity = (
                str(milestone.get("milestone") or ""),
                summary,
            )
            if dedupe_kind and any(
                existing_kind == identity[0]
                for existing_kind, _summary in state.milestones
            ):
                continue
            if any(
                existing_kind == identity[0]
                and (
                    summary.startswith(existing_summary)
                    or existing_summary.startswith(summary)
                )
                for existing_kind, existing_summary in state.milestones
            ):
                continue
            if identity in state.milestones:
                continue
            state.milestones.add(identity)
            events.append(self._event(active, "semantic.progress", milestone))
        return events

    def _native_plan_milestone(
        self,
        active: _ActiveTurn,
        state: _StreamState,
        text: str,
    ) -> list[ProviderEvent]:
        """Project one native completed plan as reported design evidence."""

        if any(kind == "design" for kind, _summary in state.milestones):
            return []
        visible, _milestones = self._without_progress_markers(str(text or ""))
        summary = " ".join(visible.split())[:320]
        if not summary:
            return []
        identity = ("design", summary)
        state.milestones.add(identity)
        return [
            self._event(
                active,
                "semantic.progress",
                {
                    "milestone": "design",
                    "summary": summary,
                    "source": "codex_native_plan",
                    "explicit": False,
                    "verified": False,
                    "status": "reported",
                },
            )
        ]

    def _event(
        self,
        active: _ActiveTurn,
        event_type: str,
        payload: dict[str, Any],
    ) -> ProviderEvent:
        return ProviderEvent(
            provider=self.provider_id,
            run_id=active.run_id,
            type=event_type,
            payload=payload,
            metadata={
                "native_thread_id": active.thread_id,
                "native_turn_id": active.turn_id,
            },
        )

    async def _interrupt(self, active: _ActiveTurn) -> None:
        try:
            await active.handle.interrupt()
            await asyncio.wait_for(
                active.terminal.wait(),
                timeout=self.cancel_confirm_timeout_s,
            )
        except Exception:
            return

    def _task_text(self, request: ProviderRunRequest) -> str:
        metadata = request.metadata or {}
        execution_contract = with_progress_contract(
            with_host_authoring_capabilities(
                with_main_role_reference(
                    request.task,
                    metadata=metadata,
                    execution_provider=self.provider_id,
                ),
                source_user_text=str(metadata.get("source_user_text") or ""),
                source_user_context=str(metadata.get("source_user_context") or ""),
                require_auip_preparation=requires_auip_authoring(
                    metadata.get("source")
                ),
                authoring_skill_path=str(metadata.get("auip_authoring_skill_path") or ""),
            ),
            presentation_locale=metadata.get("presentation_locale"),
        )
        return f"{execution_contract}\n\n{CODEX_HANDOFF_CONVERSATION_CONTRACT}"

    async def _prepare_desktop_handoff(
        self,
        codex: Any,
        thread: Any,
        request: ProviderRunRequest,
        *,
        rename_thread: bool,
    ) -> str:
        """Keep the persisted user turn readable while preserving model policy."""

        metadata = request.metadata or {}
        presentation = codex_handoff_presentation(
            request.task,
            source_user_text=str(metadata.get("source_user_text") or ""),
            source_user_context=str(metadata.get("source_user_context") or ""),
            presentation_locale=metadata.get("presentation_locale"),
        )
        user_message = (
            provider_recovery_user_message(
                presentation_locale=metadata.get("presentation_locale")
            )
            if isinstance(metadata.get("provider_recovery"), dict)
            else presentation.user_message
        )
        inject = getattr(codex, "thread_inject_items", None)
        if not callable(inject):
            # Bounded compatibility for injected legacy SDK clients. The
            # shipping wrapper implements model-visible item injection.
            return self._task_text(request)

        await inject(
            str(thread.id),
            [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Amadeus execution context for the immediately following "
                                "user turn. This supersedes earlier Amadeus execution context.\n\n"
                                + self._task_text(request)
                            ),
                        }
                    ],
                }
            ],
        )
        if rename_thread:
            set_name = getattr(codex, "thread_set_name", None)
            if callable(set_name):
                try:
                    await set_name(str(thread.id), presentation.thread_name)
                except Exception:
                    # The execution context is authoritative; a cosmetic title
                    # failure must not discard a valid Provider turn.
                    pass
        return user_message

    def _sandbox(self, request: ProviderRunRequest) -> Sandbox:
        requirements = request.requirements
        if requirements is not None and requirements.workspace_access == "write":
            return Sandbox.workspace_write
        return Sandbox.read_only

    def _request_timeout(self, request: ProviderRunRequest) -> float:
        raw = (request.metadata or {}).get("timeout_s")
        if raw is None:
            return self.turn_timeout_s
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            return self.turn_timeout_s

    def _result_metadata(
        self,
        active: _ActiveTurn,
        state: _StreamState,
        cwd: Path,
        status: str,
    ) -> dict[str, Any]:
        return {
            "codex": {
                "sdk": "openai-codex",
                "sdk_version": self.startup_readiness().get("sdk_version", ""),
                "thread_id": active.thread_id,
                "turn_id": active.turn_id,
                "turn_status": status,
                "model": self.model,
                "model_provider": self.model_provider,
                "reasoning_effort": self.reasoning_effort_label,
                "service_tier": self.service_tier or "",
                "desktop_provider_config": self._desktop_provider_sync_status,
                "cwd": str(cwd),
                "native_events": state.native_events,
                "tool_failures": state.tool_failures,
            }
        }

    @staticmethod
    def _activity_evidence(state: _StreamState) -> ProviderActivityEvidence:
        return ProviderActivityEvidence(
            terminal_observed=state.terminal_observed,
            progress_milestones=len(state.milestones),
            execution_items=len(state.execution_items),
        )

    def _session(self, thread_id: str) -> ProviderSessionHandle:
        return ProviderSessionHandle(
            provider=self.provider_id,
            session_id=thread_id,
            scope="work_item",
        )

    @staticmethod
    def _payload_dict(payload: Any) -> dict[str, Any]:
        if hasattr(payload, "model_dump"):
            value = payload.model_dump(by_alias=True, mode="json")
            return value if isinstance(value, dict) else {}
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _turn_status(payload: dict[str, Any]) -> str:
        turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
        return str(turn.get("status") or "unknown").strip().lower()

    @staticmethod
    def _turn_error(payload: dict[str, Any]) -> str:
        turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
        error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
        return str(error.get("message") or "").strip()

    @staticmethod
    def _without_progress_markers(text: str) -> tuple[str, list[dict[str, Any]]]:
        visible, milestones, pending = split_progress_stream("", text, final=True)
        return f"{visible}{pending}", milestones

    @staticmethod
    def _tool_name(item_type: str, item: dict[str, Any]) -> str:
        if item_type == "commandExecution":
            return "shell"
        if item_type == "fileChange":
            return "file_change"
        return str(item.get("tool") or item.get("name") or item_type)

    @staticmethod
    def _tool_input(item_type: str, item: dict[str, Any]) -> Any:
        if item_type == "commandExecution":
            return {"command": str(item.get("command") or "")[:2000]}
        if item_type == "fileChange":
            return {"changes": CodexAppServerAdapter._file_changes(item)}
        return item.get("arguments") or item.get("query") or {}

    @staticmethod
    def _file_changes(item: dict[str, Any]) -> list[dict[str, str]]:
        raw = item.get("changes") if isinstance(item.get("changes"), list) else []
        changes: list[dict[str, str]] = []
        for value in raw[:100]:
            if not isinstance(value, dict):
                continue
            path = str(value.get("path") or value.get("file") or "")[:1200]
            kind_value = value.get("kind")
            if isinstance(kind_value, dict):
                kind = str(kind_value.get("type") or "")[:80]
            else:
                kind = str(kind_value or "")[:80]
            diff = str(value.get("diff") or value.get("patch") or "")[:8000]
            if path or kind or diff:
                changes.append({"path": path, "kind": kind, "diff": diff})
        return changes

    @staticmethod
    def _tool_output(item: dict[str, Any]) -> str:
        value = (
            item.get("aggregatedOutput")
            or item.get("result")
            or item.get("output")
            or ""
        )
        text = " ".join(str(value).split())
        return text if len(text) <= 2000 else f"{text[:1997].rstrip()}..."

    @staticmethod
    def _item_succeeded(item: dict[str, Any]) -> bool:
        status = str(item.get("status") or "").strip().lower()
        if status in {"failed", "declined", "error"}:
            return False
        exit_code = item.get("exitCode")
        return not isinstance(exit_code, int) or exit_code == 0

    @staticmethod
    def _validated_cwd(value: str | None) -> Path | None:
        if not str(value or "").strip():
            return None
        try:
            path = Path(str(value)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        return path if path.is_dir() else None

    @staticmethod
    def _approval_mode(value: Any) -> ApprovalMode | None:
        normalized = str(value or "host").strip().lower().replace("-", "_")
        if normalized == "host":
            return None
        if normalized == "auto_review":
            return ApprovalMode.auto_review
        if normalized == "deny_all":
            return ApprovalMode.deny_all
        raise ValueError(
            "CODEX_APP_SERVER_APPROVAL_MODE must be host, auto_review or deny_all"
        )

    @staticmethod
    def _reasoning_effort(value: Any) -> ReasoningEffort | None:
        normalized = str(value or "").strip().lower().replace("-", "")
        if not normalized:
            return None
        by_name = {
            member.value.replace("-", ""): member
            for member in ReasoningEffort
        }
        if normalized in by_name:
            return by_name[normalized]
        # Current App Server advertises max/ultra before the generated enum's
        # static member table does. Its forward-compatible constructor retains
        # those wire values exactly; older runtimes still get the xhigh bound.
        if normalized == "max":
            try:
                return ReasoningEffort("max")
            except ValueError:
                return ReasoningEffort.xhigh
        allowed = ", ".join(by_name)
        raise ValueError(
            f"CODEX_APP_SERVER_REASONING_EFFORT must be one of: {allowed}"
        )

    @staticmethod
    def _service_tier(value: Any) -> str | None:
        normalized = str(value or "").strip().lower().replace("-", "")
        if not normalized:
            return None
        allowed = {"auto", "default", "flex", "priority", "fast", "ultrafast"}
        if normalized in allowed:
            return normalized
        choices = ", ".join(sorted(allowed))
        raise ValueError(
            f"CODEX_APP_SERVER_SERVICE_TIER must be one of: {choices}"
        )


__all__ = [
    "CodexAppServerAdapter",
    "CodexAppServerStartupUnavailable",
]
