"""ACP v1 translation into the existing Provider contract.

One SDK-managed process owns one run. Persistent native sessions may be
reattached explicitly; no process, transcript, or Agent becomes a Work owner.
The SDK owns framing and subprocess disposal. Cancellation is confirmed only
by the native prompt result, never by a successful write or process exit.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_host.acp_configuration import AcpAgentSpec
from agent_host.mcp_connections import (
    McpConnectionSpec,
    connections_for_provider,
    load_mcp_connections,
)
from agent_host.provider_authoring import (
    required_auip_engagement_mode,
    requires_auip_authoring,
    with_host_authoring_capabilities,
)
from agent_host.provider_contract import ProviderCapabilities, ProviderManifest
from agent_host.provider_identity import with_parent_conversation_context
from agent_host.provider_progress import with_progress_contract, split_progress_stream
from agent_host.provider_types import (
    COOPERATIVE_CONTEXT_ACCEPTED_METADATA_KEY,
    EmitProviderEvent,
    ProviderActivityEvidence,
    ProviderEvent,
    ProviderPermissionResponse,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderSessionHandle,
)


class AcpConfigurationError(ValueError):
    """Safe local diagnostics, containing no native error or secret payload."""


class AcpStartupUnavailable(RuntimeError):
    def __init__(self, provider: str, reason: str, diagnostic: str) -> None:
        self.availability = {
            "provider_id": provider,
            "ready": False,
            "registered": False,
            "reason": reason,
            "diagnostic": diagnostic,
        }
        super().__init__(diagnostic)


def _dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    return dump(mode="json", by_alias=True, exclude_none=True) if callable(dump) else {}


@dataclass
class _Run:
    run_id: str
    request: ProviderRunRequest
    emit: EmitProviderEvent
    done: asyncio.Event = field(default_factory=asyncio.Event)
    update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    session: ProviderSessionHandle | None = None
    connection: Any = None
    prompt_task: asyncio.Task | None = None
    cancel_task: asyncio.Task | None = None
    cancel_requested: bool = False
    submitted: bool = False
    terminal: bool = False
    stop_reason: str = ""
    text: str = ""
    progress_pending: str = ""
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    permission_futures: dict[str, asyncio.Future] = field(default_factory=dict)
    update_failed: bool = False
    progress_milestones: int = 0
    received_updates: int = 0
    handled_updates: int = 0


class _Client:
    def __init__(self, adapter: AcpProviderAdapter, run: _Run) -> None:
        self.adapter, self.run = adapter, run

    def observe(self, event: Any) -> None:
        # The SDK reports invalid notification schemas to its supervisor rather
        # than prompt(). Count received/handled updates to detect an incomplete
        # stream without duplicating its framing or schema validation.
        message = event.message
        params = message.get("params") or {}
        if (
            event.direction.value == "incoming"
            and message.get("method") == "session/update"
            and self.run.submitted
            and self.run.session
            and params.get("sessionId") == self.run.session.session_id
        ):
            self.run.received_updates += 1

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        run = self.run
        # Ignore replay during session setup and unrelated native sessions.
        if not run.submitted or run.session is None or session_id != run.session.session_id:
            return
        async with run.update_lock:
            try:
                await self.adapter._update(run, _dict(update))
            except Exception:
                # SDK notification handlers cannot propagate to prompt().
                # Retain the failure so a lost Host projection cannot succeed.
                run.update_failed = True
            finally:
                run.handled_updates += 1

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.adapter._permission(self.run, session_id, _dict(tool_call), options)


class AcpProviderAdapter:
    def __init__(
        self,
        spec: AcpAgentSpec,
        *,
        mcp_connections: tuple[McpConnectionSpec, ...] | None = None,
        setup_timeout: float = 30,
        cancel_timeout: float = 10,
        run_timeout: float = 3600,
        permission_timeout: float = 300,
    ) -> None:
        self.spec = spec
        self.provider_id = spec.provider_id
        self.manifest = ProviderManifest(
            provider_id=self.provider_id,
            display_name=spec.name,
            runtime_kind="agent",
            selection_priority=0,
            capabilities=ProviderCapabilities(
                task_kinds=("general", "workspace_read", "workspace_mutation"),
                workspace_access="write",
                workspace_ownership="caller",
                durability="host_restart" if spec.resume else "turn",
                resume="attach" if spec.resume else "none",
                cancellation="confirmed",
                interaction="bidirectional",
                capability_projections=("mcp_connection",),
            ),
        )
        self._connections = load_mcp_connections() if mcp_connections is None else mcp_connections
        self.setup_timeout, self.cancel_timeout = setup_timeout, cancel_timeout
        self.run_timeout, self.permission_timeout = run_timeout, permission_timeout
        self._runs: dict[str, _Run] = {}
        self._attached: set[str] = set()
        self._configuration: dict[str, Any] = {}
        self._startup_readiness: dict[str, Any] = {}

    def require_startup_ready(self) -> None:
        try:
            import acp  # Optional extra: absence disables this Provider, not the Host.
        except ImportError:
            raise AcpStartupUnavailable(
                self.provider_id,
                "acp_sdk_unavailable",
                'Install the optional Python extra: pip install -e ".[acp]"',
            ) from None
        try:
            self.spec.executable()
        except ValueError as exc:
            raise AcpStartupUnavailable(
                self.provider_id, "acp_executable_unavailable", str(exc)
            ) from None
        try:
            self.spec.process_environment()
        except ValueError as exc:
            raise AcpStartupUnavailable(
                self.provider_id, "acp_environment_unavailable", str(exc)
            ) from None
        if acp.PROTOCOL_VERSION != 1:
            raise RuntimeError("This adapter requires ACP v1")
        self._startup_readiness = {
            "authentication": "unknown",
            "protocol_version": 1,
            "diagnostic": "Local transport available; agent capabilities and authentication checked on connection.",
        }

    def configuration(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, **self._configuration}

    async def close(self) -> None:
        runs = tuple(self._runs.values())
        for run in runs:
            run.cancel_requested = True
            self._release_permissions(run)
        # Shutdown releases native resources. It does not fabricate a confirmed
        # user cancellation: an unfinished prompt becomes an unknown outcome.
        await asyncio.gather(
            *(run.connection.close() for run in runs if run.connection), return_exceptions=True
        )
        for run in runs:
            await asyncio.wait_for(run.done.wait(), self.setup_timeout + self.cancel_timeout + 5)

    async def run(
        self, request: ProviderRunRequest, run_id: str, emit: EmitProviderEvent
    ) -> ProviderRunResult:
        import acp

        if (
            request.provider != self.provider_id
            or not request.cwd
            or not Path(request.cwd).is_absolute()
        ):
            return ProviderRunResult(
                status="error", error="ACP requires a matching Provider and Host-owned absolute cwd"
            )
        cwd = Path(request.cwd).resolve()
        if not cwd.is_dir():
            return ProviderRunResult(status="error", error="ACP workspace is unavailable")
        attached = request.session
        if attached and (attached.provider != self.provider_id or not self.spec.resume):
            return ProviderRunResult(
                status="error", error="ACP session attachment is not supported by this Provider"
            )
        if run_id in self._runs or (attached and attached.session_id in self._attached):
            return ProviderRunResult(
                status="error", error="ACP execution/session already has an active owner"
            )
        run = _Run(run_id, request, emit)
        self._runs[run_id] = run
        if attached:
            self._attached.add(attached.session_id)
        result = ProviderRunResult(status="error", error="ACP setup did not complete")
        capabilities: dict[str, Any] = {}
        try:
            client = _Client(self, run)
            async with acp.spawn_agent_process(
                client,
                self.spec.executable(),
                *self.spec.args,
                env=self.spec.process_environment(),
                cwd=str(cwd),
                transport_kwargs={"stderr": asyncio.subprocess.DEVNULL, "limit": 8 * 1024 * 1024},
                observers=[client.observe],
            ) as (connection, _process):
                run.connection = connection
                try:
                    async with asyncio.timeout(self.setup_timeout):
                        initialized = _dict(
                            await connection.initialize(
                                protocol_version=1,
                                client_info=acp.schema.Implementation(
                                    name="amadeus", version="0.1.0"
                                ),
                                client_capabilities=acp.schema.ClientCapabilities(),
                            )
                        )
                        if initialized.get("protocolVersion") != 1:
                            raise AcpConfigurationError("ACP agent did not negotiate v1")
                        capabilities = initialized.get("agentCapabilities", {})
                        if self.spec.resume and not (
                            isinstance(
                                capabilities.get("sessionCapabilities", {}).get("resume"), dict
                            )
                            or capabilities.get("loadSession") is True
                        ):
                            raise AcpConfigurationError(
                                "Persistent sessions enabled but native resume is not advertised"
                            )
                        mcp = self._mcp_servers(cwd, capabilities)
                        if run.cancel_requested:
                            return ProviderRunResult(status="cancelled")
                        opened: (
                            acp.schema.NewSessionResponse
                            | acp.schema.ResumeSessionResponse
                            | acp.schema.LoadSessionResponse
                        )
                        if attached:
                            sessions = capabilities.get("sessionCapabilities", {})
                            if isinstance(sessions.get("resume"), dict):
                                opened = await connection.resume_session(
                                    session_id=attached.session_id,
                                    cwd=str(cwd),
                                    mcp_servers=mcp,
                                )
                            elif capabilities.get("loadSession") is True:
                                opened = await connection.load_session(
                                    session_id=attached.session_id,
                                    cwd=str(cwd),
                                    mcp_servers=mcp,
                                )
                            else:
                                raise AcpConfigurationError(
                                    "ACP agent did not advertise native resume"
                                )
                            session_id = attached.session_id
                        else:
                            opened = await connection.new_session(cwd=str(cwd), mcp_servers=mcp)
                            session_id = _dict(opened).get("sessionId", "")
                            if session_id in self._attached:
                                raise AcpConfigurationError(
                                    "ACP session already has an active owner"
                                )
                        scope = (
                            attached.scope
                            if attached
                            else (
                                "interaction"
                                if request.metadata.get(COOPERATIVE_CONTEXT_ACCEPTED_METADATA_KEY)
                                else "work_item"
                            )
                        )
                        run.session = ProviderSessionHandle(self.provider_id, session_id, scope)
                        self._attached.add(session_id)
                        # Bind the native address durably before any user execution.
                        await emit(
                            ProviderEvent(
                                provider=self.provider_id,
                                run_id=run_id,
                                type="session.opened",
                                session=run.session,
                            )
                        )
                        options = _dict(opened).get("configOptions", [])
                        self._configuration = {
                            "agent_info": initialized.get("agentInfo", {}),
                            "config_options": options,
                            "protocol_version": 1,
                        }
                        for config_id, value in self.spec.config_options.items():
                            self._validate_option(options, config_id, value)
                            configured = await connection.set_config_option(
                                session_id=session_id,
                                config_id=config_id,
                                value=value,
                            )
                            options = _dict(configured).get("configOptions", [])
                            self._configuration["config_options"] = options
                    if run.cancel_requested:
                        result = ProviderRunResult(status="cancelled", session=run.session)
                    else:
                        # No await between the cancellation gate and prompt ownership.
                        run.submitted = True
                        run.prompt_task = asyncio.create_task(
                            connection.prompt(
                                session_id=session_id,
                                prompt=[acp.text_block(self._task_text(request))],
                            )
                        )
                        try:
                            reply = await asyncio.wait_for(
                                asyncio.shield(run.prompt_task), self.run_timeout
                            )
                            run.stop_reason = _dict(reply).get("stopReason", "")
                            run.terminal = True
                            await self._flush_progress(run)
                            result = self._result(run)
                        except TimeoutError:
                            # A timed-out prompt has unknown effects. Try the native
                            # cancellation once; never resubmit the original input.
                            run.cancel_requested = True
                            self._release_permissions(run)
                            await connection.cancel(session_id=session_id)
                            try:
                                reply = await asyncio.wait_for(
                                    asyncio.shield(run.prompt_task), self.cancel_timeout
                                )
                                run.stop_reason = _dict(reply).get("stopReason", "")
                                run.terminal = True
                                await self._flush_progress(run)
                                result = self._result(run)
                            except (Exception, asyncio.CancelledError):
                                result = self._unknown(
                                    run, "ACP deadline elapsed without a terminal acknowledgement"
                                )
                finally:
                    self._release_permissions(run)
                    if run.prompt_task and not run.prompt_task.done():
                        run.prompt_task.cancel()
                        await asyncio.gather(run.prompt_task, return_exceptions=True)
                    if run.session and isinstance(
                        capabilities.get("sessionCapabilities", {}).get("close"), dict
                    ):
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                connection.close_session(session_id=run.session.session_id),
                                self.cancel_timeout,
                            )
        except asyncio.CancelledError:
            # Runtime owns forced producer cancellation; the SDK context still
            # closes the connection/process while unwinding.
            raise
        except Exception as exc:
            # Transport error text may contain authentication payloads. Expose
            # the class/code only, keeping credentials out of Ledger and logs.
            detail = (
                str(exc)
                if isinstance(exc, AcpConfigurationError)
                else (
                    "Authentication required; configure the agent login or credential reference"
                    if isinstance(exc, acp.RequestError) and exc.code == -32000
                    else f"{type(exc).__name__}"
                    + (f" ({exc.code})" if isinstance(exc, acp.RequestError) else "")
                )
            )
            if isinstance(exc, acp.RequestError) and exc.code == -32000:
                # ACP's typed auth_required response is an explicit admission
                # rejection. Unlike EOF, it does not leave a possibly live run.
                run.terminal = True
                run.stop_reason = "auth_required"
                result = ProviderRunResult(
                    status="error",
                    error=detail,
                    session=run.session,
                    metadata={"result_type": "authentication_required"},
                )
            else:
                result = (
                    self._unknown(run, f"ACP execution interrupted: {detail}")
                    if run.submitted
                    else ProviderRunResult(
                        status="error",
                        error=f"ACP setup failed: {detail}",
                        session=run.session,
                    )
                )
        finally:
            self._release_permissions(run)
            self._runs.pop(run_id, None)
            if attached:
                self._attached.discard(attached.session_id)
            elif run.session:
                self._attached.discard(run.session.session_id)
            run.done.set()
        return result

    async def cancel(self, run_id: str) -> dict[str, Any]:
        run = self._runs.get(run_id)
        if run is None:
            return {"confirmed": False, "cancelled": False, "reason": "not_active"}
        if run.cancel_task is None:
            run.cancel_requested = True
            self._release_permissions(run)
            run.cancel_task = asyncio.create_task(self._cancel(run))
        return await asyncio.shield(run.cancel_task)

    async def _cancel(self, run: _Run) -> dict[str, Any]:
        try:
            if run.submitted and not run.terminal and run.connection and run.session:
                await asyncio.wait_for(
                    run.connection.cancel(session_id=run.session.session_id), self.cancel_timeout
                )
            await asyncio.wait_for(run.done.wait(), self.setup_timeout + self.cancel_timeout)
        except Exception:
            return {
                "confirmed": False,
                "cancelled": False,
                "reason": "native_cancellation_unconfirmed",
            }
        cancelled = not run.submitted or (run.terminal and run.stop_reason == "cancelled")
        return {
            "confirmed": cancelled or run.terminal,
            "cancelled": cancelled,
            "reason": "cancelled"
            if cancelled
            else "native_turn_finished"
            if run.terminal
            else "native_outcome_unknown",
            **({"session": run.session} if run.session and self.spec.resume else {}),
        }

    async def resolve_permission(
        self, run_id: str, response: ProviderPermissionResponse
    ) -> dict[str, Any]:
        run = self._runs.get(run_id)
        future = run.permission_futures.get(response.request_id) if run else None
        if run is None or run.cancel_requested or run.terminal or future is None or future.done():
            return {"accepted": False, "reason": "permission_request_not_pending"}
        future.set_result(response.allow)
        return {"accepted": True}

    @staticmethod
    def _release_permissions(run: _Run) -> None:
        for future in run.permission_futures.values():
            if not future.done():
                future.set_result(None)

    async def _permission(self, run: _Run, session_id: str, call: dict, options: list) -> dict:
        cancelled = {"outcome": {"outcome": "cancelled"}}
        if (
            run.cancel_requested
            or run.terminal
            or not run.submitted
            or run.session is None
            or run.session.session_id != session_id
        ):
            return cancelled
        choices = [_dict(option) for option in options]
        allow = [item for item in choices if item.get("kind") == "allow_once"]
        deny = [item for item in choices if item.get("kind") == "reject_once"]
        # A Host one-shot approval must never select a durable allow option.
        if len(allow) != 1 or not allow[0].get("optionId"):
            return cancelled
        request_id = f"acp-permission-{uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        run.permission_futures[request_id] = future
        announced = False
        decision = None
        kind = call.get("kind", "other")
        capability = (
            "shell.execute"
            if kind == "execute"
            else "filesystem.write"
            if kind in {"edit", "delete", "move"}
            else "provider.tool.execute"
        )
        locations = [
            str(item["path"])[:2048]
            for item in call.get("locations", [])
            if isinstance(item, dict) and item.get("path")
        ]
        title = str(call.get("title") or "Agent requests tool approval")
        raw_input = call.get("rawInput")
        if isinstance(raw_input, dict) and isinstance(raw_input.get("command"), str):
            title += "\n" + raw_input["command"]
        try:
            await run.emit(
                ProviderEvent(
                    provider=self.provider_id,
                    run_id=run.run_id,
                    type="permission.requested",
                    payload={
                        "permissionRequest": {
                            "request_id": request_id,
                            "capability": capability,
                            "action": "execute_tool",
                            "tool": str(call.get("kind") or "tool")[:100],
                            "scope": locations[:16],
                            "reason": title[:1000],
                            "reversibility": "unknown",
                            "options": ["allow_once", "deny"],
                            "retryRequired": False,
                            "diagnosticOnly": False,
                        }
                    },
                )
            )
            announced = True
            decision = await asyncio.wait_for(asyncio.shield(future), self.permission_timeout)
            if decision is None or run.cancel_requested:
                return cancelled
            chosen = allow[0] if decision else deny[0] if len(deny) == 1 else None
            return (
                {"outcome": {"outcome": "selected", "optionId": chosen["optionId"]}}
                if chosen
                else cancelled
            )
        except TimeoutError:
            return cancelled
        finally:
            run.permission_futures.pop(request_id, None)
            if announced and (decision is None or run.cancel_requested):
                await run.emit(
                    ProviderEvent(
                        provider=self.provider_id,
                        run_id=run.run_id,
                        type="permission.expired",
                        payload={
                            "request_id": request_id,
                            "reason": "ACP permission no longer pending",
                        },
                    )
                )

    async def _update(self, run: _Run, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                visible, milestones, run.progress_pending = split_progress_stream(
                    run.progress_pending, content.get("text", "")
                )
                run.text += visible
                if visible:
                    await self._emit(run, "assistant.delta", {"text": visible})
                for milestone in milestones:
                    run.progress_milestones += 1
                    await self._emit(run, "semantic.progress", milestone)
        elif kind in {"tool_call", "tool_call_update"}:
            await self._flush_progress(run)
            key = update.get("toolCallId", "")
            if not key:
                raise ValueError("Missing ACP tool identity")
            previous = run.tools.get(key, {})
            tool = {**previous, **update}
            run.tools[key] = tool
            status = tool.get("status")
            payload = {
                "item_id": key,
                "tool": tool.get("kind", "other"),
                "name": tool.get("title", "Agent tool"),
                "title": tool.get("title", "Agent tool"),
                "input": tool.get("rawInput", {}),
                "status": status,
            }
            if not previous:
                await self._emit(run, "tool.call", payload)
            if status in {"completed", "failed"} and previous.get("status") not in {
                "completed",
                "failed",
            }:
                payload.update(
                    {
                        "output": tool.get("rawOutput", tool.get("content", [])),
                        "success": status == "completed",
                    }
                )
                await self._emit(run, "tool.result", payload)
        elif kind == "config_option_update":
            self._configuration["config_options"] = update.get("configOptions", [])
        elif kind == "plan":
            await self._emit(
                run,
                "assistant.update",
                {
                    "text": "; ".join(
                        str(item.get("content", "")) for item in update.get("entries", [])
                    )[:2400],
                    "source": "acp_plan",
                    "explicit": False,
                    "status": "reported_direction",
                },
            )

    async def _emit(self, run: _Run, kind: str, payload: dict) -> None:
        await run.emit(
            ProviderEvent(provider=self.provider_id, run_id=run.run_id, type=kind, payload=payload)
        )

    async def _flush_progress(self, run: _Run) -> None:
        visible, milestones, run.progress_pending = split_progress_stream(
            run.progress_pending, "", final=True
        )
        run.text += visible
        if visible:
            await self._emit(run, "assistant.delta", {"text": visible})
        for milestone in milestones:
            run.progress_milestones += 1
            await self._emit(run, "semantic.progress", milestone)

    def _result(self, run: _Run) -> ProviderRunResult:
        if run.update_failed or run.received_updates != run.handled_updates:
            return self._unknown(run, "ACP event projection failed")
        status = (
            "done"
            if run.stop_reason == "end_turn"
            else "cancelled"
            if run.stop_reason == "cancelled"
            else "error"
        )
        return ProviderRunResult(
            status=status,
            result=run.text.strip(),
            error=None if status != "error" else f"ACP stopped: {run.stop_reason or 'unknown'}",
            session=run.session if self.spec.resume else None,
            metadata={"acp": {"stop_reason": run.stop_reason, "protocol_version": 1}},
            activity_evidence=ProviderActivityEvidence(
                terminal_observed=True,
                execution_items=len(run.tools),
                progress_milestones=run.progress_milestones,
            ),
        )

    @staticmethod
    def _unknown(run: _Run, reason: str) -> ProviderRunResult:
        return ProviderRunResult(
            status="orphaned",
            result=run.text.strip(),
            error=reason,
            session=run.session,
            metadata={
                "result_type": "transport_outcome_unknown",
                "runtime_resumable": False,
                "outcome_uncertainty": "native_execution_not_observed_to_terminal",
            },
        )

    @staticmethod
    def _validate_option(options: list[dict], config_id: str, value: str) -> None:
        option = next((item for item in options if item.get("id") == config_id), None)
        if option is None or option.get("type") != "select":
            raise AcpConfigurationError("ACP configuration option is not advertised")
        values: list[str] = []
        for item in option.get("options", []):
            values.extend(
                child.get("value") for child in item["options"]
            ) if "options" in item else values.append(item.get("value"))
        if value not in values:
            raise AcpConfigurationError("ACP configuration value is not advertised")

    def _mcp_servers(self, cwd: Path, capabilities: dict) -> list:
        from acp.schema import EnvVariable, HttpHeader, HttpMcpServer, McpServerStdio
        import os

        result = []
        for connection in connections_for_provider(self._connections, self.provider_id):
            if connection.transport == "stdio":
                if connection.cwd and Path(connection.cwd).resolve() != cwd:
                    raise AcpConfigurationError(
                        "ACP v1 cannot project a different MCP working directory"
                    )
                result.append(
                    McpServerStdio(
                        name=connection.connection_id,
                        command=connection.command,
                        args=list(connection.arguments),
                        env=[
                            EnvVariable(name=k, value=v) for k, v in connection.environment.items()
                        ],
                    )
                )
            else:
                if capabilities.get("mcpCapabilities", {}).get("http") is not True:
                    raise AcpConfigurationError(
                        "ACP agent does not support the selected HTTP MCP connection"
                    )
                headers = []
                if connection.bearer_token_env_var:
                    token = connection.environment.get(
                        connection.bearer_token_env_var
                    ) or os.environ.get(connection.bearer_token_env_var)
                    if not token:
                        raise AcpConfigurationError("MCP authentication is unavailable")
                    headers.append(HttpHeader(name="Authorization", value=f"Bearer {token}"))
                result.append(
                    HttpMcpServer(
                        type="http",
                        name=connection.connection_id,
                        url=connection.url,
                        headers=headers,
                    )
                )
        return result

    def _task_text(self, request: ProviderRunRequest) -> str:
        metadata = request.metadata
        return with_progress_contract(
            with_host_authoring_capabilities(
                with_parent_conversation_context(
                    request.task, metadata=metadata, execution_provider=self.provider_id
                ),
                require_auip_preparation=requires_auip_authoring(metadata),
                authoring_skill_path=str(metadata.get("auip_authoring_skill_path") or ""),
                required_auip_mode=required_auip_engagement_mode(metadata),
            ),
            presentation_locale=metadata.get("presentation_locale"),
        )
