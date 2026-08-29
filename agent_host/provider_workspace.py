"""Provider-neutral workspace binding and host-side validation.

This boundary deliberately does not allocate worktrees or inspect a Provider
sandbox.  The Amadeus control plane first resolves/materializes a cwd;
this module then records who owns that workspace and verifies the minimum host
facts every adapter is allowed to rely on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_host.provider_contract import (
    ProviderManifest,
    WorkspaceAccess,
    WorkspaceOwnership,
)
from agent_host.provider_types import ProviderRunRequest


WorkspaceBindingStatus = Literal[
    "not_required",
    "ready",
    "provider_pending",
    "forced_unsupported",
]
WorkspaceRouteAuthority = Literal["not_applicable", "host", "provider"]


class WorkspaceContractError(ValueError):
    """The prepared workspace does not satisfy the selected Provider contract."""


def workspace_route_authority(
    ownership: WorkspaceOwnership,
) -> WorkspaceRouteAuthority:
    """Return who must turn a logical destination into a workspace.

    Caller-owned and negotiated workspaces both cross the Amadeus control
    plane before an adapter starts. Provider-owned workspaces may be allocated
    after start; providers with no workspace ownership do not participate in
    Project/Scratch routing at all.
    """

    if ownership in {"caller", "negotiated"}:
        return "host"
    if ownership == "provider":
        return "provider"
    return "not_applicable"


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    cwd: str
    access: WorkspaceAccess
    ownership: WorkspaceOwnership
    status: WorkspaceBindingStatus
    source: str
    host_readable: bool
    host_writable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "access": self.access,
            "ownership": self.ownership,
            "status": self.status,
            "source": self.source,
            "host_readable": self.host_readable,
            "host_writable": self.host_writable,
        }


def prepare_workspace_binding(
    request: ProviderRunRequest,
    manifest: ProviderManifest,
) -> WorkspaceBinding:
    """Canonicalize the control-plane result before an adapter can run.

    ``provider`` ownership may legitimately start without a cwd because the
    provider allocates it later. ``caller`` and ``negotiated`` ownership must
    arrive at the adapter with a real directory whenever workspace access was
    requested. Host readability/writability is a minimum check only; nested
    provider sandboxes may impose stricter rules and remain adapter evidence.
    """

    requirements = request.requirements
    access: WorkspaceAccess = (
        requirements.workspace_access if requirements is not None else "none"
    )
    ownership = manifest.capabilities.workspace_ownership
    source = str(
        request.metadata.get("workspace_routing_source")
        or request.metadata.get("source")
        or "provider_request"
    ).strip()
    raw_cwd = str(request.cwd or "").strip()
    if not raw_cwd:
        if access == "none":
            return WorkspaceBinding(
                cwd="",
                access=access,
                ownership=ownership,
                status="not_required",
                source=source,
                host_readable=False,
                host_writable=False,
            )
        if (
            requirements is not None
            and requirements.preference_policy == "force"
            and ownership == "none"
        ):
            return WorkspaceBinding(
                cwd="",
                access=access,
                ownership=ownership,
                status="forced_unsupported",
                source=source,
                host_readable=False,
                host_writable=False,
            )
        if ownership == "provider":
            return WorkspaceBinding(
                cwd="",
                access=access,
                ownership=ownership,
                status="provider_pending",
                source=source,
                host_readable=False,
                host_writable=False,
            )
        raise WorkspaceContractError(
            f"provider {manifest.provider_id} requires a prepared workspace "
            f"for {access} access ({ownership} ownership)"
        )

    try:
        cwd = Path(raw_cwd).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceContractError(f"invalid provider workspace: {raw_cwd!r}") from exc
    if not cwd.is_dir():
        raise WorkspaceContractError(f"provider workspace is not a directory: {cwd}")

    readable = os.access(cwd, os.R_OK)
    writable = os.access(cwd, os.W_OK)
    binding = WorkspaceBinding(
        cwd=str(cwd),
        access=access,
        ownership=ownership,
        status="ready",
        source=source,
        host_readable=readable,
        host_writable=writable,
    )
    validate_workspace_binding(binding, provider_id=manifest.provider_id)
    request.cwd = binding.cwd
    return binding


def validate_workspace_binding(
    binding: WorkspaceBinding,
    *,
    provider_id: str,
) -> None:
    """Validate the host-visible portion of a prepared workspace receipt."""

    if binding.status != "ready":
        return
    if binding.access in {"read", "write"} and not binding.host_readable:
        raise WorkspaceContractError(
            f"provider {provider_id} workspace is not host-readable: {binding.cwd}"
        )
    if binding.access == "write" and not binding.host_writable:
        raise WorkspaceContractError(
            f"provider {provider_id} workspace is not host-writable: {binding.cwd}"
        )
