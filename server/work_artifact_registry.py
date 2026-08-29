"""Baseline-aware Git artifact discovery for durable WorkItem attempts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from agent_host.work_ledger_store import WorkLedgerStore
from agent_host.work_ledger_types import RunAttemptRecord, WorkItemRecord
from server.local_git import collect_diff, run_git


_HASH_LIMIT_BYTES = 64 * 1024 * 1024
_UNTRACKED_PATCH_FILE_LIMIT_BYTES = 512 * 1024
_UNTRACKED_PATCH_TOTAL_LIMIT_BYTES = 1024 * 1024


class WorkArtifactRegistry:
    """Capture an attempt boundary and register only its attributable delta.

    The registry deliberately does not scan Desktop/Home. External files enter
    through explicit provider artifact events or an approved export contract;
    Git discovery stays within the WorkItem workspace.
    """

    def __init__(self, store: WorkLedgerStore) -> None:
        self.store = store

    async def capture_baseline(
        self,
        attempt: RunAttemptRecord,
        item: WorkItemRecord,
    ) -> dict[str, Any]:
        baseline = await capture_git_baseline(item.workspace_path)
        lineage_paths = self._lineage_owned_dirty_paths(
            attempt=attempt,
            item=item,
            baseline=baseline,
        )
        if lineage_paths:
            baseline["lineage_owned_dirty_paths"] = lineage_paths
            baseline["lineage_owner_work_item_id"] = str(
                attempt.metadata.get("related_work_item_id") or item.work_item_id
            ).strip()
        self.store.update_attempt(
            attempt.attempt_id,
            metadata={"git_baseline": baseline},
        )
        return baseline

    def _lineage_owned_dirty_paths(
        self,
        *,
        attempt: RunAttemptRecord,
        item: WorkItemRecord,
        baseline: dict[str, Any],
    ) -> list[str]:
        """Return baseline-dirty paths an explicit amendment may own.

        A dirty working tree normally has unknown authorship and remains a
        conflict boundary. The narrow exception is an explicit amendment whose
        WorkItem (or legacy related WorkItem) already registered those exact
        files as business artifacts. This lets a later Operation evolve the
        goal's own deliverable without weakening protection for unrelated user
        changes.
        """

        if str(attempt.metadata.get("intent") or "").strip().lower() != "amend":
            return []
        related_id = str(
            attempt.metadata.get("related_work_item_id") or item.work_item_id
        ).strip()
        if not related_id or not baseline.get("available"):
            return []
        predecessor = self.store.get_work_item(related_id)
        if predecessor is None or predecessor.project_id != item.project_id:
            return []
        try:
            same_workspace = os.path.normcase(
                str(Path(predecessor.workspace_path).resolve())
            ) == os.path.normcase(str(Path(item.workspace_path).resolve()))
        except (OSError, ValueError):
            return []
        if not same_workspace:
            return []

        dirty_by_key = {
            _path_key(path): str(path)
            for path in baseline.get("dirty_files") or []
            if str(path or "").strip()
        }
        owned: list[str] = []
        for artifact in self.store.list_artifacts(related_id):
            if artifact.kind != "business.file" or artifact.status not in {
                "registered",
                "approved",
            }:
                continue
            relative_path = str(artifact.metadata.get("relative_path") or "").strip()
            if not relative_path:
                continue
            key = _path_key(relative_path)
            if key in dirty_by_key and dirty_by_key[key] not in owned:
                owned.append(dirty_by_key[key])
        return owned[:160]

    async def finalize_attempt(
        self,
        attempt: RunAttemptRecord,
        item: WorkItemRecord,
    ) -> dict[str, Any]:
        baseline = attempt.metadata.get("git_baseline") if isinstance(attempt.metadata.get("git_baseline"), dict) else {}
        if not baseline:
            # A legacy/imported run has no safe attribution boundary. Capture
            # facts for diagnostics but do not claim the current dirty tree.
            return {
                "available": False,
                "reason": "missing_attempt_baseline",
                "changed_files": [],
                "ambiguous_paths": [],
            }
        delta = await collect_git_delta(item.workspace_path, baseline)
        self.store.update_attempt(attempt.attempt_id, metadata={"git_delta": delta})
        if not delta.get("available"):
            return delta

        changed_files = [str(path) for path in delta.get("changed_files") or []]
        ambiguous = {str(path) for path in delta.get("ambiguous_paths") or []}
        lineage_owned = {str(path) for path in delta.get("lineage_owned_paths") or []}
        if not changed_files and not delta.get("conflicts"):
            delta["artifact_ids"] = []
            return delta
        artifact_ids: list[str] = []
        delta_identity = f"git.delta:{attempt.attempt_id}"
        delta_artifact = self.store.register_artifact(
            item.work_item_id,
            attempt_id=attempt.attempt_id,
            kind="git.delta",
            title=f"Git delta for attempt {attempt.attempt_number}",
            uri=f"work-ledger://git-delta/{attempt.attempt_id}",
            identity=delta_identity,
            status="pending" if ambiguous or delta.get("conflicts") else "registered",
            metadata={"delta": delta},
        )
        artifact_ids.append(delta_artifact.artifact_id)

        repo_root = Path(str(delta.get("repo_root") or item.workspace_path))
        file_facts = delta.get("file_facts") if isinstance(delta.get("file_facts"), dict) else {}
        for relative_path in changed_files:
            candidate = _safe_repo_path(repo_root, relative_path)
            fact = file_facts.get(relative_path) if isinstance(file_facts.get(relative_path), dict) else {}
            artifact = self.store.register_artifact(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="business.file",
                title=Path(relative_path).name or relative_path,
                path=str(candidate) if candidate is not None else None,
                identity=f"git.path:{_path_key(relative_path)}",
                status="pending" if relative_path in ambiguous else ("registered" if fact.get("exists") else "missing"),
                sha256=str(fact.get("sha256") or ""),
                size_bytes=int(fact["size_bytes"]) if fact.get("size_bytes") is not None else None,
                modified_at=float(fact["modified_at"]) if fact.get("modified_at") is not None else None,
                metadata={
                    "relative_path": relative_path,
                    "attribution": (
                        "ambiguous_origin"
                        if relative_path in ambiguous
                        else "lineage_amendment"
                        if relative_path in lineage_owned
                        else "workspace_window"
                    ),
                    "exists": bool(fact.get("exists")),
                    "deleted": not bool(fact.get("exists")),
                },
            )
            artifact_ids.append(artifact.artifact_id)
        delta["artifact_ids"] = artifact_ids
        return delta

    def register_attempt_files(
        self,
        attempt: RunAttemptRecord,
        item: WorkItemRecord,
        *,
        root: Path,
        files: tuple[Path, ...],
        attribution: str,
    ) -> list[str]:
        """Register a pre-validated, Attempt-owned output bundle.

        Git delta remains the normal source of workspace artifacts.  Some
        Host-owned staging namespaces are deliberately gitignored, however,
        so a semantic outcome may need to materialize their files before it
        can be verified.  The caller supplies a bounded file set from the
        owner of that namespace; this method only snapshots identity and
        bytes into the existing ``business.file`` contract.
        """

        workspace = Path(item.workspace_path).resolve()
        output_root = Path(root).resolve()
        try:
            output_root.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("attempt output root escaped its WorkItem workspace") from exc

        artifact_ids: list[str] = []
        for raw_path in files:
            candidate = Path(raw_path).resolve()
            try:
                output_relative = candidate.relative_to(output_root)
                workspace_relative = candidate.relative_to(workspace)
            except ValueError as exc:
                raise ValueError("attempt output file escaped its declared root") from exc
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError("attempt output must contain regular files only")
            try:
                raw = candidate.read_bytes()
                info = candidate.stat()
            except OSError as exc:
                raise ValueError("attempt output file became unreadable") from exc
            artifact = self.store.register_artifact(
                item.work_item_id,
                attempt_id=attempt.attempt_id,
                kind="business.file",
                title=candidate.name,
                path=candidate,
                identity=(
                    f"attempt-output:{attempt.attempt_id}:"
                    f"{output_relative.as_posix().casefold()}"
                ),
                status="registered",
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                modified_at=info.st_mtime,
                metadata={
                    "relative_path": workspace_relative.as_posix(),
                    "output_relative_path": output_relative.as_posix(),
                    "attribution": str(attribution or "attempt_output"),
                },
            )
            artifact_ids.append(artifact.artifact_id)
        return artifact_ids

    def delta_for_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        attempt = self.store.get_attempt(attempt_id)
        if attempt is None:
            return None
        cached = attempt.metadata.get("git_delta") if isinstance(attempt.metadata.get("git_delta"), dict) else None
        if cached is not None:
            return dict(cached)
        for artifact in self.store.list_artifacts(attempt.work_item_id, attempt_id=attempt_id):
            if artifact.kind != "git.delta":
                continue
            delta = artifact.metadata.get("delta") if isinstance(artifact.metadata.get("delta"), dict) else None
            if delta is not None:
                return dict(delta)
        return None


async def capture_git_baseline(cwd: str) -> dict[str, Any]:
    repo = await run_git(cwd, ["rev-parse", "--show-toplevel"])
    if repo["returncode"] != 0:
        return {
            "available": False,
            "reason": "not_a_git_workspace",
            "workspace_path": str(Path(cwd).resolve()),
        }
    repo_root = str(Path(repo["stdout"].strip()).resolve())
    head_result, branch_result, dirty = await asyncio.gather(
        run_git(repo_root, ["rev-parse", "--verify", "HEAD"]),
        run_git(repo_root, ["branch", "--show-current"]),
        collect_diff(repo_root),
    )
    dirty_files = _dedupe_paths([*(dirty.get("changed_files") or []), *(dirty.get("untracked") or [])])
    fingerprints = await _fingerprints(repo_root, dirty_files)
    return {
        "available": True,
        "repo_root": repo_root,
        "head": head_result["stdout"].strip() if head_result["returncode"] == 0 else "",
        "branch": branch_result["stdout"].strip() if branch_result["returncode"] == 0 else "",
        "dirty_files": dirty_files,
        "untracked": list(dirty.get("untracked") or []),
        "dirty_patch_sha256": _text_hash(str(dirty.get("patch") or "")),
        "fingerprints": fingerprints,
    }


async def collect_git_delta(
    cwd: str,
    baseline: dict[str, Any],
    *,
    include_patch: bool = True,
    verify_baseline_dirty: bool = True,
) -> dict[str, Any]:
    if not baseline.get("available"):
        return {
            "available": False,
            "reason": str(baseline.get("reason") or "baseline_unavailable"),
            "changed_files": [],
            "ambiguous_paths": [],
        }
    repo = await run_git(cwd, ["rev-parse", "--show-toplevel"])
    if repo["returncode"] != 0:
        return {
            "available": False,
            "reason": "workspace_no_longer_git",
            "changed_files": [],
            "ambiguous_paths": [],
        }
    repo_root = str(Path(repo["stdout"].strip()).resolve())
    baseline_root = str(Path(str(baseline.get("repo_root") or repo_root)).resolve())
    if os.path.normcase(repo_root) != os.path.normcase(baseline_root):
        return {
            "available": False,
            "reason": "workspace_repository_changed",
            "repo_root": repo_root,
            "changed_files": [],
            "ambiguous_paths": [],
            "conflicts": ["repository root changed since attempt start"],
        }

    head_result, branch_result, dirty = await asyncio.gather(
        run_git(repo_root, ["rev-parse", "--verify", "HEAD"]),
        run_git(repo_root, ["branch", "--show-current"]),
        collect_diff(repo_root, include_patch=include_patch),
    )
    current_head = head_result["stdout"].strip() if head_result["returncode"] == 0 else ""
    current_branch = branch_result["stdout"].strip() if branch_result["returncode"] == 0 else ""
    baseline_head = str(baseline.get("head") or "")
    committed_files: list[str] = []
    if baseline_head and current_head and baseline_head != current_head:
        names = await run_git(
            repo_root,
            ["diff", "--name-only", "-z", baseline_head, current_head, "--"],
        )
        if names["returncode"] == 0:
            committed_files = _dedupe_paths(names["stdout"].split("\0"))
    current_dirty = _dedupe_paths([*(dirty.get("changed_files") or []), *(dirty.get("untracked") or [])])
    all_candidates = _dedupe_paths([*committed_files, *current_dirty])
    candidates = _bounded_pathspec(all_candidates)
    truncated_paths = max(0, len(all_candidates) - len(candidates))
    baseline_dirty = {_path_key(path) for path in baseline.get("dirty_files") or []}
    lineage_owned_dirty = {
        _path_key(path) for path in baseline.get("lineage_owned_dirty_paths") or []
    }
    baseline_fingerprints = baseline.get("fingerprints") if isinstance(baseline.get("fingerprints"), dict) else {}
    fingerprint_candidates = (
        candidates
        if verify_baseline_dirty
        else []
    )
    current_fingerprints = await _fingerprints(repo_root, fingerprint_candidates)

    changed_files: list[str] = []
    ambiguous: list[str] = []
    lineage_owned: list[str] = []
    excluded_baseline: list[str] = []
    committed_keys = {_path_key(path) for path in committed_files}
    for path in candidates:
        key = _path_key(path)
        if key not in baseline_dirty:
            changed_files.append(path)
            continue
        if not verify_baseline_dirty:
            ambiguous.append(path)
            continue
        before = baseline_fingerprints.get(path) or baseline_fingerprints.get(key) or {}
        after = current_fingerprints.get(path) or {}
        if key not in committed_keys and _same_fingerprint(before, after):
            excluded_baseline.append(path)
            continue
        changed_files.append(path)
        if key in lineage_owned_dirty:
            lineage_owned.append(path)
        else:
            ambiguous.append(path)

    committed_patch = ""
    working_patch = ""
    untracked_patch = ""
    untracked_patch_omissions: list[dict[str, str]] = []
    if changed_files and include_patch:
        if baseline_head and current_head and baseline_head != current_head:
            result = await run_git(
                repo_root,
                ["diff", "--no-ext-diff", "--unified=3", baseline_head, current_head, "--", *changed_files],
            )
            if result["returncode"] == 0:
                committed_patch = result["stdout"]
        result = await run_git(
            repo_root,
            ["diff", "--no-ext-diff", "--unified=3", current_head or "HEAD", "--", *changed_files],
        )
        if result["returncode"] == 0:
            working_patch = result["stdout"]
        owned_untracked = [
            str(path)
            for path in dirty.get("untracked") or []
            if _path_key(str(path)) in {_path_key(item) for item in changed_files}
        ]
        untracked_patch, untracked_patch_omissions = await asyncio.to_thread(
            _untracked_text_patch,
            Path(repo_root),
            owned_untracked,
        )
    conflicts: list[str] = []
    if ambiguous:
        conflicts.append(
            "pre-existing dirty paths changed during the attempt"
            if verify_baseline_dirty
            else "pre-existing dirty paths were not re-hashed during live observation"
        )
    if truncated_paths:
        conflicts.append(f"Git delta path list was truncated by {truncated_paths} file(s)")
    baseline_branch = str(baseline.get("branch") or "")
    if baseline_branch and current_branch and baseline_branch != current_branch:
        conflicts.append(f"Git branch changed from {baseline_branch} to {current_branch}")
    return {
        "available": True,
        "repo_root": repo_root,
        "baseline_head": baseline_head,
        "current_head": current_head,
        "baseline_branch": baseline_branch,
        "current_branch": current_branch,
        "head_changed": bool(baseline_head and current_head and baseline_head != current_head),
        "changed_files": changed_files,
        "committed_files": committed_files,
        "working_files": current_dirty,
        "untracked": [str(path) for path in dirty.get("untracked") or [] if _path_key(str(path)) in {_path_key(item) for item in changed_files}],
        "excluded_baseline_paths": excluded_baseline,
        "ambiguous_paths": ambiguous,
        "lineage_owned_paths": lineage_owned,
        "conflicts": conflicts,
        "truncated_paths": truncated_paths,
        "patch": "\n".join(
            part.rstrip("\n")
            for part in (committed_patch, working_patch, untracked_patch)
            if part
        ).strip(),
        "committed_patch": committed_patch,
        "working_patch": working_patch,
        "untracked_patch": untracked_patch,
        "untracked_patch_omissions": untracked_patch_omissions,
        "file_facts": current_fingerprints,
    }


async def _fingerprints(repo_root: str, relative_paths: list[str]) -> dict[str, dict[str, Any]]:
    async def inspect(relative_path: str) -> tuple[str, dict[str, Any]]:
        candidate = _safe_repo_path(Path(repo_root), relative_path)
        if candidate is None:
            return relative_path, {"exists": False, "unsafe_path": True}
        return relative_path, await asyncio.to_thread(_file_fact, candidate)

    pairs = await asyncio.gather(*(inspect(path) for path in relative_paths))
    return {path: fact for path, fact in pairs}


def _file_fact(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"exists": False}
    fact: dict[str, Any] = {
        "exists": True,
        "is_file": path.is_file(),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }
    if not path.is_file():
        return fact
    digest = hashlib.sha256()
    remaining = _HASH_LIMIT_BYTES
    try:
        with path.open("rb") as handle:
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        fact["sha256"] = digest.hexdigest()
        fact["hash_complete"] = stat.st_size <= _HASH_LIMIT_BYTES
    except OSError:
        fact["hash_error"] = True
    return fact


def _untracked_text_patch(
    repo_root: Path,
    relative_paths: list[str],
) -> tuple[str, list[dict[str, str]]]:
    """Render bounded new-file patches Git itself omits for untracked files."""

    patches: list[str] = []
    omissions: list[dict[str, str]] = []
    total_bytes = 0
    for relative_path in relative_paths:
        candidate = _safe_repo_path(repo_root, relative_path)
        if candidate is None or not candidate.is_file():
            omissions.append({"path": relative_path, "reason": "unsafe_or_missing"})
            continue
        try:
            size = candidate.stat().st_size
        except OSError:
            omissions.append({"path": relative_path, "reason": "unreadable"})
            continue
        if size > _UNTRACKED_PATCH_FILE_LIMIT_BYTES:
            omissions.append({"path": relative_path, "reason": "file_too_large"})
            continue
        if total_bytes + size > _UNTRACKED_PATCH_TOTAL_LIMIT_BYTES:
            omissions.append({"path": relative_path, "reason": "total_limit"})
            continue
        try:
            raw = candidate.read_bytes()
        except OSError:
            omissions.append({"path": relative_path, "reason": "unreadable"})
            continue
        if b"\x00" in raw:
            omissions.append({"path": relative_path, "reason": "binary"})
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            omissions.append({"path": relative_path, "reason": "non_utf8"})
            continue
        total_bytes += size
        display_path = str(relative_path).replace("\\", "/")
        old_marker = "/dev/null"
        new_marker = f"b/{display_path}"
        lines = text.splitlines()
        patch_lines = [
            f"diff --git {_quoted_patch_path(f'a/{display_path}')} {_quoted_patch_path(new_marker)}",
            "new file mode 100644",
            f"--- {old_marker}",
            f"+++ {_quoted_patch_path(new_marker)}",
        ]
        if lines:
            patch_lines.append(f"@@ -0,0 +1,{len(lines)} @@")
            patch_lines.extend(f"+{line}" for line in lines)
            if text and not text.endswith(("\n", "\r")):
                patch_lines.append("\\ No newline at end of file")
        patches.append("\n".join(patch_lines))
    return "\n".join(patches), omissions


def _quoted_patch_path(value: str) -> str:
    clean = str(value or "")
    if any(character.isspace() or character in {'"', "\\"} for character in clean):
        return json.dumps(clean, ensure_ascii=False)
    return clean


def _same_fingerprint(before: Any, after: Any) -> bool:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if bool(before.get("exists")) != bool(after.get("exists")):
        return False
    if not before.get("exists"):
        return True
    before_hash = str(before.get("sha256") or "")
    after_hash = str(after.get("sha256") or "")
    if before_hash and after_hash:
        return before_hash == after_hash and before.get("size_bytes") == after.get("size_bytes")
    return (
        before.get("size_bytes") == after.get("size_bytes")
        and before.get("modified_at") == after.get("modified_at")
    )


def _safe_repo_path(repo_root: Path, relative_path: str) -> Path | None:
    try:
        root = repo_root.resolve()
        candidate = (root / str(relative_path)).resolve()
        if candidate != root and root not in candidate.parents:
            return None
        return candidate
    except (OSError, ValueError):
        return None


def _dedupe_paths(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip().replace("\\", "/")
        if not text:
            continue
        key = _path_key(text)
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _bounded_pathspec(values: list[str], *, max_files: int = 160, max_chars: int = 12_000) -> list[str]:
    output: list[str] = []
    total = 0
    for value in values:
        size = len(value) + 1
        if len(output) >= max_files or total + size > max_chars:
            break
        output.append(value)
        total += size
    return output


def _path_key(value: str) -> str:
    return os.path.normcase(str(value or "").strip().replace("\\", "/")).casefold()


def _text_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()
