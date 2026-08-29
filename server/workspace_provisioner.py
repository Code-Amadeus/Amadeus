"""Host-owned allocation of isolated Git workspaces for WorkItems.

Providers consume the final ``cwd``.  They do not choose, create, or own the
workspace that gives a durable WorkItem its filesystem identity.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from config import settings
from server.scratch_workspace import slugify


CREATE_NO_WINDOW = 0x08000000


class WorkspaceProvisioningError(RuntimeError):
    """The Host could not establish the requested workspace boundary."""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = str(code or "")


def ensure_workspace(
    *,
    work_item_external_id: str,
    project_cwd: str,
    policy: str = "worktree",
    base_ref: str | None = None,
    name: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Create or reuse the Host workspace bound to one WorkItem.

    Non-Git projects honestly retain their local directory and single-writer
    lease.  Git projects get a stable branch and linked worktree below the
    configured Host state root.  Every failure after Git ownership is known
    fails closed instead of falling back to the shared checkout.
    """

    external_id = str(work_item_external_id or "").strip()
    if not external_id:
        raise WorkspaceProvisioningError(
            "work item identity is required",
            code="invalid_identity",
        )
    requested_policy = str(policy or "worktree").strip().lower()
    if requested_policy != "worktree":
        raise WorkspaceProvisioningError(
            f"unsupported workspace policy: {requested_policy}",
            code="unsupported_policy",
        )
    try:
        project = Path(project_cwd).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceProvisioningError(
            f"project workspace is unavailable: {project_cwd}",
            code="project_unavailable",
        ) from exc
    if not project.is_dir():
        raise WorkspaceProvisioningError(
            f"project workspace is not a directory: {project}",
            code="project_unavailable",
        )

    timeout = max(
        1.0,
        float(
            timeout_s
            if timeout_s is not None
            else getattr(settings, "WORK_WORKTREE_ENSURE_TIMEOUT_S", 30.0)
        ),
    )
    repository = _git_toplevel(project, timeout=timeout)
    if repository is None:
        return _local_workspace(project, external_id)

    base_revision = _resolve_commit(repository, base_ref or "HEAD", timeout=timeout)
    if not base_revision:
        # An unborn repository cannot have a linked worktree.  The local path
        # remains truthful and its existing single-writer lease still applies.
        return _local_workspace(repository, external_id)

    branch = _branch_name(external_id)
    worktree = _worktree_path(repository, external_id, name)
    if worktree.exists():
        return _existing_worktree(
            repository,
            worktree,
            external_id=external_id,
            base_revision=base_revision,
            timeout=timeout,
        )

    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = _git_ok(
        repository,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        timeout=timeout,
    )
    command = ["worktree", "add"]
    if not branch_exists:
        command.extend(["-b", branch])
    command.extend([str(worktree), branch if branch_exists else base_revision])
    process = _git(repository, *command, timeout=timeout)
    if process.returncode != 0:
        diagnostics = _diagnostic_tail(process.stderr or process.stdout)
        raise WorkspaceProvisioningError(
            f"git worktree allocation failed: {diagnostics}",
            code="worktree_failed",
        )
    return _worktree_envelope(
        cwd=worktree,
        external_id=external_id,
        branch=branch,
        base_revision=base_revision,
        created=True,
    )


def _git_toplevel(project: Path, *, timeout: float) -> Path | None:
    try:
        process = _git(project, "rev-parse", "--show-toplevel", timeout=timeout)
    except FileNotFoundError as exc:
        raise WorkspaceProvisioningError(
            "git executable is unavailable",
            code="git_unavailable",
        ) from exc
    if process.returncode != 0:
        return None
    raw = str(process.stdout or "").strip()
    if not raw:
        return None
    try:
        root = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceProvisioningError(
            "git returned an unusable repository root",
            code="bad_git_response",
        ) from exc
    return root if root.is_dir() else None


