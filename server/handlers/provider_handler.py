from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_host.provider_bootstrap import builtin_provider_specs
from agent_host.mcp_connections import McpConnectionSpec, load_mcp_connections
from agent_host.provider_contract import ProviderRequirements
from agent_host.provider_runtime import runtime
from agent_host.provider_types import ProviderRunRequest, ProviderSteerRequest
from server.local_git import collect_diff, run_git
from server.protocol import Method
from server.ws_handler import RequestHandler

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from server.capability_catalog import CapabilityCatalog


class ProviderHandler(RequestHandler):
    methods = [
        Method.PROVIDER_RUN,
        Method.PROVIDER_CANCEL,
        Method.PROVIDER_LIST,
        Method.PROVIDER_DIFF,
        Method.PROVIDER_STATUS,
    ]

    def __init__(
        self,
        *,
        capability_catalog: "CapabilityCatalog | None" = None,
    ) -> None:
        self._registered = False
        self._work_control = None
        self._capability_catalog = capability_catalog
        self._host_adapters: dict[str, Any] = {}
        self._provider_availability: dict[str, dict[str, Any]] = {}
        self._mcp_connections: tuple[McpConnectionSpec, ...] = load_mcp_connections()
        self._ensure_registered()

    def configure_work_control(self, coordinator) -> None:
        self._work_control = coordinator

    def _ensure_registered(self) -> None:
        if self._registered:
            return
        for spec in builtin_provider_specs():
            availability: dict[str, Any] = {
                "provider_id": spec.provider_id,
                "configured": bool(spec.runtime_enabled),
                "ready": False,
                "registered": False,
                "reason": "disabled" if not spec.runtime_enabled else "initializing",
            }
            self._provider_availability[spec.provider_id] = availability
            if not spec.runtime_enabled and not spec.instantiate_when_disabled:
                continue
            try:
                adapter = spec.factory()
            except Exception as exc:
                detail = getattr(exc, "availability", None)
                if isinstance(detail, dict):
                    availability.update(detail)
                    availability["configured"] = bool(spec.runtime_enabled)
                    availability["registered"] = False
                else:
                    availability.update(
                        {
                            "reason": "adapter_initialization_failed",
                            # Generic adapter exceptions are not required to
                            # implement the Direct Codex redaction contract.
                            # Keep the public startup surface diagnostic-free
                            # rather than copying arbitrary transport output.
                            "diagnostic": "",
                        }
                    )
                if spec.required:
                    raise
                logger.warning(
                    "%s provider adapter unavailable: %s",
                    spec.provider_id,
                    availability.get("reason") or "initialization_failed",
                )
                continue
            self._host_adapters[spec.provider_id] = adapter
            startup = getattr(adapter, "_startup_readiness", None)
            if isinstance(startup, dict):
                availability.update(startup)
                availability["configured"] = bool(spec.runtime_enabled)
            if not spec.runtime_enabled:
                availability.update(
                    {
                        "ready": False,
                        "registered": False,
                        "reason": "disabled",
                    }
                )
                continue
            runtime.register(adapter)
            availability.update(
                {
                    "ready": True,
                    "registered": True,
                    "reason": "ready",
                }
            )
        if self._capability_catalog is not None:
            from server.capability_composition import (
                sync_mcp_connection_capabilities,
                sync_provider_capabilities,
            )

            sync_provider_capabilities(
                self._capability_catalog,
                runtime.provider_manifests(),
            )
            sync_mcp_connection_capabilities(
                self._capability_catalog,
                self._mcp_connections,
            )
        self._registered = True

    @property
    def mcp_connections(self) -> tuple[McpConnectionSpec, ...]:
        return self._mcp_connections

    def provider_availability(self) -> list[dict[str, Any]]:
        return [
            dict(self._provider_availability[key])
            for key in sorted(self._provider_availability)
        ]

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.PROVIDER_RUN:
            # Generic provider.run is a new-run API. Resume is deliberately
            # available only to the Work Ledger's bounded work.resume path.
            return await self._run(params, allow_resume=False)
        if method == Method.PROVIDER_CANCEL:
            return await self._cancel(params)
        if method == Method.PROVIDER_LIST:
            return await self._list(params)
        if method == Method.PROVIDER_DIFF:
            return await self._diff(params)
        if method == Method.PROVIDER_STATUS:
            return await self._status(params)
        return None

    async def _run(
        self,
        params: dict[str, Any],
        *,
        allow_resume: bool,
    ) -> dict[str, Any]:
        provider = str(params.get("provider") or "").strip()
        task = str(params.get("task") or "").strip()
        if not provider:
            raise ValueError("provider is required")
        if not task:
            raise ValueError("task is required")

        metadata_raw = params.get("metadata")
        metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        requested_work_item_id = str(
            params.get("work_item_id") or params.get("workItemId") or ""
        ).strip()
        if requested_work_item_id:
            work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
            metadata["work"] = {**work, "work_item_id": requested_work_item_id}
        if not metadata.get("session_id"):
            try:
                from core import session_manager as sm
                session_id = sm.get_current_session_id()
                if session_id:
                    metadata = {**metadata, "session_id": session_id}
            except Exception:
                pass
        cwd = params.get("cwd")
        mode = str(params.get("mode") or "agent")
        ownership = str(
            params.get("ownership") or metadata.get("provider_ownership") or "managed"
        ).strip().lower()
        if ownership not in {"managed", "attached"}:
            raise ValueError(f"invalid provider ownership: {ownership}")
        requirements_raw = (
            params.get("requirements")
            if isinstance(params.get("requirements"), dict)
            else metadata.get("provider_requirements")
            if isinstance(metadata.get("provider_requirements"), dict)
            else {}
        )
        requirements_payload = dict(requirements_raw)
        requirements_payload.setdefault("ownership", ownership)
        request = ProviderRunRequest(
            provider=provider,
            task=task,
            cwd=str(cwd) if cwd else None,
            mode=mode,
            metadata=dict(metadata),
            requirements=ProviderRequirements.from_dict(requirements_payload),
            ownership=ownership,
        )
        resume_run_id = str(params.get("resume") or "").strip()
        if resume_run_id:
            if not allow_resume:
                raise ValueError("provider.run cannot Resume; use work.resume")
            request.metadata["resume"] = True
            record = await runtime.resume(resume_run_id, request)
            return self._run_response(record)
        record = await runtime.start(request)
        return self._run_response(record)

    @staticmethod
    def _run_response(record) -> dict[str, Any]:
        metadata = record.metadata if isinstance(record.metadata, dict) else {}
        work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
        return {
            "run": record.to_dict(),
            "providers": runtime.list_providers(),
            "provider_manifests": runtime.list_provider_manifests(),
            "work": dict(work),
        }

    async def run_provider(self, params: dict[str, Any]) -> dict[str, Any]:
        """Internal provider entrypoint used by host-owned control planes."""

        return await self._run(params, allow_resume=True)

    async def steer_provider(self, params: dict[str, Any]) -> dict[str, Any]:
        """Internal immediate-steer entrypoint for host-owned coordinators."""

        run_id = str(params.get("run_id") or "").strip()
        task = str(params.get("task") or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        if not task:
            raise ValueError("task is required")
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        return await runtime.steer(
            run_id,
            ProviderSteerRequest(
                task=task,
                revision=max(1, int(params.get("revision") or 1)),
                metadata=dict(metadata),
            ),
        )

    async def _cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        return await runtime.cancel(run_id)

    async def _list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "providers": runtime.list_providers(),
            "provider_manifests": runtime.list_provider_manifests(),
            "provider_availability": self.provider_availability(),
            "runs": runtime.list_runs(),
        }

    def _cwd_from_params_or_run(self, params: dict[str, Any]) -> str | None:
        cwd = params.get("cwd")
        if cwd:
            return str(cwd)
        run_id = str(params.get("run_id") or "").strip()
        if not run_id:
            return None
        run = runtime.get_run(run_id)
        return run.cwd if run else None

    async def _diff(self, params: dict[str, Any]) -> dict[str, Any]:
        work_control = getattr(self, "_work_control", None)
        run_id = str(params.get("run_id") or "").strip()
        attempt_id = str(params.get("attempt_id") or "").strip()
        attempt = None
        if work_control is not None:
            if attempt_id:
                attempt = work_control.store.get_attempt(attempt_id)
            elif run_id:
                attempt = work_control.store.get_attempt_by_provider_run(run_id)
            if attempt is not None:
                delta = work_control.artifact_registry.delta_for_attempt(attempt.attempt_id)
                if delta is not None:
                    return {
                        "diff": {
                            "success": bool(delta.get("available")),
                            "source": "work_ledger",
                            "work_item_id": attempt.work_item_id,
                            "attempt_id": attempt.attempt_id,
                            "cwd": str(delta.get("repo_root") or ""),
                            "patch": str(delta.get("patch") or ""),
                            "stderr": "",
                            "returncode": 0 if delta.get("available") else 2,
                            "untracked": list(delta.get("untracked") or []),
                            "changed_files": list(delta.get("changed_files") or []),
                            "head": bool(delta.get("current_head")),
                            "baseline_head": str(delta.get("baseline_head") or ""),
                            "current_head": str(delta.get("current_head") or ""),
                            "ambiguous_paths": list(delta.get("ambiguous_paths") or []),
                            "reason": str(delta.get("reason") or ""),
                        }
                    }
                if attempt.execution_status not in {"queued", "running"}:
                    return {
                        "diff": {
                            "success": False,
                            "source": "work_ledger",
                            "work_item_id": attempt.work_item_id,
                            "attempt_id": attempt.attempt_id,
                            "patch": "",
                            "changed_files": [],
                            "untracked": [],
                            "reason": "attempt_diff_unavailable",
                            "returncode": 2,
                            "stderr": "This attempt has no persistent Git baseline.",
                            "head": False,
                        }
                    }
        cwd = self._cwd_from_params_or_run(params)
        if not cwd:
            raise ValueError("cwd or run_id is required")
        result = await collect_diff(cwd)
        return {
            "diff": {
                "success": result["success"],
                "cwd": result["cwd"],
                "patch": result["patch"],
                "stderr": result["stderr"],
                "returncode": result["returncode"],
                "untracked": result["untracked"],
                "changed_files": result["changed_files"],
                "head": result["head"],
            }
        }

    async def _status(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = self._cwd_from_params_or_run(params)
        if not cwd:
            raise ValueError("cwd or run_id is required")
        result = await run_git(cwd, ["status", "--porcelain=v1", "--branch"])
        return {
            "status": {
                "success": result["returncode"] == 0,
                "cwd": str(Path(cwd).resolve()),
                "porcelain": result["stdout"].splitlines(),
                "raw": result["stdout"],
                "stderr": result["stderr"],
                "returncode": result["returncode"],
            }
        }
