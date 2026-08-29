"""Amadeus-owned trust boundary for Project and workspace routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from config import settings
from server.scratch_workspace import is_scratch_path
from server.workspace_trust import (
    cwd_matches_workspace_roots,
    parse_workspace_roots,
)


ProjectRegistrySource = Literal[
    "work_project_allowlist",
    "none",
]


@dataclass(frozen=True, slots=True)
class ProjectRegistryConfig:
    roots: tuple[str, ...]
    source: ProjectRegistrySource

    def to_dict(self) -> dict[str, Any]:
        return {
            "roots": list(self.roots),
            "source": self.source,
        }


def project_registry_config() -> ProjectRegistryConfig:
    """Read the Host-owned trust roots for persistent Project routing."""

    configured = parse_workspace_roots(
        str(getattr(settings, "WORK_PROJECT_ALLOWLIST", "") or "")
    )
    if configured:
        return ProjectRegistryConfig(
            roots=configured,
            source="work_project_allowlist",
        )
    return ProjectRegistryConfig(roots=(), source="none")


def project_registry_entries() -> list[str]:
    return list(project_registry_config().roots)


def cwd_in_project_registry(cwd: str | None) -> bool:
    """Whether the host may expose this cwd as a Project destination."""

    if not cwd:
        return False
    # Scratch is allocated and owned by Amadeus. Requiring it in deployment
    # config would make the safe default destination fail on every fresh setup.
    if is_scratch_path(cwd):
        return True
    return cwd_matches_workspace_roots(cwd, project_registry_config().roots)
