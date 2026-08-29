"""Compose native Amadeus capabilities into catalog metadata.

This module is the adapter between existing owners and the horizontal catalog.
It never becomes a routing or execution source of truth: Provider selection
still reads ``ProviderRuntime`` manifests, MCP calls still use discovered tool
schemas and exact arguments, Skills still load their files, and AUIP launch
still revalidates its artifact revision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from agent_host.provider_authoring import auip_authoring_source_identity
from agent_host.provider_contract import ProviderManifest
from agent_host.mcp_connections import (
    MCP_CONNECTION_PROJECTION,
    McpConnectionSpec,
)
from server.capability_catalog import (
    CapabilityBinding,
    CapabilityCatalog,
    CapabilityContribution,
    CapabilityPackage,
)


BUILTIN_PROVIDER_PACKAGE_ID = "amadeus.builtin.providers"
BUILTIN_AUIP_AUTHORING_PACKAGE_ID = "amadeus.builtin.auip-authoring"
USER_MCP_CONNECTION_PACKAGE_ID = "amadeus.user.mcp-connections"


def _digest_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def provider_capability_package(
    manifests: Iterable[ProviderManifest],
) -> CapabilityPackage | None:
    """Index the live Provider registry and optional MCP protocol facets."""

    ordered = tuple(sorted(tuple(manifests), key=lambda item: item.provider_id))
    if not ordered:
        return None
    contributions: list[CapabilityContribution] = []
    for manifest in ordered:
        provider_id = str(manifest.provider_id or "").strip().lower()
        contributions.append(
            CapabilityContribution(
                kind="provider",
                contribution_id=provider_id,
                contract_version=manifest.contract_version,
                native_ref=f"provider:{provider_id}",
                summary=(
                    f"{manifest.display_name} execution Provider "
                    f"({manifest.runtime_kind})."
                ),
                bindings=(
                    CapabilityBinding(
                        surface="work_execution",
                        projection="provider_manifest",
                    ),
                ),
                metadata={
                    "display_name": manifest.display_name,
                    "runtime_kind": manifest.runtime_kind,
                    "task_kinds": list(manifest.capabilities.task_kinds),
                    "declared": bool(manifest.declared),
                },
            )
        )
        if str(manifest.runtime_kind or "").strip().lower() == "mcp_server":
            contributions.append(
                CapabilityContribution(
                    kind="mcp_server",
                    contribution_id=provider_id,
                    contract_version="mcp",
                    native_ref=f"mcp:{provider_id}",
                    summary=(
                        f"{manifest.display_name} MCP tools and resources."
                    ),
                    bindings=(
                        CapabilityBinding(
                            surface="work_execution",
                            projection="mcp_provider",
                        ),
                    ),
                    requirements=("mcp_client",),
                    metadata={
                        "provider_id": provider_id,
                        "operation_ids": [
                            operation.operation_id
                            for operation in manifest.capabilities.operations
                        ],
                    },
                )
            )
    manifest_projection = [manifest.to_dict() for manifest in ordered]
    return CapabilityPackage(
        package_id=BUILTIN_PROVIDER_PACKAGE_ID,
        version="0.1.0",
        source="builtin:provider-runtime",
        digest=_digest_json(manifest_projection),
        trust="builtin",
        contributions=tuple(contributions),
        metadata={"native_registry": "ProviderRuntime"},
    )


def sync_provider_capabilities(
    catalog: CapabilityCatalog,
    manifests: Iterable[ProviderManifest],
) -> None:
    """Replace the read-only Provider projection after runtime composition."""

    package = provider_capability_package(manifests)
    if package is None:
        catalog.unregister_package(BUILTIN_PROVIDER_PACKAGE_ID)
        return
    catalog.register_package(package, replace_existing=True)


def mcp_connection_capability_package(
    connections: Iterable[McpConnectionSpec],
) -> CapabilityPackage | None:
    """Project the encrypted desktop registry without copying its secrets."""

    ordered = tuple(sorted(tuple(connections), key=lambda item: item.connection_id))
    if not ordered:
        return None
    public_projection = [item.public_dict() for item in ordered]
    contributions = tuple(
        CapabilityContribution(
            kind="mcp_server",
            contribution_id=item.connection_id,
            contract_version="mcp",
            native_ref=f"mcp-connection:{item.connection_id}",
            summary=f"{item.display_name} MCP connection.",
            bindings=(
                CapabilityBinding(
                    surface="work_execution",
                    projection=MCP_CONNECTION_PROJECTION,
                ),
            ),
            requirements=("mcp_client",),
            enabled=item.enabled,
            health=(
                "ready"
                if item.enabled and item.provider_ids
                else "degraded"
                if item.enabled
                else "disabled"
            ),
            health_detail=(
                ""
                if item.enabled and item.provider_ids
                else "No compatible Work Provider is selected."
                if item.enabled
                else "Connection is disabled."
            ),
            metadata={
                "provider_ids": list(item.provider_ids),
                "transport": item.transport,
                "environment_keys": sorted(item.environment),
                "main_chat_access": False,
            },
        )
        for item in ordered
    )
    return CapabilityPackage(
        package_id=USER_MCP_CONNECTION_PACKAGE_ID,
        version="0.1.0",
        source="desktop:mcp-registry",
        digest=_digest_json(public_projection),
        trust="trusted_local",
        contributions=contributions,
        metadata={
            "native_registry": "DesktopSettingsStore",
            "main_chat_access": False,
        },
    )


def sync_mcp_connection_capabilities(
    catalog: CapabilityCatalog,
    connections: Iterable[McpConnectionSpec],
) -> None:
    package = mcp_connection_capability_package(connections)
    if package is None:
        catalog.unregister_package(USER_MCP_CONNECTION_PACKAGE_ID)
        return
    catalog.register_package(package, replace_existing=True)


def builtin_auip_authoring_package() -> CapabilityPackage:
    """Index the one built-in Skill without copying its prompt or assets."""

    identity = auip_authoring_source_identity()
    raw_files = identity.get("files")
    relative_files = tuple(
        str(value)
        for value in raw_files
    ) if isinstance(raw_files, (list, tuple)) else ()
    return CapabilityPackage(
        package_id=BUILTIN_AUIP_AUTHORING_PACKAGE_ID,
        version="0.1.0",
        source="builtin:auip-authoring",
        digest=str(identity["digest"]),
        trust="builtin",
        contributions=(
            CapabilityContribution(
                kind="skill",
                contribution_id=str(identity["skill_id"]),
                contract_version="amadeus.skill/v0",
                native_ref="skill:auip-authoring",
                summary=(
                    "Author or adapt one application to the AUIP contract."
                ),
                bindings=(
                    CapabilityBinding(
                        surface="work_execution",
                        projection="agent_skill",
                    ),
                ),
                requirements=(
                    "workspace_write",
                    "shell",
                    "artifact_output",
                ),
                metadata={
                    "relative_path": str(identity["skill_relative_path"]),
                    "progressive_disclosure": True,
                    "file_count": len(relative_files),
                },
            ),
        ),
        metadata={"native_owner": "agent_host.provider_authoring"},
    )


def auip_app_capability_packages(
    candidates: Iterable[Any],
) -> tuple[CapabilityPackage, ...]:
    """Project verified AUIP launch candidates without changing launch truth.

    AUIP actions remain AUIP bridge calls.  Indexing an application here does
    not turn its action catalog into MCP and does not grant an MCP server to the
    Participant, Controller, or speaking role.
    """

    packages: list[CapabilityPackage] = []
    for candidate in candidates:
        artifact_id = str(getattr(candidate, "artifact_id", "") or "").strip()
        artifact_ref = str(getattr(candidate, "artifact_ref", "") or "").strip()
        if not artifact_id or not artifact_ref:
            continue
        app_id = str(getattr(candidate, "app_id", "") or artifact_id).strip()
        title = str(getattr(candidate, "title", "") or app_id).strip()
        version = str(getattr(candidate, "app_version", "") or "0").strip()
        stances = tuple(str(value) for value in getattr(candidate, "stances", ()) or ())
        modes = ["observe"]
        if "participant" in stances:
            modes.extend(("collaborate", "delegate"))
        packages.append(
            CapabilityPackage(
                package_id=f"amadeus.auip.artifact.{artifact_id}",
                version=version,
                source="work-ledger:launchable-auip",
                digest=hashlib.sha256(artifact_ref.encode("utf-8")).hexdigest(),
                trust="trusted_local",
                contributions=(
                    CapabilityContribution(
                        kind="auip_app",
                        contribution_id=artifact_id,
                        contract_version="amadeus.auip/v0",
                        native_ref=artifact_ref,
                        summary=f"Launch {title} as a verified AUIP application.",
                        bindings=(
                            CapabilityBinding(
                                surface="role_chat",
                                projection="auip_launch",
                            ),
                            CapabilityBinding(
                                surface="app_runtime",
                                projection="auip_attach",
                            ),
                        ),
                        requirements=("verified_artifact",),
                        metadata={
                            "app_id": app_id,
                            "title": title,
                            "work_item_id": str(
                                getattr(candidate, "work_item_id", "") or ""
                            ),
                            "modes": modes,
                        },
                    ),
                ),
                metadata={"dynamic_projection": True},
            )
        )
    return tuple(packages)
