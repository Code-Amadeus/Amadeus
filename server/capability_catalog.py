"""Host-owned discovery and lifecycle metadata for horizontal capabilities.

The catalog deliberately indexes native contracts instead of replacing them.
Provider manifests, MCP schemas, Agent Skills, and AUIP manifests remain owned
and validated by their existing runtimes.  This module gives those independent
contracts one package identity, trust state, health surface, and product-surface
binding without turning their payloads into a new universal schema.

The first implementation is intentionally in-memory and read-mostly.  It does
not scan the workspace, import third-party code, store secrets, or grant action
authority.  Installation persistence and external discovery can be added only
after out-of-tree contributions pass their native conformance tests.
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal


CapabilityKind = Literal["provider", "mcp_server", "skill", "auip_app"]
CapabilityHealth = Literal["ready", "degraded", "error", "disabled"]
PackageTrust = Literal["builtin", "trusted_local", "external_protocol"]

CAPABILITY_KINDS = frozenset({"provider", "mcp_server", "skill", "auip_app"})
_WORK_PROVIDER_ONLY_KINDS = frozenset({"mcp_server", "skill"})
CAPABILITY_HEALTH_STATES = frozenset({"ready", "degraded", "error", "disabled"})
PACKAGE_TRUST_STATES = frozenset({"builtin", "trusted_local", "external_protocol"})
CAPABILITY_CATALOG_SCHEMA = "amadeus.capability-catalog/v0"
CAPABILITY_PACKAGE_SCHEMA = "amadeus.capability-package/v0"
HOST_CAPABILITY_API_VERSION = "0.1"
_METADATA_LIMIT_BYTES = 64 * 1024


def _clean_identifier(value: object, field_name: str, *, limit: int = 240) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field_name} is required")
    if len(clean) > limit or any(character.isspace() for character in clean):
        raise ValueError(f"invalid {field_name}: {clean!r}")
    if any(ord(character) < 32 for character in clean):
        raise ValueError(f"invalid {field_name}: {clean!r}")
    return clean


def _clean_values(values: Iterable[object], field_name: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for value in values:
        item = _clean_identifier(value, field_name, limit=120)
        if item not in cleaned:
            cleaned.append(item)
    return tuple(cleaned)


def _json_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    try:
        encoded = json.dumps(
            source,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("capability metadata must be JSON serializable") from exc
    if len(encoded) > _METADATA_LIMIT_BYTES:
        raise ValueError("capability metadata exceeds 64 KiB")
    return json.loads(encoded.decode("utf-8"))


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    """Make one contribution visible on a stable Amadeus product surface.

    ``projection`` names the adapter shape (for example ``agent_skill`` or
    ``mcp``), not a Provider id.  A Provider-specific adapter may support or
    reject that projection without duplicating the installed capability.
    """

    surface: str
    projection: str
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "surface",
            _clean_identifier(self.surface, "capability binding surface", limit=120),
        )
        object.__setattr__(
            self,
            "projection",
            _clean_identifier(
                self.projection,
                "capability binding projection",
                limit=120,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "projection": self.projection,
            "enabled": bool(self.enabled),
        }


@dataclass(frozen=True, slots=True)
class CapabilityContribution:
    """One native capability indexed by a package.

    ``native_ref`` is an opaque lookup identity owned by the native subsystem.
    The exact Provider manifest, MCP arguments, Skill files, or AUIP manifest
    never move into this descriptor and therefore cannot drift into a second
    source of execution truth.
    """

    kind: CapabilityKind
    contribution_id: str
    contract_version: str
    native_ref: str
    summary: str = ""
    bindings: tuple[CapabilityBinding, ...] = ()
    requirements: tuple[str, ...] = ()
    enabled: bool = True
    health: CapabilityHealth = "ready"
    health_detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip().lower()
        if kind not in CAPABILITY_KINDS:
            raise ValueError(f"unsupported capability kind: {kind!r}")
        health = str(self.health or "").strip().lower()
        if health not in CAPABILITY_HEALTH_STATES:
            raise ValueError(f"unsupported capability health: {health!r}")
        bindings = tuple(self.bindings)
        if any(not isinstance(binding, CapabilityBinding) for binding in bindings):
            raise TypeError("capability bindings must be CapabilityBinding values")
        binding_keys = [(item.surface, item.projection) for item in bindings]
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("duplicate capability binding")
        if kind in _WORK_PROVIDER_ONLY_KINDS and any(
            binding.surface != "work_execution" for binding in bindings
        ):
            raise ValueError(
                f"{kind} capabilities may bind only to work_execution; "
                "they must be consumed through a Work Provider"
            )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "contribution_id",
            _clean_identifier(self.contribution_id, "contribution_id"),
        )
        object.__setattr__(
            self,
            "contract_version",
            _clean_identifier(self.contract_version, "contract_version", limit=120),
        )
        object.__setattr__(
            self,
            "native_ref",
            _clean_identifier(self.native_ref, "native_ref", limit=1024),
        )
        object.__setattr__(self, "summary", str(self.summary or "").strip()[:500])
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(
            self,
            "requirements",
            _clean_values(self.requirements, "capability requirement"),
        )
        object.__setattr__(self, "health", health)
        object.__setattr__(
            self,
            "health_detail",
            str(self.health_detail or "").strip()[:240],
        )
        object.__setattr__(self, "metadata", _json_metadata(self.metadata))

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.contribution_id)

    def supports_surface(self, surface: str) -> bool:
        clean = str(surface or "").strip()
        return any(binding.enabled and binding.surface == clean for binding in self.bindings)

    def to_dict(self, *, package_enabled: bool = True) -> dict[str, Any]:
        available = bool(
            package_enabled and self.enabled and self.health in {"ready", "degraded"}
        )
        return {
            "kind": self.kind,
            "id": self.contribution_id,
            "contract_version": self.contract_version,
            "native_ref": self.native_ref,
            "summary": self.summary,
            "bindings": [binding.to_dict() for binding in self.bindings],
            "requirements": list(self.requirements),
            "enabled": bool(self.enabled),
            "health": self.health,
            "health_detail": self.health_detail,
            "available": available,
            "consumer_scope": (
                "work_providers" if self.kind in _WORK_PROVIDER_ONLY_KINDS else "host"
            ),
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CapabilityPackage:
    """Distribution identity and lifecycle state for native contributions."""

    package_id: str
    version: str
    source: str
    digest: str
    trust: PackageTrust
    contributions: tuple[CapabilityContribution, ...]
    host_api: str = HOST_CAPABILITY_API_VERSION
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        trust = str(self.trust or "").strip().lower()
        if trust not in PACKAGE_TRUST_STATES:
            raise ValueError(f"unsupported package trust: {trust!r}")
        contributions = tuple(self.contributions)
        if not contributions:
            raise ValueError("capability package requires at least one contribution")
        if any(
            not isinstance(contribution, CapabilityContribution)
            for contribution in contributions
        ):
            raise TypeError(
                "capability package contributions must be CapabilityContribution values"
            )
        keys = [contribution.key for contribution in contributions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate contribution inside capability package")
        object.__setattr__(
            self,
            "package_id",
            _clean_identifier(self.package_id, "package_id"),
        )
        object.__setattr__(
            self,
            "version",
            _clean_identifier(self.version, "package version", limit=120),
        )
        object.__setattr__(self, "source", str(self.source or "").strip()[:500])
        object.__setattr__(
            self,
            "digest",
            _clean_identifier(self.digest, "package digest", limit=240),
        )
        object.__setattr__(self, "trust", trust)
        object.__setattr__(self, "contributions", contributions)
        object.__setattr__(
            self,
            "host_api",
            _clean_identifier(self.host_api, "host_api", limit=120),
        )
        object.__setattr__(self, "metadata", _json_metadata(self.metadata))

    def to_dict(
        self,
        *,
        kind: str = "",
        surface: str = "",
        include_disabled: bool = False,
    ) -> dict[str, Any] | None:
        selected: list[CapabilityContribution] = []
        for contribution in self.contributions:
            if kind and contribution.kind != kind:
                continue
            if surface and not contribution.supports_surface(surface):
                continue
            if not include_disabled and (
                not self.enabled
                or not contribution.enabled
                or contribution.health not in {"ready", "degraded"}
            ):
                continue
            selected.append(contribution)
        if not selected:
            return None
        return {
            "schema": CAPABILITY_PACKAGE_SCHEMA,
            "id": self.package_id,
            "version": self.version,
            "host_api": self.host_api,
            "source": self.source,
            "digest": self.digest,
            "trust": self.trust,
            "enabled": bool(self.enabled),
            "metadata": copy.deepcopy(self.metadata),
            "contributions": [
                contribution.to_dict(package_enabled=self.enabled)
                for contribution in selected
            ],
        }


class CapabilityCatalog:
    """Thread-safe in-memory package catalog with deterministic projections."""

    def __init__(self, *, host_api: str = HOST_CAPABILITY_API_VERSION) -> None:
        self.host_api = _clean_identifier(host_api, "host_api", limit=120)
        self._packages: dict[str, CapabilityPackage] = {}
        self._lock = threading.RLock()

    def register_package(
        self,
        package: CapabilityPackage,
        *,
        replace_existing: bool = False,
    ) -> None:
        if not isinstance(package, CapabilityPackage):
            raise TypeError("catalog entries must be CapabilityPackage values")
        if package.host_api != self.host_api:
            raise ValueError(
                f"incompatible Host capability API: {package.host_api!r}"
            )
        with self._lock:
            existing = self._packages.get(package.package_id)
            if existing is not None and not replace_existing:
                raise ValueError(f"capability package already registered: {package.package_id}")
            owners = {
                contribution.key: candidate.package_id
                for candidate in self._packages.values()
                if candidate.package_id != package.package_id
                for contribution in candidate.contributions
            }
            for contribution in package.contributions:
                owner = owners.get(contribution.key)
                if owner is not None:
                    raise ValueError(
                        "capability contribution already registered: "
                        f"{contribution.kind}:{contribution.contribution_id} by {owner}"
                    )
            self._packages[package.package_id] = package

    def unregister_package(self, package_id: str) -> bool:
        clean = str(package_id or "").strip()
        with self._lock:
            return self._packages.pop(clean, None) is not None

    def set_package_enabled(self, package_id: str, enabled: bool) -> CapabilityPackage:
        clean = str(package_id or "").strip()
        with self._lock:
            package = self._packages.get(clean)
            if package is None:
                raise KeyError(clean)
            updated = replace(package, enabled=bool(enabled))
            self._packages[clean] = updated
            return updated

    def set_contribution_health(
        self,
        *,
        kind: str,
        contribution_id: str,
        health: CapabilityHealth,
        detail: str = "",
    ) -> CapabilityContribution:
        key = (str(kind or "").strip().lower(), str(contribution_id or "").strip())
        with self._lock:
            for package_id, package in self._packages.items():
                for index, contribution in enumerate(package.contributions):
                    if contribution.key != key:
                        continue
                    updated = replace(
                        contribution,
                        health=health,
                        health_detail=str(detail or ""),
                    )
                    contributions = list(package.contributions)
                    contributions[index] = updated
                    self._packages[package_id] = replace(
                        package,
                        contributions=tuple(contributions),
                    )
                    return updated
        raise KeyError(f"{key[0]}:{key[1]}")

    def packages(self) -> tuple[CapabilityPackage, ...]:
        with self._lock:
            return tuple(self._packages[key] for key in sorted(self._packages))

    def snapshot(
        self,
        *,
        kind: str = "",
        surface: str = "",
        include_disabled: bool = False,
        extra_packages: Iterable[CapabilityPackage] = (),
    ) -> dict[str, Any]:
        clean_kind = str(kind or "").strip().lower()
        if clean_kind and clean_kind not in CAPABILITY_KINDS:
            raise ValueError(f"unsupported capability kind: {clean_kind!r}")
        clean_surface = str(surface or "").strip()
        with self._lock:
            combined = list(self._packages.values())
        package_ids = {package.package_id for package in combined}
        contribution_keys = {
            contribution.key
            for package in combined
            for contribution in package.contributions
        }
        for package in tuple(extra_packages):
            if not isinstance(package, CapabilityPackage):
                raise TypeError("extra catalog entries must be CapabilityPackage values")
            if package.host_api != self.host_api:
                raise ValueError(
                    f"incompatible Host capability API: {package.host_api!r}"
                )
            if package.package_id in package_ids:
                raise ValueError(
                    f"duplicate projected capability package: {package.package_id}"
                )
            for contribution in package.contributions:
                if contribution.key in contribution_keys:
                    raise ValueError(
                        "duplicate projected capability contribution: "
                        f"{contribution.kind}:{contribution.contribution_id}"
                    )
                contribution_keys.add(contribution.key)
            package_ids.add(package.package_id)
            combined.append(package)
        rows = [
            row
            for row in (
                package.to_dict(
                    kind=clean_kind,
                    surface=clean_surface,
                    include_disabled=bool(include_disabled),
                )
                for package in sorted(combined, key=lambda item: item.package_id)
            )
            if row is not None
        ]
        return {
            "schema": CAPABILITY_CATALOG_SCHEMA,
            "host_api": self.host_api,
            "packages": rows,
            "package_count": len(rows),
            "contribution_count": sum(
                len(row.get("contributions") or []) for row in rows
            ),
        }
