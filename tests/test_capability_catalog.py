"""Horizontal capability management without changing native authority."""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import patch

import pytest

from agent_host.provider_bootstrap import BuiltinProviderSpec
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST, OPENCLAW_MANIFEST
from agent_host.provider_contract import (
    ProviderCapabilities,
    ProviderManifest,
    ProviderOperation,
)
from agent_host.provider_runtime import ProviderRuntime
from server.capability_catalog import (
    CAPABILITY_CATALOG_SCHEMA,
    CapabilityBinding,
    CapabilityCatalog,
    CapabilityContribution,
    CapabilityPackage,
)
from server.capability_composition import (
    BUILTIN_AUIP_AUTHORING_PACKAGE_ID,
    BUILTIN_PROVIDER_PACKAGE_ID,
    auip_app_capability_packages,
    builtin_auip_authoring_package,
    provider_capability_package,
    sync_provider_capabilities,
)
from server.handlers.capability_handler import CapabilityHandler
from server.handlers.provider_handler import ProviderHandler
from server.protocol import Method


def _package(
    package_id: str,
    *contributions: CapabilityContribution,
) -> CapabilityPackage:
    return CapabilityPackage(
        package_id=package_id,
        version="0.1.0",
        source=f"test:{package_id}",
        digest=hashlib.sha256(package_id.encode("utf-8")).hexdigest(),
        trust="builtin",
        contributions=tuple(contributions),
    )


def _contribution(
    kind: str,
    contribution_id: str,
    *,
    surface: str = "work_execution",
    health: str = "ready",
) -> CapabilityContribution:
    return CapabilityContribution(
        kind=kind,  # type: ignore[arg-type]
        contribution_id=contribution_id,
        contract_version="v0",
        native_ref=f"{kind}:{contribution_id}",
        bindings=(CapabilityBinding(surface=surface, projection="native"),),
        health=health,  # type: ignore[arg-type]
    )


def test_catalog_keeps_package_lifecycle_separate_from_native_contracts() -> None:
    catalog = CapabilityCatalog()
    skill = _contribution("skill", "authoring")
    catalog.register_package(_package("example.authoring", skill))

    snapshot = catalog.snapshot()

    assert snapshot["schema"] == CAPABILITY_CATALOG_SCHEMA
    assert snapshot["package_count"] == 1
    assert snapshot["contribution_count"] == 1
    projected = snapshot["packages"][0]["contributions"][0]
    assert projected["native_ref"] == "skill:authoring"
    assert projected["available"] is True
    assert "payload" not in projected
    assert "schema" not in projected["metadata"]

    catalog.set_package_enabled("example.authoring", False)
    assert catalog.snapshot()["packages"] == []
    disabled = catalog.snapshot(include_disabled=True)
    assert disabled["packages"][0]["enabled"] is False
    assert disabled["packages"][0]["contributions"][0]["available"] is False


def test_bad_contribution_is_isolated_and_duplicate_native_identity_fails() -> None:
    catalog = CapabilityCatalog()
    catalog.register_package(
        _package(
            "example.mixed",
            _contribution("skill", "healthy"),
            _contribution("mcp_server", "broken", health="error"),
        )
    )

    visible = catalog.snapshot()
    assert visible["contribution_count"] == 1
    assert visible["packages"][0]["contributions"][0]["id"] == "healthy"
    all_rows = catalog.snapshot(include_disabled=True)
    assert {row["id"] for row in all_rows["packages"][0]["contributions"]} == {
        "healthy",
        "broken",
    }

    try:
        catalog.register_package(
            _package("example.duplicate", _contribution("skill", "healthy"))
        )
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("two packages cannot own one native contribution")


def test_skills_and_mcp_can_bind_only_to_work_provider_execution() -> None:
    for kind in ("skill", "mcp_server"):
        with pytest.raises(ValueError, match="Work Provider"):
            CapabilityContribution(
                kind=kind,
                contribution_id=f"direct-{kind}",
                contract_version="1",
                native_ref=f"{kind}:direct",
                bindings=(
                    CapabilityBinding(
                        surface="role_chat",
                        projection="direct_tool",
                    ),
                ),
            )


def test_skill_and_mcp_snapshots_declare_work_provider_consumer_scope() -> None:
    for kind in ("skill", "mcp_server"):
        contribution = _contribution(kind, f"shared-{kind}")
        assert contribution.to_dict()["consumer_scope"] == "work_providers"


def test_provider_must_explicitly_accept_a_shared_capability_projection() -> None:
    assert "agent_skill" in (
        CODEX_APP_SERVER_MANIFEST.capabilities.capability_projections
    )
    assert "agent_skill" not in OPENCLAW_MANIFEST.capabilities.capability_projections