def _resolve_commit(repository: Path, ref: str, *, timeout: float) -> str:
    process = _git(
        repository,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{ref}^{{commit}}",
        timeout=timeout,
    )
    return str(process.stdout or "").strip() if process.returncode == 0 else ""


def _existing_worktree(
    repository: Path,
    worktree: Path,
    *,
    external_id: str,
    base_revision: str,
    timeout: float,
) -> dict[str, Any]:
    actual_root = _git_toplevel(worktree, timeout=timeout)
    if actual_root is None or os.path.normcase(str(actual_root)) != os.path.normcase(
        str(worktree.resolve())
    ):
        raise WorkspaceProvisioningError(
            f"workspace path already exists without the expected worktree: {worktree}",
            code="workspace_collision",
        )
    project_common = _git_common_dir(repository, timeout=timeout)
    worktree_common = _git_common_dir(worktree, timeout=timeout)
    if project_common != worktree_common:
        raise WorkspaceProvisioningError(
            f"workspace path belongs to a different Git repository: {worktree}",
            code="workspace_collision",
        )
    branch_process = _git(
        worktree,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        timeout=timeout,
    )
    branch = str(branch_process.stdout or "").strip()
    if branch_process.returncode != 0 or branch != _branch_name(external_id):
        raise WorkspaceProvisioningError(
            f"workspace path is not bound to the expected WorkItem branch: {worktree}",
            code="workspace_collision",
        )
    return _worktree_envelope(
        cwd=worktree,
        external_id=external_id,
        branch=branch,
        base_revision=base_revision,
        created=False,
    )


def _git_common_dir(repository: Path, *, timeout: float) -> str:
    process = _git(repository, "rev-parse", "--git-common-dir", timeout=timeout)
    if process.returncode != 0:
        raise WorkspaceProvisioningError(
            "could not verify Git repository identity",
            code="bad_git_response",
        )
    raw = Path(str(process.stdout or "").strip())
    resolved = raw if raw.is_absolute() else repository / raw
    return os.path.normcase(str(resolved.resolve()))


def _worktree_path(repository: Path, external_id: str, name: str | None) -> Path:
    configured = Path(str(settings.WORK_WORKTREE_ROOT)).expanduser().resolve()
    repository_key = hashlib.sha256(
        os.path.normcase(str(repository)).encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:12]
    stem = slugify(str(name or ""))[:32]
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "-", external_id).strip("-")[-16:]
    directory = f"{stem}-{suffix}" if stem else suffix
    return configured / repository_key / directory


def _branch_name(external_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", external_id).strip("-")
    if not safe:
        raise WorkspaceProvisioningError(
            "work item identity cannot form a Git branch",
            code="invalid_identity",
        )
    return f"amadeus/work/{safe}"


def _local_workspace(project: Path, external_id: str) -> dict[str, Any]:
    return {
        "created": False,
        "workspace": {
            "allocationId": external_id,
            "backend": "host-local",
            "cwd": str(project),
            "gitBranch": "",
            "baseRef": "",
            "policy": "local",
            "exists": True,
        },
    }


def _worktree_envelope(
    *,
    cwd: Path,
    external_id: str,
    branch: str,
    base_revision: str,
    created: bool,
) -> dict[str, Any]:
    return {
        "created": created,
        "workspace": {
            "allocationId": external_id,
            "backend": "host-git-worktree",
            "cwd": str(cwd.resolve()),
            "gitBranch": branch,
            "baseRef": base_revision,
            "policy": "worktree",
            "exists": True,
        },
    }


def _git(repository: Path, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **(
            {"creationflags": CREATE_NO_WINDOW}
            if os.name == "nt"
            else {}
        ),
    )


def _git_ok(repository: Path, *args: str, timeout: float) -> bool:
    return _git(repository, *args, timeout=timeout).returncode == 0


def _diagnostic_tail(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:800] or "no diagnostics"


__all__ = ["WorkspaceProvisioningError", "ensure_workspace"]