def test_provider_projection_indexes_mcp_without_replacing_provider_manifest() -> None:
    manifest = ProviderManifest(
        provider_id="warehouse",
        display_name="Warehouse",
        runtime_kind="mcp_server",
        contract_version="0.3",
        capabilities=ProviderCapabilities(
            task_kinds=("inventory",),
            operations=(
                ProviderOperation(
                    "lookup_inventory",
                    outcome_facet="mcp.tool_result",
                ),
            ),
        ),
    )
    package = provider_capability_package((manifest,))
    assert package is not None
    assert package.package_id == BUILTIN_PROVIDER_PACKAGE_ID
    assert {item.kind for item in package.contributions} == {
        "provider",
        "mcp_server",
    }
    provider = next(item for item in package.contributions if item.kind == "provider")
    mcp = next(item for item in package.contributions if item.kind == "mcp_server")
    assert provider.native_ref == "provider:warehouse"
    assert mcp.native_ref == "mcp:warehouse"
    assert mcp.metadata["operation_ids"] == ["lookup_inventory"]
    assert "inputSchema" not in mcp.metadata
    # The catalog remains a projection; selection still consumes the exact
    # original object and its complete native capability contract.
    assert manifest.capabilities.operation("lookup_inventory") is not None

    catalog = CapabilityCatalog()
    sync_provider_capabilities(catalog, (manifest,))
    assert catalog.snapshot(surface="role_chat")["contribution_count"] == 0
    assert catalog.snapshot(surface="work_execution")["contribution_count"] == 2


def test_provider_handler_composition_populates_catalog_without_changing_runtime() -> None:
    manifest = ProviderManifest(
        provider_id="fixture",
        display_name="Fixture Provider",
        capabilities=ProviderCapabilities(task_kinds=("general",)),
    )

    class Adapter:
        provider_id = "fixture"

        def __init__(self) -> None:
            self.manifest = manifest

    local_runtime = ProviderRuntime()
    catalog = CapabilityCatalog()
    specs = (
        BuiltinProviderSpec(
            provider_id="fixture",
            factory=Adapter,  # type: ignore[arg-type]
            runtime_enabled=True,
        ),
    )
    with patch(
        "server.handlers.provider_handler.builtin_provider_specs",
        return_value=specs,
    ), patch("server.handlers.provider_handler.runtime", local_runtime):
        ProviderHandler(capability_catalog=catalog)

    assert local_runtime.get_manifest("fixture") is manifest
    snapshot = catalog.snapshot(kind="provider")
    assert snapshot["contribution_count"] == 1
    assert snapshot["packages"][0]["contributions"][0]["native_ref"] == (
        "provider:fixture"
    )


def test_builtin_authoring_skill_is_path_free_and_progressively_disclosed() -> None:
    package = builtin_auip_authoring_package()
    assert package.package_id == BUILTIN_AUIP_AUTHORING_PACKAGE_ID
    assert len(package.digest) == 64
    skill = package.contributions[0]
    assert skill.kind == "skill"
    assert skill.native_ref == "skill:auip-authoring"
    assert skill.metadata["relative_path"] == "skills/auip-authoring/SKILL.md"
    assert skill.metadata["progressive_disclosure"] is True
    assert ":\\" not in str(skill.metadata)
    assert "/Users/" not in str(skill.metadata)


def test_dynamic_auip_projection_pins_artifact_ref_without_copying_app_payload() -> None:
    candidate = type(
        "Candidate",
        (),
        {
            "artifact_id": "artifact-42",
            "artifact_ref": "export-bundle:manifest-7@0123456789abcdef",
            "app_id": "gomoku",
            "app_version": "0.1.0",
            "title": "Gomoku",
            "work_item_id": "work-9",
            "stances": ("spectator", "participant"),
        },
    )()
    packages = auip_app_capability_packages((candidate,))
    assert len(packages) == 1
    app = packages[0].contributions[0]
    assert app.native_ref == candidate.artifact_ref
    assert app.metadata["modes"] == ["observe", "collaborate", "delegate"]
    assert "manifest" not in app.metadata
    assert "entry_path" not in app.metadata
    assert packages[0].trust == "trusted_local"


def test_read_only_handler_filters_surfaces_and_degrades_on_projection_failure() -> None:
    catalog = CapabilityCatalog()
    catalog.register_package(
        _package(
            "example.static",
            _contribution("skill", "authoring", surface="work_execution"),
        )
    )
    dynamic = _package(
        "example.dynamic",
        _contribution("auip_app", "game", surface="role_chat"),
    )
    handler = CapabilityHandler(catalog, extra_packages=lambda: (dynamic,))

    role = asyncio.run(
        handler.handle(Method.CAPABILITY_LIST, {"surface": "role_chat"})
    )
    assert role is not None
    assert role["contribution_count"] == 1
    assert role["packages"][0]["contributions"][0]["id"] == "game"

    broken = CapabilityHandler(
        catalog,
        extra_packages=lambda: (_ for _ in ()).throw(RuntimeError("private path")),
    )
    degraded = asyncio.run(broken.handle(Method.CAPABILITY_LIST, {}))
    assert degraded is not None
    assert degraded["contribution_count"] == 1
    assert degraded["projection_errors"] == [
        {"source": "dynamic", "code": "projection_failed"}
    ]
    assert "private path" not in str(degraded)
