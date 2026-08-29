r"""Disposable-repository acceptance matrix with the shipping Codex Provider.

This is the execution counterpart to ``probe_project_host_matrix.py``.  The
chat model, shipping parser/dispatcher/handler, Work Ledger, Codex App Server and
coding runtime are all real.  Every writable path is fenced below one fresh OS
temporary directory; the Amadeus checkout is not allowlisted and the backend
itself starts with the isolated directory as its cwd.

The matrix keeps the current project-routing conversation:

* switch to amadeus, return to Drafts, then switch-and-work;
* one one-off while amadeus remains selected;
* switch to chess, create a file, switch back, amend that chess file;
    * restart the host, reload the active session and verify its project binding;
* run an unrelated post-restart one-off.
* in a fresh Session, resolve a promoted-style ``ETERNAL_LOOP`` Project through
  the natural alias "endless game", then amend its current source even though
  two historical WorkItems both registered the same filename.

For every real provider run it checks the file delta, provider cwd, Git root
and common-dir, ledger completion assessment, and the canonical ``work.list``
projection consumed by Electron and the Slice.  The temporary repositories are
always removed.  A compact JSON report survives outside the source checkout.

Usage::

    .venv\Scripts\python.exe -X utf8 tools/probes/probe_project_provider_matrix.py
    .venv\Scripts\python.exe -X utf8 tools/probes/probe_project_provider_matrix.py --canary
    .venv\Scripts\python.exe -X utf8 tools/probes/probe_project_provider_matrix.py --history-canary
    .venv\Scripts\python.exe -X utf8 tools/probes/probe_project_provider_matrix.py --promotion-canary

Exit codes: 0 all checks passed; 1 semantic/acceptance failure; 2 infrastructure
or safety failure.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.server
import json
import ntpath
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# This process imports production modules from ROOT.  Keep those imports
# read-only even when a cache is stale or absent; the isolated backend repeats
# the same constraint through both ``-B`` and its environment.
sys.dont_write_bytecode = True

from tools.semantic_journey_evidence import build_evidence

SUCCESS_STATUSES = {"done", "succeeded", "completed"}
ACTIVE_EXECUTION = {"queued", "running"}


class SafetyViolation(RuntimeError):
    """The disposable-root invariant was violated; no more work may start."""


class InfrastructureFailure(RuntimeError):
    """The matrix could not exercise the product rather than finding a bug."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _short(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


def _path_is_within(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
            str(Path(right).resolve())
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _run_git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=check,
    )


def _git_identity(cwd: Path) -> dict[str, str]:
    result = _run_git(cwd, "rev-parse", "--show-toplevel", "--git-common-dir")
    if result.returncode:
        return {"root": "", "common_dir": "", "error": _short(result.stderr)}
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return {"root": "", "common_dir": "", "error": "git identity response was incomplete"}
    root = Path(lines[0])
    common = Path(lines[1])
    if not common.is_absolute():
        common = cwd / common
    return {
        "root": str(root.resolve()),
        "common_dir": str(common.resolve()),
        "error": "",
    }


def _repo_evidence(cwd: Path) -> dict[str, Any]:
    identity = _git_identity(cwd)
    status = _run_git(cwd, "status", "--porcelain=v1", "--untracked-files=all")
    files = sorted(
        str(path.relative_to(cwd)).replace("\\", "/")
        for path in cwd.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(cwd).parts
    )
    return {
        **identity,
        "status": status.stdout.splitlines(),
        "files": files,
    }


def _init_repo(path: Path, files: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=False)
    _run_git(path, "init", "-q", check=True)
    _run_git(path, "config", "user.name", "Amadeus disposable E2E", check=True)
    _run_git(path, "config", "user.email", "e2e@invalid.local", check=True)
    for relative, content in files.items():
        target = (path / relative).resolve()
        if not _path_is_within(target, path):
            raise SafetyViolation(f"fixture path escaped repository: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _run_git(path, "add", "--all", check=True)
    _run_git(path, "commit", "-q", "-m", "disposable fixture", check=True)


def _source_snapshot() -> dict[str, str]:
    status = _run_git(ROOT, "status", "--porcelain=v1", "--untracked-files=all")
    diff = _run_git(ROOT, "diff", "--no-ext-diff", "--binary", "HEAD", "--")
    untracked = _run_git(ROOT, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_digest = hashlib.sha256()
    for relative in sorted(path for path in untracked.stdout.split("\0") if path):
        target = (ROOT / relative).resolve()
        untracked_digest.update(relative.encode("utf-8", errors="surrogatepass"))
        untracked_digest.update(b"\0")
        if _path_is_within(target, ROOT) and target.is_file():
            untracked_digest.update(target.read_bytes())
        else:
            untracked_digest.update(b"<missing-or-escaped>")
        untracked_digest.update(b"\0")
    return {
        "head": _run_git(ROOT, "rev-parse", "HEAD").stdout.strip(),
        "status": status.stdout,
        "diff_sha256": hashlib.sha256(diff.stdout.encode("utf-8")).hexdigest(),
        "untracked_sha256": untracked_digest.hexdigest(),
    }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ledger_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    if not path.is_file():
        return {
            "work_items": [],
            "operations": [],
            "attempts": [],
            "conversation_bindings": [],
            "session_work_contexts": [],
            "artifacts": [],
            "permissions": [],
            "completions": [],
        }
    uri = f"{path.resolve().as_uri()}?mode=ro"
    deadline = time.monotonic() + 5.0
    while True:
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1.0)
            connection.row_factory = sqlite3.Row
            try:
                work_items = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT work_item_id, project_id, title, state, workspace_mode, "
                        "workspace_path, "
                        "metadata_json, created_at FROM work_items ORDER BY created_at"
                    )
                ]
                operations = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT operation_id, work_item_id, operation_number, intent, "
                        "instruction, metadata_json, created_at "
                        "FROM work_operations ORDER BY created_at"
                    )
                ]
                attempts = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT attempt_id, work_item_id, operation_id, provider, "
                        "provider_run_id, task, mode, execution_status, result, error, "
                        "metadata_json, created_at "
                        "FROM run_attempts ORDER BY created_at"
                    )
                ]
                conversation_bindings = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT session_id, project_id, anchor_work_item_id, binding_kind, "
                        "metadata_json, created_at, updated_at "
                        "FROM conversation_bindings ORDER BY created_at"
                    )
                ]
                session_work_contexts = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT session_id, active_work_item_id, metadata_json, "
                        "created_at, updated_at FROM session_work_contexts ORDER BY created_at"
                    )
                ]
                artifacts = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT artifact_id, work_item_id, attempt_id, kind, title, uri, "
                        "path, status, metadata_json "
                        "FROM artifacts ORDER BY created_at"
                    )
                ]
                permissions = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT request_id, work_item_id, attempt_id, capability, "
                        "action, status, scope_paths_json, options_json, metadata_json, "
                        "created_at, resolved_at FROM permission_requests "
                        "ORDER BY created_at"
                    )
                ]
                completions = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT assessment_id, work_item_id, attempt_id, execution_status, "
                        "completeness, attention, work_item_state, rationale, terminal "
                        "FROM completion_assessments ORDER BY created_at"
                    )
                ]
            finally:
                connection.close()
            for row in [
                *work_items,
                *operations,
                *attempts,
                *conversation_bindings,
                *session_work_contexts,
                *permissions,
            ]:
                row["metadata"] = _json_object(row.pop("metadata_json", "{}"))
            for row in permissions:
                row["scope_paths"] = list(
                    json.loads(str(row.pop("scope_paths_json", "[]") or "[]"))
                )
                row["options"] = list(
                    json.loads(str(row.pop("options_json", "[]") or "[]"))
                )
            for row in artifacts:
                metadata = _json_object(row.pop("metadata_json", "{}"))
                provider_payload = (
                    metadata.get("provider_payload")
                    if isinstance(metadata.get("provider_payload"), dict)
                    else {}
                )
                row["metadata"] = {
                    "provider_payload": {
                        key: provider_payload.get(key)
                        for key in (
                            "title",
                            "url",
                            "excerpt",
                            "summary",
                            "browserSessionId",
                            "status_code",
                        )
                        if provider_payload.get(key) not in (None, "")
                    }
                }
            return {
                "work_items": work_items,
                "operations": operations,
                "attempts": attempts,
                "conversation_bindings": conversation_bindings,
                "session_work_contexts": session_work_contexts,
                "artifacts": artifacts,
                "permissions": permissions,
                "completions": completions,
            }
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _work_projection(response: dict[str, Any]) -> dict[str, Any]:
    work = response.get("work")
    if isinstance(work, dict):
        return work
    projection = response.get("projection")
    return projection if isinstance(projection, dict) else response


def _item_by_id(snapshot: dict[str, Any], work_item_id: str) -> dict[str, Any]:
    return next(
        (
            item
            for item in snapshot.get("items", [])
            if isinstance(item, dict) and str(item.get("id") or "") == work_item_id
        ),
        {},
    )


def _provider_runs_host_settled(
    ledger: dict[str, list[dict[str, Any]]],
    projection: dict[str, Any],
    run_ids: set[str],
) -> bool:
    """Return whether Provider terminals have reached the host/UI boundary."""

    attempts = {
        str(row.get("provider_run_id") or ""): row
        for row in ledger.get("attempts", [])
        if str(row.get("provider_run_id") or "") in run_ids
    }
    if set(attempts) != run_ids:
        return False
    completed_attempts = {
        str(row.get("attempt_id") or "")
        for row in ledger.get("completions", [])
    }
    if any(
        str(row.get("attempt_id") or "") not in completed_attempts
        for row in attempts.values()
    ):
        return False
    work_item_ids = {
        str(row.get("work_item_id") or "")
        for row in attempts.values()
        if str(row.get("work_item_id") or "")
    }
    return bool(work_item_ids) and all(
        bool(_item_by_id(projection, work_id))
        and str(_item_by_id(projection, work_id).get("execution") or "")
        not in ACTIVE_EXECUTION
        and str(_item_by_id(projection, work_id).get("completion") or "")
        != "unknown"
        for work_id in work_item_ids
    )


def _artifact_fact_text(
    ledger: dict[str, list[dict[str, Any]]],
    *,
    work_item_ids: set[str],
) -> str:
    """Return bounded host-observed artifact facts without screenshot bodies."""

    facts: list[str] = []
    for row in ledger.get("artifacts", []):
        if str(row.get("work_item_id") or "") not in work_item_ids:
            continue
        facts.extend(
            str(row.get(key) or "") for key in ("title", "uri")
        )
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        payload = (
            metadata.get("provider_payload")
            if isinstance(metadata.get("provider_payload"), dict)
            else {}
        )
        facts.extend(
            str(payload.get(key) or "")
            for key in ("title", "url", "excerpt", "summary")
        )
    return "\n".join(fact for fact in facts if fact)


def _compact_ledger_row(row: dict[str, Any]) -> dict[str, Any]:
    """Retain routing/completion audit facts without provider payload bulk."""

    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    compact = {key: value for key, value in row.items() if key != "metadata"}
    if metadata:
        compact["metadata"] = {
            key: metadata.get(key)
            for key in (
                "source",
                "session_id",
                "intent",
                "focus_applied",
                "amend_inferred",
                "project_source_amend",
                "related_work_item_id",
                "continuation",
                "write_intent",
                "payload_source",
                "payload_rebase_reason",
                "provider_session",
                "provider_session_attach",
            )
            if metadata.get(key) not in (None, "", False)
        }
        git_delta = metadata.get("git_delta")
        if isinstance(git_delta, dict):
            compact["metadata"]["git_delta"] = {
                key: git_delta.get(key)
                for key in (
                    "available",
                    "repo_root",
                    "changed_files",
                    "ambiguous_paths",
                    "conflicts",
                )
            }
        provider_result = metadata.get("provider_result")
        if isinstance(provider_result, dict):
            compact["metadata"]["provider_result"] = {
                key: provider_result.get(key)
                for key in (
                    "result_type",
                    "native_run_ids",
                    "steer_revisions",
                    "session_attached",
                    "provider_session",
                )
                if provider_result.get(key) not in (None, "", [], False)
            }
    return compact


@dataclass
class Check:
    name: str
    errors: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    cascade_from: tuple[str, ...] = ()

    def require(self, condition: bool, message: str) -> None:
        if not condition:
            self.errors.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "errors": self.errors,
            "evidence": self.evidence,
            "cascade_from": list(self.cascade_from),
        }


def _runtime_error_evidence(value: Any) -> dict[str, Any]:
    """Reduce an arbitrary runtime error to bounded diagnostic facts."""

    text = str(value or "")
    if not text:
        return {}
    evidence: dict[str, Any] = {"runtime_error_present": True}
    directory = re.search(r"(?im)^\s*Directory:\s*(.+?)\s*$", text)
    if directory:
        evidence["observed_directory"] = _short(directory.group(1), 2048)
    exit_code = re.search(r"(?i)\bexit code\s*[:=]?\s*(-?\d+)", text)
    if exit_code:
        evidence["exit_code"] = int(exit_code.group(1))
    lowered = text.lower()
    if "access is denied" in lowered or "access denied" in lowered:
        evidence["runtime_error_kind"] = "access-denied"
    elif "specified module could not be found" in lowered or "module could not be found" in lowered:
        evidence["runtime_error_kind"] = "missing-module"
    elif "windows sandbox" in lowered or ".sandbox" in lowered:
        evidence["runtime_error_kind"] = "windows-sandbox"
    elif "failed to spawn" in lowered or "spawn failed" in lowered:
        evidence["runtime_error_kind"] = "spawn-failed"
    elif "exited before" in lowered or "process exited" in lowered:
        evidence["runtime_error_kind"] = "process-exited"
    elif "connection closed" in lowered or "transport closed" in lowered:
        evidence["runtime_error_kind"] = "transport-closed"
    elif "timed out" in lowered or "timeout" in lowered:
        evidence["runtime_error_kind"] = "timeout"
    elif "cannot find path" in lowered or "path not found" in lowered:
        evidence["runtime_error_kind"] = "path-not-found"
    winerror = re.search(r"(?i)(?:winerror|os error)\s*[:=]?\s*(\d+)", text)
    if winerror:
        evidence["runtime_error_code"] = int(winerror.group(1))
    return evidence


_SHELL_OPERATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("location-query", re.compile(r"(?i)\b(?:Get-Location|pwd)\b")),
    ("directory-list", re.compile(r"(?i)\b(?:Get-ChildItem|dir|ls)\b")),
    ("file-read", re.compile(r"(?i)\b(?:Get-Content|Select-String|type|cat)\b")),
    (
        "file-write",
        re.compile(
            r"(?i)\b(?:Set-Content|Add-Content|Out-File|New-Item|Remove-Item|"
            r"Move-Item|Copy-Item|apply_patch)\b"
        ),
    ),
    (
        "location-change",
        re.compile(r"(?i)\b(?:Set-Location|Push-Location|Pop-Location|cd)\b"),
    ),
    ("git", re.compile(r"(?i)(?<![\w.-])git(?:\.exe)?(?=\s|$)")),
    ("path-check", re.compile(r"(?i)\b(?:Test-Path|Resolve-Path)\b")),
)


def _shell_command_shape(value: Any, *, declared_cwd: Any = None) -> dict[str, Any]:
    """Classify a shell request without retaining model-authored command text."""

    text = str(value or "")
    if not text:
        return {}
    wrapper = re.search(
        r"(?i)(?:^|\s)-(?P<mode>EncodedCommand|Command|c)\s+",
        text,
    )
    payload = text[wrapper.end() :] if wrapper else text
    encoded = bool(wrapper and wrapper.group("mode").casefold() == "encodedcommand")
    if encoded:
        # Never decode or retain an encoded provider command in a surviving
        # report.  The wrapper fact is enough to route a local diagnostic.
        payload = ""
    shape: dict[str, Any] = {
        "operation_classes": [
            name for name, pattern in _SHELL_OPERATION_PATTERNS if pattern.search(payload)
        ],
        "has_absolute_path": bool(
            re.search(r"(?i)(?:\b[A-Z]:\\|(?<!:)\B/[^/\s])", payload)
        ),
        "references_system32": bool(re.search(r"(?i)\bSystem32\b", payload)),
    }
    if wrapper:
        shape["has_shell_wrapper"] = True
        shape["uses_encoded_command"] = encoded
    cwd = str(declared_cwd or "").strip()
    if cwd:
        normalized_payload = payload.replace("/", "\\").casefold()
        normalized_cwd = cwd.replace("/", "\\").rstrip("\\").casefold()
        shape["references_declared_cwd"] = normalized_cwd in normalized_payload
    return shape


def _provider_event_evidence(event: Any) -> dict[str, Any] | None:
    """Keep a bounded, non-secret trace of the real Provider boundary.

    The trace deliberately excludes assistant/reasoning text, raw commands, and
    raw provider envelopes.  Its job is only to distinguish "no tool call" from
    "tool call observed but lost later in the host projection" after the
    disposable Provider workspace has been removed.
    """

    method = str(getattr(event, "method", "") or "")
    params = getattr(event, "params", None)
    if method not in {"provider.event", "provider.result"} or not isinstance(params, dict):
        return None
    provider = str(params.get("provider") or "").lower()
    if provider not in {"codex", "browser", "openclaw"}:
        return None
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    event_type = str(params.get("type") or "")
    evidence: dict[str, Any] = {
        "method": method,
        "provider": provider,
        "run_id": str(params.get("run_id") or ""),
    }
    if event_type:
        evidence["type"] = event_type
    for key in (
        "status",
        "stage",
        "safe_boundary",
        "tool",
        "toolName",
        "capability",
        "reasonCode",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            evidence[key] = _short(str(value), 240)
    if payload.get("revision") is not None:
        try:
            evidence["revision"] = max(0, int(payload.get("revision") or 0))
        except (TypeError, ValueError):
            evidence["revision_valid"] = False
    for key in (
        "runtime",
        "executionProfile",
        "adapterSource",
        "policyGrantScopeBinding",
        "fallbackReason",
    ):
        value = payload.get(key)
        if value not in (None, ""):
            evidence[key] = _short(str(value), 240)
    for key in ("ok", "diagnosticOnly", "retryRequired"):
        if isinstance(payload.get(key), bool):
            evidence[key] = payload[key]
    for key in ("error", "message", "reason"):
        value = payload.get(key)
        if value not in (None, ""):
            evidence[f"{key}_present"] = True
            evidence.update(_runtime_error_evidence(value))
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    tool_input = raw.get("input") if isinstance(raw.get("input"), dict) else {}
    declared_cwd = tool_input.get("cwd")
    if declared_cwd not in (None, ""):
        evidence["declared_cwd"] = _short(str(declared_cwd), 2048)
    command_shape = _shell_command_shape(
        tool_input.get("command"),
        declared_cwd=declared_cwd,
    )
    if command_shape:
        evidence["command_shape"] = command_shape
    runtime_error = raw.get("errorText")
    if runtime_error not in (None, ""):
        evidence.update(_runtime_error_evidence(runtime_error))
    if event_type in {"assistant.delta", "reasoning.delta"}:
        evidence["text_chars"] = len(str(payload.get("text") or ""))
    if method == "provider.result":
        evidence["status"] = str(params.get("status") or "")
        if params.get("error") not in (None, ""):
            evidence["error_present"] = True
            evidence.update(_runtime_error_evidence(params.get("error")))
    return evidence


@dataclass
class Turn:
    name: str
    utterance: str
    reply: str
    new_work_item_ids: list[str]
    provider_run_ids: list[str]
    observed_providers: list[str]
    provider_cwds: list[str]
    terminal_statuses: list[str]
    provider_event_trace: list[dict[str, Any]]
    observer_decisions: list[dict[str, Any]]
    projection: dict[str, Any]
    ledger: dict[str, list[dict[str, Any]]]
    continued_provider_run_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        new_ids = set(self.new_work_item_ids)
        observed_run_ids = set(self.provider_run_ids) | set(
            self.continued_provider_run_ids
        )
        tracked_ids = new_ids | {
            str(row.get("work_item_id") or "")
            for row in self.ledger.get("attempts", [])
            if str(row.get("provider_run_id") or "") in observed_run_ids
        }
        tracked_ids.discard("")
        return {
            "name": self.name,
            "utterance": self.utterance,
            "reply": _short(self.reply, 900),
            "new_work_item_ids": self.new_work_item_ids,
            "provider_run_ids": self.provider_run_ids,
            "continued_provider_run_ids": self.continued_provider_run_ids,
            "tracked_work_item_ids": sorted(tracked_ids),
            "observed_providers": self.observed_providers,
            "provider_cwds": self.provider_cwds,
            "terminal_statuses": self.terminal_statuses,
            "provider_event_trace": self.provider_event_trace,
            "observer_decisions": self.observer_decisions,
            "destination_label": self.projection.get("destinationLabel") or "",
            "ui_items": [
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "projectId",
                        "projectName",
                        "state",
                        "execution",
                        "completion",
                        "attention",
                        "workspacePath",
                        "workspaceLabel",
                        "isScratch",
                        "artifactCount",
                        "completionRationale",
                    )
                }
                | {
                    "activity": {
                        key: (item.get("activity") or {}).get(key)
                        for key in (
                            "phase",
                            "lastEventType",
                            "toolCount",
                            "artifactCount",
                            "uncertainty",
                        )
                    }
                }
                for item in self.projection.get("items", [])
                if isinstance(item, dict) and item.get("id") in tracked_ids
            ],
            # Keep the semantic audit rows in the surviving report.  The
            # disposable SQLite database is intentionally deleted, and without
            # these rows a correct file landing can hide an execute/amend or
            # lineage error after the fact.
            "ledger_work_items": [
                _compact_ledger_row(row) for row in self.ledger.get("work_items", [])
                if str(row.get("work_item_id") or "") in tracked_ids
            ],
            "ledger_attempts": [
                _compact_ledger_row(row) for row in self.ledger.get("attempts", [])
                if str(row.get("work_item_id") or "") in tracked_ids
            ],
            "ledger_completions": [
                row for row in self.ledger.get("completions", [])
                if str(row.get("work_item_id") or "") in tracked_ids
            ],
        }


class ProjectProviderMatrix:
    def __init__(
        self,
        root: Path,
        *,
        report_dir: Path,
        chat_provider: str,
        execution_provider: str,
        chat_timeout: float,
        dispatch_timeout: float,
        provider_timeout: float,
    ) -> None:
        self.root = root.resolve()
        self.report_dir = report_dir
        self.chat_provider = chat_provider
        self.execution_provider = execution_provider.strip().lower()
        if self.execution_provider != "codex":
            raise ValueError(
                f"unsupported execution provider: {self.execution_provider}"
            )
        self.chat_timeout = chat_timeout
        self.dispatch_timeout = dispatch_timeout
        self.provider_timeout = provider_timeout
        self.isolation = self.root / "host"
        self.ledger_path = self.isolation / "work_ledger.sqlite3"
        self.main_path = self.root / "projects" / "amadeus"
        self.chess_path = self.root / "projects" / "international-chess"
        self.loop_path = self.root / "projects" / "ETERNAL_LOOP"
        self.desktop_path = self.root / "Desktop"
        self.desktop_filename = "two_player_maze.html"
        self.desktop_target = self.desktop_path / self.desktop_filename
        self.scratch_root = self.root / "drafts"
        self.fixture_token = f"AMADEUS_E2E_{uuid.uuid4().hex[:12].upper()}"
        self.focus_filename = "focus-route.txt"
        self.focus_line = f"FOCUS_OK_{self.fixture_token}"
        self.session_id = f"project-provider-e2e-{uuid.uuid4().hex}"
        self.process: subprocess.Popen[Any] | None = None
        self.log_handle: Any = None
        self.probe: Any = None
        self.port = 0
        self.checks: list[Check] = []
        self.turns: list[Turn] = []
        self.main_project_id = ""
        self.chess_project_id = ""
        self.loop_project_id = ""
        self.route_note_item_id = ""
        self.loop_filename = "two_player_maze.html"
        self.historical_loop_work_item_ids: list[str] = []
        self.desktop_export_work_item_id = ""
        self.desktop_seed_sha256 = ""

    def prepare(self, *, external_export_fixture: bool = False) -> None:
        if _path_is_within(self.root, ROOT) or _path_is_within(ROOT, self.root):
            raise SafetyViolation(
                f"temporary root must be disjoint from source checkout: {self.root}"
            )
        self.isolation.mkdir(parents=True, exist_ok=False)
        _init_repo(
            self.scratch_root,
            {
                # The host creates a separate nested Git repository per draft;
                # this parent repository only fences those disposable children.
                ".gitignore": "*\n!.gitignore\n",
            },
        )
        _init_repo(
            self.main_path,
            {"README.md": "# amadeus disposable fixture\n"},
        )
        _init_repo(
            self.chess_path,
            {"README.md": "# international chess disposable fixture\n"},
        )
        _init_repo(
            self.loop_path,
            {
                "README.md": "# ETERNAL_LOOP disposable promoted Project\n",
                self.loop_filename: (
                    "<!doctype html>\n"
                    "<html lang=\"zh-CN\">\n"
                    "<meta charset=\"utf-8\">\n"
                    "<title>双人迷宫</title>\n"
                    "<body><p id=\"status\">先获得三分的玩家获胜</p></body>\n"
                    "<script>\n"
                    "const WIN_SCORE = 3;\n"
                    "function hasWinner(score) { return score >= WIN_SCORE; }\n"
                    "globalThis.mazeGame = { WIN_SCORE, hasWinner };\n"
                    "</script>\n"
                    "</html>\n"
                ),
            },
        )
        self.desktop_path.mkdir(parents=True)
        if external_export_fixture:
            self.desktop_target.write_text(
                (
                    "<!doctype html>\n"
                    "<html lang=\"zh-CN\"><meta charset=\"utf-8\">\n"
                    "<title>桌面双人迷宫</title>\n"
                    "<body><p id=\"status\">先获得三分的玩家获胜</p>\n"
                    "<script>\n"
                    "const WIN_SCORE = 3;\n"
                    "function hasWinner(score) { return score >= WIN_SCORE; }\n"
                    "globalThis.desktopMaze = { WIN_SCORE, hasWinner };\n"
                    "</script></body></html>\n"
                ),
                encoding="utf-8",
            )
            self.desktop_seed_sha256 = hashlib.sha256(
                self.desktop_target.read_bytes()
            ).hexdigest()
        from agent_host.work_ledger_store import WorkLedgerStore

        store = WorkLedgerStore(self.ledger_path)
        try:
            self.main_project_id = store.create_or_get_project(
                self.main_path, name="amadeus"
            ).project_id
            self.chess_project_id = store.create_or_get_project(
                self.chess_path, name="international chess"
            ).project_id
            self.loop_project_id = store.create_or_get_project(
                self.loop_path,
                name="ETERNAL_LOOP",
                metadata={
                    "identity_version": 1,
                    "name_source": "generated:work_item",
                    "semantic_aliases": [
                        "endless game",
                        "endless loop game",
                        "双人迷宫游戏",
                        self.loop_filename,
                    ],
                    "promoted_fixture": True,
                },
            ).project_id
            for index, title in enumerate(
                (
                    "Build the endless game",
                    "Improve the two-player maze game",
                ),
                start=1,
            ):
                item = store.create_work_item(
                    self.loop_project_id,
                    title=title,
                    goal="Historical delivery that touched the current maze source.",
                    workspace_path=self.loop_path,
                    metadata={
                        "source": "promoted_project_history_fixture",
                        "source_user_text": title,
                    },
                )
                _, attempt = store.create_operation_attempt(
                    item.work_item_id,
                    intent="execute",
                    instruction=title,
                    provider="codex",
                    task=title,
                    provider_run_id=f"historical-loop-{index}-{uuid.uuid4().hex[:8]}",
                    operation_metadata={"source": "promoted_project_history_fixture"},
                    attempt_metadata={
                        "source": "promoted_project_history_fixture",
                        "session_id": f"historical-loop-session-{index}",
                        "source_user_text": title,
                        "intent": "execute",
                    },
                )
                store.update_attempt(
                    attempt.attempt_id,
                    execution_status="succeeded",
                    result="historical fixture completed",
                )
                store.register_artifact(
                    item.work_item_id,
                    kind="business.file",
                    title=self.loop_filename,
                    attempt_id=attempt.attempt_id,
                    path=self.loop_path / self.loop_filename,
                    metadata={
                        "relative_path": self.loop_filename,
                        "source": "promoted_project_history_fixture",
                    },
                )
                store.set_work_item_state(item.work_item_id, "review_ready")
                self.historical_loop_work_item_ids.append(item.work_item_id)
            if external_export_fixture:
                desktop_item = store.create_work_item(
                    self.loop_project_id,
                    title="Deliver the Desktop two-player maze game",
                    goal="Maintain the approved Desktop copy as one continuing goal.",
                    workspace_path=self.loop_path,
                    metadata={
                        "source": "approved_desktop_export_fixture",
                        "source_user_text": "把双人迷宫游戏交付到桌面。",
                    },
                )
                _, desktop_attempt = store.create_operation_attempt(
                    desktop_item.work_item_id,
                    intent="execute",
                    instruction="Deliver the Desktop two-player maze game",
                    provider="codex",
                    task=f"Create and export {self.desktop_filename}",
                    provider_run_id=f"historical-desktop-{uuid.uuid4().hex[:8]}",
                    operation_metadata={"source": "approved_desktop_export_fixture"},
                    attempt_metadata={
                        "source": "approved_desktop_export_fixture",
                        "session_id": "historical-desktop-session",
                        "source_user_text": "把双人迷宫游戏交付到桌面。",
                        "intent": "execute",
                    },
                )
                store.update_attempt(
                    desktop_attempt.attempt_id,
                    execution_status="succeeded",
                    result="approved Desktop fixture completed",
                )
                store.register_artifact(
                    desktop_item.work_item_id,
                    kind="business.export",
                    title=f"Exported {self.desktop_filename}",
                    attempt_id=desktop_attempt.attempt_id,
                    path=self.desktop_target,
                    status="approved",
                    sha256=self.desktop_seed_sha256,
                    size_bytes=self.desktop_target.stat().st_size,
                    modified_at=self.desktop_target.stat().st_mtime,
                    metadata={"source": "approved_desktop_export_fixture"},
                )
                store.set_work_item_state(desktop_item.work_item_id, "accepted")
                self.desktop_export_work_item_id = desktop_item.work_item_id
        finally:
            store.close()
        self._assert_fence()

    def register_execution_projects(self) -> list[dict[str, Any]]:
        """Codex consumes Host-owned workspace identity without a second registry."""

        return []

    def _assert_fence(self) -> None:
        paths = (
            self.isolation,
            self.ledger_path,
            self.main_path,
            self.chess_path,
            self.loop_path,
            self.desktop_path,
            self.scratch_root,
        )
        escaped = [str(path) for path in paths if not _path_is_within(path, self.root)]
        if escaped:
            raise SafetyViolation("isolated paths escaped temporary root: " + ", ".join(escaped))
        if _path_is_within(self.root, ROOT):
            raise SafetyViolation("temporary execution root is inside the source checkout")

    def _env(self) -> dict[str, str]:
        self._assert_fence()
        env = os.environ.copy()
        env.update(
            {
                "AMADEUS_HEADLESS": "1",
                "AMADEUS_E2E_NO_TTS": "1",
                "AMADEUS_PRE_TRANSLATION_ENABLED": "0",
                "AMADEUS_SERVER_LOG": str(self.isolation / "server.log"),
                "AMADEUS_SESSION_DIR": str(self.isolation / "sessions"),
                "AMADEUS_WORK_LEDGER_PATH": str(self.ledger_path),
                "WORK_PROJECT_ALLOWLIST": str(self.root),
                "WORK_SCRATCH_ROOT": str(self.scratch_root),
                "AMADEUS_DESKTOP_PATH": str(self.desktop_path),
                "WORK_WORKTREE_ISOLATION": "0",
                "CODEX_APP_SERVER_PROVIDER_ENABLED": "1",
                "CODEX_APP_SERVER_APPROVAL_MODE": "host",
                "DIRECT_CODEX_PROVIDER_ENABLED": "0",
                "LLM_PROVIDER": self.chat_provider,
                "DELEGATE_INTENT_ATTRIBUTE": "1",
                "DELEGATE_FOCUS_INTENT": "1",
                "DELEGATE_AMEND_INTENT": "1",
                "TASK_LOOKUP_ENABLED": "1",
                "VTS_ENABLED": "0",
                "WAKE_ENABLED": "0",
                "AEC_REALTIME_ENABLED": "0",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        for key in (
            "AMADEUS_SERVER_LOG",
            "AMADEUS_SESSION_DIR",
            "AMADEUS_WORK_LEDGER_PATH",
            "WORK_PROJECT_ALLOWLIST",
            "WORK_SCRATCH_ROOT",
            "AMADEUS_DESKTOP_PATH",
        ):
            if not _path_is_within(env[key], self.root):
                raise SafetyViolation(f"{key} escaped temporary root: {env[key]}")
        return env

    async def start(self) -> None:
        from tools.e2e_real_work_conversation import (
            WsProbe,
            _free_port,
            _wait_for_health,
        )

        self._assert_fence()
        self.port = _free_port()
        stdout_path = self.isolation / "backend.stdout.log"
        ready_log = self.isolation / "server.log"
        ready_offset = ready_log.stat().st_size if ready_log.is_file() else 0
        self.log_handle = stdout_path.open("a", encoding="utf-8", newline="\n")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-X",
                "utf8",
                "-m",
                "server.app",
                "--port",
                str(self.port),
            ],
            cwd=self.isolation,
            env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        await _wait_for_health(self.port, self.process, timeout=120.0)
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise InfrastructureFailure(
                    f"isolated backend exited during startup with code {self.process.returncode}"
                )
            if ready_log.is_file():
                text = ready_log.read_text(encoding="utf-8", errors="replace")
                if "backend server ready" in text[ready_offset:]:
                    break
            await asyncio.sleep(0.25)
        else:
            raise InfrastructureFailure("backend health passed but chat runtime never became ready")
        self.probe = WsProbe(f"ws://127.0.0.1:{self.port}/ws")
        await self.probe.__aenter__()
        provider_deadline = time.monotonic() + 120.0
        latest: dict[str, Any] = {}
        while time.monotonic() < provider_deadline:
            latest = await self.probe.request("provider.list", {})
            availability = latest.get("provider_availability")
            rows = availability if isinstance(availability, list) else []
            if any(
                isinstance(row, dict)
                and str(row.get("provider_id") or "").lower()
                == self.execution_provider
                and row.get("ready") is True
                and row.get("registered") is True
                for row in rows
            ):
                break
            await asyncio.sleep(0.25)
        else:
            raise InfrastructureFailure(
                f"{self.execution_provider} did not become ready: {latest!r}"
            )

    async def stop(self) -> None:
        from tools.e2e_real_work_conversation import _stop_server

        if self.probe is not None:
            await self.probe.__aexit__(None, None, None)
            self.probe = None
        if self.process is not None:
            await _stop_server(self.port, self.process)
            self.process = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()
        if self.probe is None:
            raise InfrastructureFailure("WebSocket probe is not connected after restart")
        loaded = await self.probe.request(
            "session.load",
            {"session_id": self.session_id},
        )
        if not bool(loaded.get("ok")):
            raise InfrastructureFailure(
                "isolated backend restarted but could not reload the active session"
            )

    async def projection(self) -> dict[str, Any]:
        if self.probe is None:
            raise InfrastructureFailure("WebSocket probe is not connected")
        return _work_projection(await self.probe.request("work.list", {}))

    async def begin_fresh_session(self, label: str) -> str:
        """Start a conversation with no inherited WorkItem or Project binding."""

        if self.probe is None:
            raise InfrastructureFailure("WebSocket probe is not connected")
        session_id = f"project-provider-{label}-{uuid.uuid4().hex}"
        created = await self.probe.request(
            "session.create",
            {"session_id": session_id, "title": f"Project Provider matrix: {label}"},
        )
        if created.get("ok") is False:
            raise InfrastructureFailure(f"could not create fresh Session: {created!r}")
        self.session_id = session_id
        return session_id

    @staticmethod
    def _openclaw_session_ids(
        ledger: dict[str, list[dict[str, Any]]],
        *,
        work_item_ids: set[str] | None = None,
    ) -> set[str]:
        session_ids: set[str] = set()
        for row in ledger.get("attempts", []):
            if work_item_ids is not None and str(row.get("work_item_id") or "") not in work_item_ids:
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            session = (
                metadata.get("provider_session")
                if isinstance(metadata.get("provider_session"), dict)
                else {}
            )
            if str(session.get("provider") or "").lower() != "openclaw":
                continue
            session_id = str(session.get("session_id") or "").strip()
            if session_id:
                session_ids.add(session_id)
        return session_ids

    async def _delete_openclaw_probe_sessions(
        self,
        session_ids: set[str],
    ) -> dict[str, dict[str, Any]]:
        """Delete only opaque OpenClaw Sessions minted by this J5 fixture."""

        if not session_ids:
            return {}
        from config.settings import OPENCLAW_BASE_URL, OPENCLAW_TOKEN
        from openclaw.gateway_client import OpenClawGatewayClient

        outcomes: dict[str, dict[str, Any]] = {}
        async with OpenClawGatewayClient(
            base_url=OPENCLAW_BASE_URL,
            token=OPENCLAW_TOKEN,
            scopes=("operator.read", "operator.write", "operator.admin"),
        ) as client:
            for session_id in sorted(session_ids):
                if not session_id.startswith("agent:main:dashboard:amadeus-"):
                    raise SafetyViolation(
                        "refusing to delete a non-Amadeus OpenClaw Session: "
                        f"{session_id}"
                    )
                payload = await client.request(
                    "sessions.delete",
                    {"key": session_id, "deleteTranscript": True},
                )
                outcomes[session_id] = (
                    dict(payload)
                    if isinstance(payload, dict)
                    else {"response_type": type(payload).__name__}
                )
        return outcomes

    async def _wait_projection(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 90.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = await self.projection()
            if predicate(last):
                return last
            await asyncio.sleep(0.25)
        return last

    async def turn(
        self,
        name: str,
        utterance: str,
        *,
        provider_expected: bool,
        expected_provider: str | None = None,
    ) -> Turn:
        if self.probe is None:
            raise InfrastructureFailure("WebSocket probe is not connected")
        self._assert_fence()
        expected_provider = (
            str(expected_provider or self.execution_provider).strip().lower()
        )
        before = _ledger_snapshot(self.ledger_path)
        before_ids = {str(row["work_item_id"]) for row in before["work_items"]}
        event_start = len(self.probe.state.events)
        turn_id = f"{name}-{uuid.uuid4().hex}"
        await self.probe.request(
            "chat.send",
            {
                "text": utterance,
                "provider": self.chat_provider,
                "session_id": self.session_id,
                "turn_id": turn_id,
                "source": "project_provider_matrix",
            },
        )
        complete = await self.probe.wait_event(
            lambda event: event.method == "chat.complete"
            and str(event.params.get("turn_id") or "") == turn_id,
            timeout=self.chat_timeout,
            after=event_start,
            description=f"{name} chat.complete",
        )

        if provider_expected:
            try:
                await self.probe.wait_event(
                    lambda event: event.method == "provider.event"
                    and str(event.params.get("provider") or "").lower()
                    == expected_provider
                    and str(event.params.get("type") or "").lower() == "run.created",
                    timeout=self.dispatch_timeout,
                    after=event_start,
                    description=f"{name} {expected_provider} run.created",
                )
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(1.0)

        created_events = [
            event
            for event in self.probe.state.events[event_start:]
            if event.method == "provider.event"
            and str(event.params.get("provider") or "").lower() == expected_provider
            and str(event.params.get("type") or "").lower() == "run.created"
        ]
        observed_providers = sorted(
            {
                str(event.params.get("provider") or "").strip().lower()
                for event in self.probe.state.events[event_start:]
                if event.method == "provider.event"
                and str(event.params.get("type") or "").lower() == "run.created"
                and str(event.params.get("provider") or "").strip()
            }
        )
        run_ids = sorted(
            {
                str(event.params.get("run_id") or "")
                for event in created_events
                if str(event.params.get("run_id") or "")
            }
        )
        provider_cwds = [
            str(
                (
                    event.params.get("payload")
                    if isinstance(event.params.get("payload"), dict)
                    else {}
                ).get("cwd")
                or ""
            )
            for event in created_events
        ]
        for cwd in provider_cwds:
            if cwd and not _path_is_within(cwd, self.root):
                raise SafetyViolation(f"provider run escaped temporary root before execution: {cwd}")

        terminal_statuses: list[str] = []
        for run_id in run_ids:
            terminal = await self.probe.wait_event(
                lambda event, wanted=run_id: event.method == "provider.result"
                and str(event.params.get("run_id") or "") == wanted,
                timeout=self.provider_timeout,
                after=event_start,
                description=f"{name} terminal {expected_provider} result",
            )
            terminal_statuses.append(str(terminal.params.get("status") or "").lower())

        after = _ledger_snapshot(self.ledger_path)
        after_ids = {str(row["work_item_id"]) for row in after["work_items"]}
        new_ids = sorted(after_ids - before_ids)
        if run_ids:
            # ``provider.result`` is the Provider boundary, not the completed
            # host boundary.  The coordinator still has to finalize artifacts,
            # discover permissions, assess completion, and publish the Slice
            # projection.  Wait for those provider-neutral ledger facts even
            # when an amendment reuses an existing WorkItem.
            def host_settled(value: dict[str, Any]) -> bool:
                return _provider_runs_host_settled(
                    _ledger_snapshot(self.ledger_path),
                    value,
                    set(run_ids),
                )

            projection = await self._wait_projection(host_settled)
        elif new_ids:
            projection = await self._wait_projection(
                lambda value: all(
                    bool(_item_by_id(value, work_id))
                    and str(_item_by_id(value, work_id).get("execution") or "")
                    not in ACTIVE_EXECUTION
                    and str(_item_by_id(value, work_id).get("completion") or "") != "unknown"
                    for work_id in new_ids
                )
            )
        else:
            projection = await self.projection()
        after = _ledger_snapshot(self.ledger_path)
        for row in after["work_items"]:
            workspace = str(row.get("workspace_path") or "")
            if workspace and not _path_is_within(workspace, self.root):
                raise SafetyViolation(f"ledger workspace escaped temporary root: {workspace}")

        provider_event_trace = [
            evidence
            for event in self.probe.state.events[event_start:]
            if (evidence := _provider_event_evidence(event)) is not None
        ]
        observer_decisions = [
            {
                key: event.params.get(key)
                for key in (
                    "source",
                    "action",
                    "terminal",
                    "speak",
                    "speech_status",
                    "display_text",
                )
                if event.params.get(key) not in (None, "")
            }
            for event in self.probe.state.events[event_start:]
            if event.method == "chat.observer_decision"
        ]

        turn = Turn(
            name=name,
            utterance=utterance,
            reply=str(complete.params.get("full_text") or ""),
            new_work_item_ids=new_ids,
            provider_run_ids=run_ids,
            observed_providers=observed_providers,
            provider_cwds=provider_cwds,
            terminal_statuses=terminal_statuses,
            provider_event_trace=provider_event_trace,
            observer_decisions=observer_decisions,
            projection=projection,
            ledger=after,
        )
        self.turns.append(turn)
        return turn

    async def _turn_inflight_amendment(
        self,
        name: str,
        utterance: str,
        *,
        provider: str,
        active_run_id: str,
    ) -> Turn:
        """Send a normal chat turn while one Provider run is active.

        The production chat/control path remains authoritative.  This helper
        only observes whether that turn was projected onto the existing outer
        run (queued, then applied) or incorrectly forked new work.
        """

        if self.probe is None:
            raise InfrastructureFailure("WebSocket probe is not connected")
        self._assert_fence()
        before = _ledger_snapshot(self.ledger_path)
        before_ids = {str(row["work_item_id"]) for row in before["work_items"]}
        event_start = len(self.probe.state.events)
        turn_id = f"{name}-{uuid.uuid4().hex}"
        await self.probe.request(
            "chat.send",
            {
                "text": utterance,
                "provider": self.chat_provider,
                "session_id": self.session_id,
                "turn_id": turn_id,
                "source": "project_provider_matrix",
            },
        )
        complete = await self.probe.wait_event(
            lambda event: event.method == "chat.complete"
            and str(event.params.get("turn_id") or "") == turn_id,
            timeout=self.chat_timeout,
            after=event_start,
            description=f"{name} chat.complete",
        )

        for stage in ("steer_queued", "steer_applied"):
            try:
                await self.probe.wait_event(
                    lambda event, wanted=stage: event.method == "provider.event"
                    and str(event.params.get("provider") or "").lower() == provider
                    and str(event.params.get("run_id") or "") == active_run_id
                    and str(event.params.get("type") or "").lower() == "run.status"
                    and str(
                        (
                            event.params.get("payload")
                            if isinstance(event.params.get("payload"), dict)
                            else {}
                        ).get("stage")
                        or ""
                    ).lower()
                    == wanted,
                    timeout=min(20.0, self.dispatch_timeout),
                    after=event_start,
                    description=f"{name} {stage}",
                )
            except TimeoutError:
                # A missing transition is a semantic assertion failure, not
                # probe infrastructure failure.  Preserve the rest of the run.
                pass

        terminal = await self.probe.wait_event(
            lambda event: event.method == "provider.result"
            and str(event.params.get("run_id") or "") == active_run_id,
            timeout=self.provider_timeout,
            after=event_start,
            description=f"{name} terminal active provider result",
        )
        await asyncio.sleep(0.25)
        created_events = [
            event
            for event in self.probe.state.events[event_start:]
            if event.method == "provider.event"
            and str(event.params.get("type") or "").lower() == "run.created"
        ]
        created_run_ids = sorted(
            {
                str(event.params.get("run_id") or "")
                for event in created_events
                if str(event.params.get("run_id") or "")
            }
        )
        terminal_statuses = [str(terminal.params.get("status") or "").lower()]
        for run_id in created_run_ids:
            if run_id == active_run_id:
                continue
            result = await self.probe.wait_event(
                lambda event, wanted=run_id: event.method == "provider.result"
                and str(event.params.get("run_id") or "") == wanted,
                timeout=self.provider_timeout,
                after=event_start,
                description=f"{name} unexpected fork terminal",
            )
            terminal_statuses.append(str(result.params.get("status") or "").lower())

        after = _ledger_snapshot(self.ledger_path)
        after_ids = {str(row["work_item_id"]) for row in after["work_items"]}
        new_ids = sorted(after_ids - before_ids)
        projection = await self.projection()
        provider_cwds = [
            str(
                (
                    event.params.get("payload")
                    if isinstance(event.params.get("payload"), dict)
                    else {}
                ).get("cwd")
                or ""
            )
            for event in created_events
        ]
        for cwd in provider_cwds:
            if cwd and not _path_is_within(cwd, self.root):
                raise SafetyViolation(
                    f"provider run escaped temporary root before execution: {cwd}"
                )
        trace = [
            evidence
            for event in self.probe.state.events[event_start:]
            if (evidence := _provider_event_evidence(event)) is not None
        ]
        observer_decisions = [
            {
                key: event.params.get(key)
                for key in (
                    "source",
                    "action",
                    "terminal",
                    "speak",
                    "speech_status",
                    "display_text",
                )
                if event.params.get(key) not in (None, "")
            }
            for event in self.probe.state.events[event_start:]
            if event.method == "chat.observer_decision"
        ]
        return Turn(
            name=name,
            utterance=utterance,
            reply=str(complete.params.get("full_text") or ""),
            new_work_item_ids=new_ids,
            provider_run_ids=created_run_ids,
            continued_provider_run_ids=[active_run_id],
            observed_providers=sorted(
                {
                    str(event.params.get("provider") or "").strip().lower()
                    for event in self.probe.state.events[event_start:]
                    if event.method in {"provider.event", "provider.result"}
                    and str(event.params.get("provider") or "").strip()
                }
            ),
            provider_cwds=provider_cwds,
            terminal_statuses=terminal_statuses,
            provider_event_trace=trace,
            observer_decisions=observer_decisions,
            projection=projection,
            ledger=after,
        )

    def checked(
        self,
        name: str,
        validate: Callable[[Check], None],
        *,
        depends_on: tuple[str, ...] = (),
    ) -> Check:
        check = Check(name)
        validate(check)
        prior = {item.name: item for item in self.checks}
        check.cascade_from = tuple(
            dependency
            for dependency in depends_on
            if dependency in prior and not prior[dependency].ok
        )
        self.checks.append(check)
        status = "PASS" if check.ok else "CASC" if check.cascade_from else "FAIL"
        print(f"  {status:4s} {name}", flush=True)
        if check.cascade_from:
            print(
                "       - downstream of: " + ", ".join(check.cascade_from),
                flush=True,
            )
        for error in check.errors:
            print(f"       - {error}", flush=True)
        return check

    def _work_row(self, turn: Turn) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if len(turn.new_work_item_ids) != 1:
            return {}, {}, {}
        work_id = turn.new_work_item_ids[0]
        item = next(
            (row for row in turn.ledger["work_items"] if row.get("work_item_id") == work_id),
            {},
        )
        attempt = next(
            (row for row in turn.ledger["attempts"] if row.get("work_item_id") == work_id),
            {},
        )
        ui = _item_by_id(turn.projection, work_id)
        return item, attempt, ui

    def _validate_real_run(
        self,
        check: Check,
        turn: Turn,
        *,
        expected_project_id: str | None = None,
        expected_workspace: Path | None = None,
        scratch: bool = False,
        expected_intent: str = "execute",
        focus_applied: bool | None = None,
        related_work_item_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        check.require(
            len(turn.provider_run_ids) == 1,
            f"expected exactly one real {self.execution_provider} run",
        )
        check.require(len(turn.new_work_item_ids) == 1, "expected exactly one new WorkItem")
        check.require(
            len(turn.terminal_statuses) == 1
            and turn.terminal_statuses[0] in SUCCESS_STATUSES,
            f"provider did not succeed: {turn.terminal_statuses}",
        )
        item, attempt, ui = self._work_row(turn)
        workspace_text = str(item.get("workspace_path") or "")
        workspace = Path(workspace_text) if workspace_text else Path()
        check.require(bool(item), "ledger WorkItem is missing")
        check.require(bool(attempt), "ledger attempt is missing")
        check.require(bool(ui), "canonical UI projection row is missing")
        check.require(_path_is_within(workspace, self.root), "workspace escaped disposable root")
        check.require(not _path_is_within(workspace, ROOT), "workspace entered source checkout")
        if expected_project_id is not None:
            check.require(
                str(item.get("project_id") or "") == expected_project_id,
                "WorkItem belongs to the wrong project",
            )
        if expected_workspace is not None:
            check.require(_same_path(workspace, expected_workspace), "WorkItem cwd is not the expected project")
        if scratch:
            check.require(_path_is_within(workspace, self.scratch_root), "one-off did not use Drafts")
            check.require(
                not _path_is_within(workspace, self.main_path)
                and not _path_is_within(workspace, self.chess_path),
                "one-off polluted a named project",
            )
        check.require(
            len(turn.provider_cwds) == 1 and _same_path(turn.provider_cwds[0], workspace),
            "provider run.created cwd disagrees with the WorkItem workspace",
        )
        identity = _git_identity(workspace) if workspace_text else {"root": "", "common_dir": "", "error": ""}
        check.require(_same_path(identity.get("root", ""), workspace), "Git top-level is not the WorkItem cwd")
        check.require(
            _same_path(identity.get("common_dir", ""), workspace / ".git"),
            "Git common-dir does not belong to the disposable repository",
        )
        check.require(
            str(attempt.get("execution_status") or "") == "succeeded",
            "ledger attempt was not marked succeeded",
        )
        attempt_metadata = attempt.get("metadata") if isinstance(attempt.get("metadata"), dict) else {}
        item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        check.require(
            str(attempt_metadata.get("intent") or item_metadata.get("intent") or "")
            == expected_intent,
            f"ledger intent is not {expected_intent}",
        )
        if focus_applied is not None:
            check.require(
                bool(attempt_metadata.get("focus_applied") or item_metadata.get("focus_applied"))
                is focus_applied,
                f"focus_applied audit flag is not {focus_applied}",
            )
        if related_work_item_id:
            check.require(
                str(attempt_metadata.get("related_work_item_id") or "") == related_work_item_id,
                "amendment lineage does not name the original WorkItem",
            )
        check.require(str(ui.get("workspacePath") or "") == workspace_text, "UI cwd differs from ledger cwd")
        check.require(str(ui.get("execution") or "") == "succeeded", "UI execution is not succeeded")
        check.require(
            str(ui.get("completion") or "") in {"partial", "complete"},
            "UI has no terminal completion assessment",
        )
        check.require(
            str(ui.get("state") or "") != "accepted",
            "provider success incorrectly auto-accepted the WorkItem",
        )
        check.require(bool(str(ui.get("completionRationale") or "").strip()), "UI completion rationale is empty")
        check.require(bool(ui.get("workspaceExists")), "UI says the disposable workspace is missing")
        check.require(int(ui.get("artifactCount") or 0) > 0, "UI exposes no business artifact")
        activity = ui.get("activity") if isinstance(ui.get("activity"), dict) else {}
        check.require(bool(activity), "UI exposes no durable activity snapshot")
        check.require(
            str(activity.get("phase") or "") == "review",
            f"UI activity phase is not review-ready: {activity.get('phase')}",
        )
        check.require(
            str(activity.get("lastEventType") or "") == "provider.result",
            f"UI activity did not retain the real result boundary: {activity.get('lastEventType')}",
        )
        check.evidence.update(
            {
                "work_item_id": item.get("work_item_id"),
                "attempt_id": attempt.get("attempt_id"),
                "provider_run_id": attempt.get("provider_run_id"),
                "provider_cwd": turn.provider_cwds[0] if turn.provider_cwds else "",
                "workspace": workspace_text,
                "git": identity,
                "ui": {
                    key: ui.get(key)
                    for key in (
                        "state",
                        "execution",
                        "completion",
                        "attention",
                        "workspacePath",
                        "workspaceLabel",
                        "isScratch",
                        "artifactCount",
                        "completionRationale",
                    )
                }
                | {
                    "activity": {
                        key: activity.get(key)
                        for key in (
                            "phase",
                            "lastEventType",
                            "toolCount",
                            "artifactCount",
                            "uncertainty",
                        )
                    }
                },
                "ledger_audit": {
                    key: attempt_metadata.get(key)
                    for key in (
                        "intent",
                        "focus_applied",
                        "amend_inferred",
                        "delegate_recovered",
                        "focus_guard",
                        "related_work_item_id",
                        "project_source_amend",
                    )
                    if attempt_metadata.get(key) not in (None, "", False)
                },
            }
        )
        return item, attempt, ui

    async def run(
        self,
        *,
        canary: bool,
        history_only: bool = False,
        promotion_only: bool = False,
        cross_domain_only: bool = False,
        external_export_only: bool = False,
    ) -> None:
        if history_only:
            await self._run_promoted_project_history_journey()
            return
        if promotion_only:
            await self._run_draft_promotion_journey()
            return
        if cross_domain_only:
            await self._run_cross_domain_journey()
            return
        if external_export_only:
            await self._run_external_export_journey()
            return
        pure = await self.turn("pure-switch-main", "切换到 amadeus 项目。", provider_expected=False)
        self.checked(
            "pure-switch-main",
            lambda check: (
                check.require(not pure.provider_run_ids, "pure focus unexpectedly started Codex"),
                check.require(not pure.new_work_item_ids, "pure focus created a WorkItem"),
                check.require(
                    str(pure.projection.get("destinationLabel") or "") == "amadeus",
                    "UI destination did not become amadeus",
                ),
            ),
        )

        drafts = await self.turn(
            "return-to-drafts",
            "回到草稿，接下来不在项目里做。",
            provider_expected=False,
        )
        self.checked(
            "return-to-drafts",
            lambda check: (
                check.require(
                    str(pure.projection.get("destinationLabel") or "") == "amadeus",
                    "return to Drafts was not exercised from an active project",
                ),
                check.require(not drafts.provider_run_ids, "return to Drafts started Codex"),
                check.require(not drafts.new_work_item_ids, "return to Drafts created a WorkItem"),
                check.require(
                    not str(drafts.projection.get("destinationLabel") or ""),
                    "UI destination did not return to Drafts",
                ),
            ),
            depends_on=("pure-switch-main",),
        )

        compound = await self.turn(
            "switch-and-work",
            (
                f"切到 amadeus，在当前项目根目录新建 {self.focus_filename}，"
                f"只写入一行 {self.focus_line}，然后读取验证，不要搜索其他目录。"
            ),
            provider_expected=True,
        )

        def validate_compound(check: Check) -> None:
            self._validate_real_run(
                check,
                compound,
                expected_project_id=self.main_project_id,
                expected_workspace=self.main_path,
                focus_applied=True,
            )
            focus_file = self.main_path / self.focus_filename
            check.require(focus_file.is_file(), "real Codex did not create the focus sentinel")
            content = (
                focus_file.read_text(encoding="utf-8", errors="replace")
                if focus_file.is_file()
                else ""
            )
            check.require(content.strip() == self.focus_line, "focus sentinel content is not exact")
            check.require(
                str(compound.projection.get("destinationLabel") or "") == "amadeus",
                "compound focus was not retained in the UI",
            )
            check.evidence["focus_sentinel"] = content
            check.evidence["repo"] = _repo_evidence(self.main_path)

        self.checked("switch-and-work", validate_compound)
        if canary:
            return

        one_off = await self.turn(
            "one-off-inside-main",
            "另外做个一次性的国际象棋游戏，不要放在任何项目里。",
            provider_expected=True,
        )

        def validate_one_off(check: Check) -> None:
            item, _, _ = self._validate_real_run(check, one_off, scratch=True)
            workspace = Path(str(item.get("workspace_path") or ""))
            repo = _repo_evidence(workspace) if workspace.is_dir() else {}
            check.require(bool(repo.get("files")), "real Codex created no one-off files")
            check.require(
                str(one_off.projection.get("destinationLabel") or "") == "amadeus",
                "one-off changed the active project",
            )
            check.evidence["repo"] = repo

        self.checked(
            "one-off-inside-main",
            validate_one_off,
            depends_on=("switch-and-work",),
        )

        ambiguous_chess = await self.turn(
            "ambiguous-chess-reference",
            "切到刚才那个象棋。",
            provider_expected=False,
        )
        self.checked(
            "ambiguous-chess-reference",
            lambda check: (
                check.require(
                    not ambiguous_chess.provider_run_ids,
                    "ambiguous reference started Codex",
                ),
                check.require(
                    not ambiguous_chess.new_work_item_ids,
                    "ambiguous reference created a WorkItem",
                ),
                check.require(
                    str(ambiguous_chess.projection.get("destinationLabel") or "")
                    == "amadeus",
                    "ambiguous reference guessed a persistent project",
                ),
            ),
            depends_on=("one-off-inside-main",),
        )

        chess_switch = await self.turn(
            "switch-to-chess",
            "切换到已注册的 “international chess” 项目，不是刚才的一次性草稿。",
            provider_expected=False,
        )
        self.checked(
            "switch-to-chess",
            lambda check: (
                check.require(not chess_switch.provider_run_ids, "chess focus started Codex"),
                check.require(not chess_switch.new_work_item_ids, "chess focus created a WorkItem"),
                check.require(
                    str(chess_switch.projection.get("destinationLabel") or "")
                    == "international chess",
                    "UI destination did not become the chess project",
                ),
            ),
            depends_on=("switch-and-work",),
        )

        create_note = await self.turn(
            "create-cross-amend-file",
            "新建 route-note.txt，写入 chess route。",
            provider_expected=True,
        )

        def validate_note(check: Check) -> None:
            item, _, _ = self._validate_real_run(
                check,
                create_note,
                expected_project_id=self.chess_project_id,
                expected_workspace=self.chess_path,
            )
            note = self.chess_path / "route-note.txt"
            check.require(note.is_file(), "real Codex did not create route-note.txt")
            content = note.read_text(encoding="utf-8", errors="replace") if note.is_file() else ""
            check.require("chess route" in content.lower(), "route-note.txt has the wrong content")
            check.require(
                str(create_note.projection.get("destinationLabel") or "")
                == "international chess",
                "cross-project work did not retain the chess destination",
            )
            self.route_note_item_id = str(item.get("work_item_id") or "")
            check.evidence["route_note_before_amend"] = content
            check.evidence["repo"] = _repo_evidence(self.chess_path)

        self.checked(
            "create-cross-amend-file",
            validate_note,
        )
        note_path = self.chess_path / "route-note.txt"
        note_before_amend = (
            note_path.read_text(encoding="utf-8", errors="replace")
            if note_path.is_file()
            else ""
        )

        back = await self.turn(
            "switch-back-main",
            "切换回 amadeus 项目。",
            provider_expected=False,
        )
        self.checked(
            "switch-back-main",
            lambda check: (
                check.require(not back.provider_run_ids, "switch back started Codex"),
                check.require(not back.new_work_item_ids, "switch back created a WorkItem"),
                check.require(
                    str(back.projection.get("destinationLabel") or "") == "amadeus",
                    "UI destination did not return to amadeus",
                ),
            ),
            depends_on=("create-cross-amend-file",),
        )

        amend = await self.turn(
            "cross-project-amend",
            "给象棋项目刚才那个 route-note.txt 加一行 reviewed。",
            provider_expected=True,
        )

        def validate_amend(check: Check) -> None:
            self._validate_real_run(
                check,
                amend,
                expected_project_id=self.chess_project_id,
                expected_workspace=self.chess_path,
                expected_intent="amend",
                related_work_item_id=self.route_note_item_id,
            )
            note = self.chess_path / "route-note.txt"
            content = (
                note.read_text(encoding="utf-8", errors="replace")
                if note.is_file()
                else ""
            )
            check.require(content != note_before_amend, "real Codex did not amend route-note.txt")
            check.require("reviewed" in content.lower(), "amendment content is missing")
            check.require(
                str(amend.projection.get("destinationLabel") or "") == "amadeus",
                "cross-project amend changed session focus",
            )
            check.evidence["route_note_after_amend"] = content
            check.evidence["repo"] = _repo_evidence(self.chess_path)

        self.checked(
            "cross-project-amend",
            validate_amend,
            depends_on=("create-cross-amend-file", "switch-back-main"),
        )

        main_before_restart_task = _repo_evidence(self.main_path)
        chess_before_restart_task = _repo_evidence(self.chess_path)
        await self.restart()
        restarted_projection = await self.projection()
        self.checked(
            "restart-restores-binding",
            lambda check: check.require(
                str(restarted_projection.get("destinationLabel") or "") == "amadeus",
                "host restart did not restore the durable conversation project",
            ),
            depends_on=("cross-project-amend",),
        )

        post_restart = await self.turn(
            "post-restart-unrelated",
            "另外做个一次性的番茄钟小工具。",
            provider_expected=True,
        )

        def validate_post_restart(check: Check) -> None:
            item, _, _ = self._validate_real_run(check, post_restart, scratch=True)
            workspace = Path(str(item.get("workspace_path") or ""))
            repo = _repo_evidence(workspace) if workspace.is_dir() else {}
            check.require(bool(repo.get("files")), "real Codex created no pomodoro files")
            check.require(
                _repo_evidence(self.main_path) == main_before_restart_task,
                "post-restart one-off changed the amadeus repository",
            )
            check.require(
                _repo_evidence(self.chess_path) == chess_before_restart_task,
                "post-restart one-off changed the chess repository",
            )
            check.evidence["repo"] = repo

        self.checked(
            "post-restart-unrelated",
            validate_post_restart,
        )
        self.checked(
            "post-restart-one-off-preserves-focus",
            lambda check: check.require(
                str(post_restart.projection.get("destinationLabel") or "") == "amadeus",
                "post-restart one-off lost the restored project binding",
            ),
            depends_on=("restart-restores-binding",),
        )
        await self._run_promoted_project_history_journey()

    async def _run_cross_domain_journey(self) -> None:
        """Cross Browser's session-local branch and return to a bound Project."""

        web_root = self.root / "browser-fixture"
        web_root.mkdir(parents=True, exist_ok=False)
        browser_token = f"BROWSER_{self.fixture_token}"
        (web_root / "index.html").write_text(
            (
                "<!doctype html><meta charset='utf-8'><title>Amadeus Browser Fixture</title>"
                f"<main><h1>Fixture home</h1><a href='/detail.html'>Open detail</a>"
                f"<p>{browser_token}</p></main>"
            ),
            encoding="utf-8",
        )
        (web_root / "detail.html").write_text(
            (
                "<!doctype html><meta charset='utf-8'><title>Fixture Detail</title>"
                f"<main><h1>Detail reached</h1><p id='result'>{browser_token}_DETAIL</p></main>"
            ),
            encoding="utf-8",
        )

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

        handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
            *args, directory=str(web_root), **kwargs
        )
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        http_thread = threading.Thread(
            target=httpd.serve_forever,
            name="amadeus-j5-http-fixture",
            daemon=True,
        )
        http_thread.start()
        base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            await self.begin_fresh_session("cross-domain")
            focused = await self.turn(
                "cross-domain-focus-project",
                "切到 amadeus 项目。",
                provider_expected=False,
            )
            self.checked(
                "cross-domain-focus-project",
                lambda check: (
                    check.require(
                        not focused.provider_run_ids,
                        "pure Project focus unexpectedly started a provider",
                    ),
                    check.require(
                        str(focused.projection.get("destinationLabel") or "")
                        == "amadeus",
                        "Session did not bind the amadeus Project",
                    ),
                ),
            )

            opened = await self.turn(
                "cross-domain-browser-open",
                (
                    f"用浏览器打开 {base_url}/index.html 看看。先别关，"
                    "我等下还要在这个页面上点东西。"
                ),
                provider_expected=True,
                expected_provider="browser",
            )
            browser_item_ids: set[str] = set()
            browser_item_ownership: dict[str, tuple[str, str, str]] = {}

            def validate_open(check: Check) -> None:
                browser_attempts = [
                    row
                    for row in opened.ledger["attempts"]
                    if str(row.get("provider") or "").lower() == "browser"
                ]
                browser_item_ids.update(
                    str(row.get("work_item_id") or "") for row in browser_attempts
                )
                browser_items = [
                    row
                    for row in opened.ledger["work_items"]
                    if str(row.get("work_item_id") or "") in browser_item_ids
                ]
                browser_item_ownership.update(
                    {
                        str(row.get("work_item_id") or ""): (
                            str(row.get("project_id") or ""),
                            str(row.get("workspace_path") or ""),
                            str(row.get("workspace_mode") or ""),
                        )
                        for row in browser_items
                        if str(row.get("work_item_id") or "")
                    }
                )
                result_text = "\n".join(
                    str(row.get("result") or "") for row in browser_attempts
                )
                artifact_text = _artifact_fact_text(
                    opened.ledger,
                    work_item_ids=browser_item_ids,
                )
                check.require(
                    len(opened.provider_run_ids) == 1,
                    "browser open did not create exactly one real Browser run "
                    f"(observed: {opened.observed_providers})",
                )
                check.require(
                    opened.terminal_statuses
                    and all(status in SUCCESS_STATUSES for status in opened.terminal_statuses),
                    "browser open did not complete successfully",
                )
                check.require(
                    len(browser_item_ids) == 1,
                    "browser open did not establish one continuable WorkItem",
                )
                check.require(
                    bool(browser_items)
                    and all(
                        str(row.get("workspace_mode") or "") == "none"
                        and not str(row.get("workspace_path") or "")
                        for row in browser_items
                    ),
                    "Browser work incorrectly acquired a filesystem Project workspace",
                )
                check.require(
                    browser_token in result_text
                    or browser_token in opened.reply
                    or browser_token in artifact_text,
                    "Browser outcome did not preserve the page's visible fixture fact",
                )
                check.require(
                    str(opened.projection.get("destinationLabel") or "") == "amadeus",
                    "Browser work mutated the Session's Project binding",
                )
                check.evidence.update(
                    {
                        "url": f"{base_url}/index.html",
                        "browser_work_item_ids": sorted(browser_item_ids),
                        "fixture_token_observed": browser_token in result_text
                        or browser_token in opened.reply
                        or browser_token in artifact_text,
                    }
                )

            self.checked(
                "cross-domain-browser-open",
                validate_open,
                depends_on=("cross-domain-focus-project",),
            )

            continued = await self.turn(
                "cross-domain-browser-continue",
                "就在刚才那个页面里，点开唯一的 Detail 链接，把那里的结果告诉我。",
                provider_expected=True,
                expected_provider="browser",
            )

            def validate_continue(check: Check) -> None:
                browser_attempts = [
                    row
                    for row in continued.ledger["attempts"]
                    if str(row.get("provider") or "").lower() == "browser"
                ]
                continued_item_ids = {
                    str(row.get("work_item_id") or "") for row in browser_attempts
                }
                result_text = "\n".join(
                    str(row.get("result") or "") for row in browser_attempts
                )
                artifact_text = _artifact_fact_text(
                    continued.ledger,
                    work_item_ids=continued_item_ids,
                )
                check.require(
                    len(continued.provider_run_ids) == 1,
                    "browser continuation did not create one follow-up Browser run",
                )
                check.require(
                    continued_item_ids == browser_item_ids and bool(browser_item_ids),
                    "browser continuation forked a second WorkItem",
                )
                check.require(
                    len(browser_attempts) >= 2,
                    "completed Browser continuation did not preserve attempt lineage",
                )
                check.require(
                    f"{browser_token}_DETAIL" in result_text
                    or f"{browser_token}_DETAIL" in continued.reply
                    or f"{browser_token}_DETAIL" in artifact_text,
                    "browser continuation did not reach the linked detail fact",
                )
                check.require(
                    str(continued.projection.get("destinationLabel") or "") == "amadeus",
                    "Browser continuation mutated the Project binding",
                )
                check.evidence.update(
                    {
                        "browser_work_item_ids": sorted(continued_item_ids),
                        "browser_attempt_ids": [
                            str(row.get("attempt_id") or "") for row in browser_attempts
                        ],
                    }
                )

            self.checked(
                "cross-domain-browser-continue",
                validate_continue,
                depends_on=("cross-domain-browser-open",),
            )

            readme_before = (self.main_path / "README.md").read_text(encoding="utf-8")
            project_token = f"PROJECT_RETURN_{self.fixture_token}"
            returned = await self.turn(
                "cross-domain-return-project-work",
                (
                    "先把网页放一边，回到 amadeus 项目。"
                    f"在 README.md 末尾加一行 `{project_token}`，再读回来确认。"
                ),
                provider_expected=True,
                expected_provider=self.execution_provider,
            )

            def validate_return(check: Check) -> None:
                item, _attempt, _ui = self._validate_real_run(
                    check,
                    returned,
                    expected_workspace=self.main_path,
                    expected_intent="amend",
                )
                readme_after = (
                    (self.main_path / "README.md").read_text(encoding="utf-8")
                    if (self.main_path / "README.md").is_file()
                    else ""
                )
                retained_browser_ownership = {
                    str(row.get("work_item_id") or ""): (
                        str(row.get("project_id") or ""),
                        str(row.get("workspace_path") or ""),
                        str(row.get("workspace_mode") or ""),
                    )
                    for row in returned.ledger["work_items"]
                    if str(row.get("work_item_id") or "") in browser_item_ids
                }
                check.require(
                    readme_after.count(project_token) == 1,
                    "return-to-Project work did not apply the exact amendment once",
                )
                check.require(
                    readme_after.startswith(readme_before),
                    "return-to-Project work replaced unrelated README content",
                )
                check.require(
                    str(returned.projection.get("destinationLabel") or "") == "amadeus",
                    "return-to-Project work lost the Session binding",
                )
                check.require(
                    retained_browser_ownership == browser_item_ownership
                    and set(retained_browser_ownership) == browser_item_ids,
                    "returning to Project rewrote Browser WorkItem ownership",
                )
                check.evidence.update(
                    {
                        "project_work_item_id": str(item.get("work_item_id") or ""),
                        "project_token_count": readme_after.count(project_token),
                        "readme_sha256": hashlib.sha256(
                            readme_after.encode("utf-8")
                        ).hexdigest(),
                    }
                )

            self.checked(
                "cross-domain-return-project-work",
                validate_return,
                depends_on=("cross-domain-browser-continue",),
            )

            old_marker = f"OPENCLAW_OLD_{self.fixture_token}"
            steer_marker = f"OPENCLAW_STEER_{self.fixture_token}"
            initial_event_start = len(self.probe.state.events) if self.probe else 0
            # This is the one deliberately synthetic utterance in J5: the wait
            # keeps a real native run open long enough to exercise mid-run
            # steering, while the marker proves the aborted terminal did not
            # leak. Ordinary routing turns above and below stay conversational.
            initial_openclaw_task = asyncio.create_task(
                self.turn(
                    "cross-domain-openclaw-start",
                    (
                        "Use OpenClaw for a separate web WorkItem outside the Project. "
                        f"Open {base_url}/index.html with its managed browser, inspect the "
                        f"visible fixture code, then perform one harmless wait of about 30 "
                        f"seconds before replying `{old_marker}`. Do not write any files or "
                        "change the current Project binding."
                    ),
                    provider_expected=True,
                    expected_provider="openclaw",
                ),
                name="j5-openclaw-active-turn",
            )
            if self.probe is None:
                raise InfrastructureFailure("WebSocket probe disconnected during J5")
            try:
                created = await self.probe.wait_event(
                    lambda event: event.method == "provider.event"
                    and str(event.params.get("provider") or "").lower() == "openclaw"
                    and str(event.params.get("type") or "").lower() == "run.created",
                    timeout=self.dispatch_timeout,
                    after=initial_event_start,
                    description="cross-domain OpenClaw run.created",
                )
            except BaseException:
                # The outer turn may still be waiting on a wrongly selected or
                # stalled Provider. Always retrieve its exception before the
                # fixture shuts down so the report owns the failure and no
                # background task races a closed Ledger.
                if not initial_openclaw_task.done():
                    initial_openclaw_task.cancel()
                await asyncio.gather(initial_openclaw_task, return_exceptions=True)
                raise
            active_openclaw_run_id = str(created.params.get("run_id") or "")
            await asyncio.sleep(4.0)
            active_ended_before_steer = initial_openclaw_task.done()
            steered: Turn | None = None
            if not active_ended_before_steer:
                steered = await self._turn_inflight_amendment(
                    "cross-domain-openclaw-active-steer",
                    (
                        "别等了，继续刚才那个 OpenClaw 任务，点开唯一的 Detail "
                        f"链接，把结果和 `{steer_marker}` 一起告诉我。"
                    ),
                    provider="openclaw",
                    active_run_id=active_openclaw_run_id,
                )
            opened_openclaw = await initial_openclaw_task
            if steered is not None:
                # ``turn`` records the outer run once it becomes terminal.  Add
                # the amendment after it so the surviving report preserves the
                # human conversation order without double-counting a run.
                self.turns.append(steered)

            openclaw_item_ids: set[str] = {
                str(row.get("work_item_id") or "")
                for row in opened_openclaw.ledger["attempts"]
                if str(row.get("provider") or "").lower() == "openclaw"
                and str(row.get("provider_run_id") or "")
                == active_openclaw_run_id
            }
            openclaw_item_ids.discard("")
            openclaw_item_ownership = {
                str(row.get("work_item_id") or ""): (
                    str(row.get("project_id") or ""),
                    str(row.get("workspace_path") or ""),
                    str(row.get("workspace_mode") or ""),
                )
                for row in opened_openclaw.ledger["work_items"]
                if str(row.get("work_item_id") or "") in openclaw_item_ids
            }

            def validate_openclaw_active_steer(check: Check) -> None:
                attempts = [
                    row
                    for row in opened_openclaw.ledger["attempts"]
                    if str(row.get("work_item_id") or "") in openclaw_item_ids
                ]
                operations = [
                    row
                    for row in opened_openclaw.ledger["operations"]
                    if str(row.get("work_item_id") or "") in openclaw_item_ids
                ]
                items = [
                    row
                    for row in opened_openclaw.ledger["work_items"]
                    if str(row.get("work_item_id") or "") in openclaw_item_ids
                ]
                result_text = "\n".join(str(row.get("result") or "") for row in attempts)
                event_trace = opened_openclaw.provider_event_trace + (
                    steered.provider_event_trace if steered is not None else []
                )
                stages = {
                    str(row.get("stage") or "")
                    for row in event_trace
                    if str(row.get("type") or "") == "run.status"
                }
                attempt_metadata = (
                    attempts[-1].get("metadata")
                    if attempts and isinstance(attempts[-1].get("metadata"), dict)
                    else {}
                )
                provider_result = (
                    attempt_metadata.get("provider_result")
                    if isinstance(attempt_metadata.get("provider_result"), dict)
                    else {}
                )
                provider_session = (
                    attempt_metadata.get("provider_session")
                    if isinstance(attempt_metadata.get("provider_session"), dict)
                    else {}
                )
                check.require(
                    not active_ended_before_steer,
                    "OpenClaw initial turn ended before the mid-run amendment",
                )
                check.require(
                    opened_openclaw.provider_run_ids == [active_openclaw_run_id],
                    "OpenClaw did not establish exactly one outer Provider run",
                )
                check.require(
                    opened_openclaw.terminal_statuses
                    and all(
                        status in SUCCESS_STATUSES
                        for status in opened_openclaw.terminal_statuses
                    ),
                    "steered OpenClaw run did not finish successfully",
                )
                check.require(
                    len(openclaw_item_ids) == 1,
                    "OpenClaw did not establish one durable WorkItem",
                )
                check.require(
                    bool(items)
                    and all(
                        str(row.get("workspace_mode") or "") == "none"
                        and not str(row.get("workspace_path") or "")
                        for row in items
                    ),
                    "OpenClaw web work incorrectly acquired a Project workspace",
                )
                check.require(
                    steered is not None
                    and not steered.new_work_item_ids
                    and not steered.provider_run_ids,
                    "mid-run OpenClaw amendment forked a WorkItem or outer run",
                )
                check.require(
                    len(attempts) == 1 and len(operations) == 1,
                    "mid-run OpenClaw amendment created a second delivery attempt",
                )
                check.require(
                    {"steer_queued", "steer_applied"}.issubset(stages),
                    "OpenClaw steer was not observed as queued then applied",
                )
                check.require(
                    list(provider_result.get("steer_revisions") or []) == [1]
                    and len(list(provider_result.get("native_run_ids") or [])) == 2,
                    "OpenClaw result does not prove one replacement native run",
                )
                check.require(
                    str(provider_session.get("provider") or "") == "openclaw"
                    and bool(str(provider_session.get("session_id") or "")),
                    "OpenClaw did not return a typed persistent Session",
                )
                check.require(
                    old_marker not in result_text,
                    "aborted OpenClaw terminal leaked into the replacement result",
                )
                check.require(
                    f"{browser_token}_DETAIL" in result_text,
                    "steered OpenClaw task did not report the detail-page fact",
                )
                check.require(
                    str(opened_openclaw.projection.get("destinationLabel") or "")
                    == "amadeus",
                    "OpenClaw work mutated the Session's Project binding",
                )
                check.evidence.update(
                    {
                        "openclaw_work_item_ids": sorted(openclaw_item_ids),
                        "outer_run_count": len(opened_openclaw.provider_run_ids),
                        "native_run_count": len(
                            list(provider_result.get("native_run_ids") or [])
                        ),
                        "steering_stages": sorted(stage for stage in stages if stage),
                        "detail_fact_observed": f"{browser_token}_DETAIL" in result_text,
                        "old_terminal_suppressed": old_marker not in result_text,
                    }
                )

            self.checked(
                "cross-domain-openclaw-active-steer",
                validate_openclaw_active_steer,
                depends_on=("cross-domain-return-project-work",),
            )

            followup_marker = f"OPENCLAW_FOLLOWUP_{self.fixture_token}"
            followed_openclaw = await self.turn(
                "cross-domain-openclaw-completed-followup",
                (
                    "继续刚才那个 OpenClaw 网页，看看现在页面上是什么，"
                    f"再带上 `{followup_marker}`。"
                ),
                provider_expected=True,
                expected_provider="openclaw",
            )

            def validate_openclaw_followup(check: Check) -> None:
                attempts = [
                    row
                    for row in followed_openclaw.ledger["attempts"]
                    if str(row.get("work_item_id") or "") in openclaw_item_ids
                    and str(row.get("provider") or "").lower() == "openclaw"
                ]
                operations = [
                    row
                    for row in followed_openclaw.ledger["operations"]
                    if str(row.get("work_item_id") or "") in openclaw_item_ids
                ]
                session_ids = self._openclaw_session_ids(
                    followed_openclaw.ledger,
                    work_item_ids=openclaw_item_ids,
                )
                latest_metadata = (
                    attempts[-1].get("metadata")
                    if attempts and isinstance(attempts[-1].get("metadata"), dict)
                    else {}
                )
                attach = (
                    latest_metadata.get("provider_session_attach")
                    if isinstance(latest_metadata.get("provider_session_attach"), dict)
                    else {}
                )
                result_text = "\n".join(str(row.get("result") or "") for row in attempts)
                check.require(
                    not followed_openclaw.new_work_item_ids,
                    "completed OpenClaw follow-up forked a second WorkItem",
                )
                check.require(
                    len(followed_openclaw.provider_run_ids) == 1,
                    "completed OpenClaw follow-up did not create one new outer run",
                )
                check.require(
                    followed_openclaw.terminal_statuses
                    and all(
                        status in SUCCESS_STATUSES
                        for status in followed_openclaw.terminal_statuses
                    ),
                    "completed OpenClaw follow-up did not succeed",
                )
                check.require(
                    len(attempts) == 2 and len(operations) == 2,
                    "completed follow-up did not create exactly one new Operation/Attempt",
                )
                check.require(
                    len(session_ids) == 1,
                    "OpenClaw follow-up did not retain exactly one Provider Session",
                )
                check.require(
                    str(attach.get("state") or "") == "attached"
                    and str(attach.get("provider") or "") == "openclaw",
                    "completed follow-up was not attached through WorkItem lineage",
                )
                check.require(
                    f"{browser_token}_DETAIL" in result_text,
                    "completed OpenClaw follow-up lost the observed page fact",
                )
                check.require(
                    str(followed_openclaw.projection.get("destinationLabel") or "")
                    == "amadeus",
                    "completed OpenClaw follow-up mutated the Project binding",
                )
                check.evidence.update(
                    {
                        "openclaw_work_item_ids": sorted(openclaw_item_ids),
                        "attempt_ids": [
                            str(row.get("attempt_id") or "") for row in attempts
                        ],
                        "operation_ids": [
                            str(row.get("operation_id") or "") for row in operations
                        ],
                        "provider_session_count": len(session_ids),
                        "attached": str(attach.get("state") or "") == "attached",
                    }
                )

            self.checked(
                "cross-domain-openclaw-completed-followup",
                validate_openclaw_followup,
                depends_on=("cross-domain-openclaw-active-steer",),
            )

            second_readme_before = (self.main_path / "README.md").read_text(
                encoding="utf-8"
            )
            second_project_token = f"PROJECT_AFTER_OPENCLAW_{self.fixture_token}"
            returned_after_openclaw = await self.turn(
                "cross-domain-openclaw-return-project-work",
                (
                    "先把 OpenClaw 放一边，回到 amadeus 项目。"
                    f"在 README.md 末尾加一行 `{second_project_token}`，"
                    "再读回来确认。"
                ),
                provider_expected=True,
                expected_provider=self.execution_provider,
            )

            def validate_return_after_openclaw(check: Check) -> None:
                item, attempt, ui = self._validate_real_run(
                    check,
                    returned_after_openclaw,
                    expected_workspace=self.main_path,
                    expected_intent="amend",
                )
                readme_after = (self.main_path / "README.md").read_text(
                    encoding="utf-8"
                )
                retained_openclaw_ownership = {
                    str(row.get("work_item_id") or ""): (
                        str(row.get("project_id") or ""),
                        str(row.get("workspace_path") or ""),
                        str(row.get("workspace_mode") or ""),
                    )
                    for row in returned_after_openclaw.ledger["work_items"]
                    if str(row.get("work_item_id") or "") in openclaw_item_ids
                }
                attempt_metadata = (
                    attempt.get("metadata")
                    if isinstance(attempt.get("metadata"), dict)
                    else {}
                )
                git_delta = (
                    attempt_metadata.get("git_delta")
                    if isinstance(attempt_metadata.get("git_delta"), dict)
                    else {}
                )
                check.require(
                    readme_after.count(second_project_token) == 1,
                    "return from OpenClaw did not apply the exact Project amendment once",
                )
                check.require(
                    readme_after.startswith(second_readme_before),
                    "return from OpenClaw replaced unrelated README content",
                )
                check.require(
                    retained_openclaw_ownership == openclaw_item_ownership
                    and set(retained_openclaw_ownership) == openclaw_item_ids,
                    "returning to Project rewrote OpenClaw WorkItem ownership",
                )
                # This is a second independent Project delivery touching the
                # same still-unaccepted path. Landing the requested line is
                # success, but claiming a clean delivery would be false.
                check.require(
                    str(ui.get("attention") or "") == "conflict"
                    and "README.md" in set(git_delta.get("ambiguous_paths") or [])
                    and bool(git_delta.get("conflicts")),
                    "overlapping unaccepted Project work did not surface a conflict",
                )
                check.require(
                    str(returned_after_openclaw.projection.get("destinationLabel") or "")
                    == "amadeus",
                    "return from OpenClaw lost the Project binding",
                )
                check.evidence.update(
                    {
                        "project_work_item_id": str(item.get("work_item_id") or ""),
                        "project_token_count": readme_after.count(second_project_token),
                        "overlap_attention": str(ui.get("attention") or ""),
                        "overlap_conflicts": list(git_delta.get("conflicts") or []),
                        "readme_sha256": hashlib.sha256(
                            readme_after.encode("utf-8")
                        ).hexdigest(),
                    }
                )

            self.checked(
                "cross-domain-openclaw-return-project-work",
                validate_return_after_openclaw,
                depends_on=("cross-domain-openclaw-completed-followup",),
            )
        finally:
            cleanup_session_ids: set[str] = set()
            cleanup_outcomes: dict[str, dict[str, Any]] = {}
            cleanup_error = ""
            if self.ledger_path.is_file():
                try:
                    cleanup_session_ids = self._openclaw_session_ids(
                        _ledger_snapshot(self.ledger_path)
                    )
                    cleanup_outcomes = await self._delete_openclaw_probe_sessions(
                        cleanup_session_ids
                    )
                except Exception as exc:
                    cleanup_error = f"{type(exc).__name__}: {exc}"
            self.checked(
                "cross-domain-openclaw-session-cleanup",
                lambda check: (
                    check.require(
                        not cleanup_error,
                        f"OpenClaw Session cleanup failed: {cleanup_error}",
                    ),
                    check.require(
                        len(cleanup_outcomes) == len(cleanup_session_ids),
                        "not every J5 OpenClaw Session received a delete response",
                    ),
                    check.evidence.update(
                        {
                            "session_count": len(cleanup_session_ids),
                            "delete_response_count": len(cleanup_outcomes),
                        }
                    ),
                ),
                depends_on=("cross-domain-openclaw-active-steer",),
            )
            httpd.shutdown()
            httpd.server_close()
            http_thread.join(timeout=5.0)

    async def _run_draft_promotion_journey(self) -> None:
        """Prove the Session-Draft/persistent-Project boundary through the host API."""

        def delivery_candidates(workspace: Path, name: str) -> list[Path]:
            if not workspace.is_dir() or not _path_is_within(
                workspace, self.scratch_root
            ):
                return []
            return sorted(
                candidate
                for candidate in workspace.rglob(name)
                if candidate.is_file()
                and _path_is_within(candidate.resolve(), workspace.resolve())
            )

        original_session = await self.begin_fresh_session("draft-promotion-source")
        timer = await self.turn(
            "promotion-create-timer-draft",
            (
                "做一个一次性的番茄钟草稿，不要放进任何已有项目。在独立草稿"
                "仓库创建 pomodoro.txt，只写一行 25-minute timer，并读取验证。"
            ),
            provider_expected=True,
        )
        timer_item: dict[str, Any] = {}
        timer_workspace = Path()
        timer_sentinel = Path()

        def validate_timer(check: Check) -> None:
            nonlocal timer_item, timer_workspace, timer_sentinel
            timer_item, _, _ = self._validate_real_run(check, timer, scratch=True)
            timer_workspace = Path(str(timer_item.get("workspace_path") or ""))
            candidates = delivery_candidates(timer_workspace, "pomodoro.txt")
            check.require(
                len(candidates) == 1,
                f"timer Draft expected one pomodoro.txt, found {len(candidates)}",
            )
            timer_sentinel = candidates[0] if len(candidates) == 1 else Path()
            content = (
                timer_sentinel.read_text(encoding="utf-8", errors="replace")
                if timer_sentinel.is_file()
                else ""
            )
            check.require(
                content.strip() == "25-minute timer",
                "timer Draft delivery content is not exactly '25-minute timer'",
            )
            check.require(
                not str(timer.projection.get("destinationLabel") or ""),
                "creating a Draft unexpectedly bound a Project",
            )
            check.evidence.update(
                {
                    "session_id": original_session,
                    "workspace": str(timer_workspace),
                    "delivery_path": str(timer_sentinel),
                    "sentinel": content,
                    "pomodoro_sha256": (
                        hashlib.sha256(timer_sentinel.read_bytes()).hexdigest()
                        if timer_sentinel.is_file()
                        else ""
                    ),
                }
            )

        self.checked("promotion-create-timer-draft", validate_timer)
        timer_item_id = str(timer_item.get("work_item_id") or "")
        timer_project_before = str(timer_item.get("project_id") or "")

        transient = await self.turn(
            "promotion-create-transient-draft",
            (
                "另外做一个不会保存成项目的一次性饮水提醒草稿。在另一个独立草稿"
                "仓库创建 water-note.txt，只写一行 drink water，并读取验证。"
            ),
            provider_expected=True,
        )
        transient_item: dict[str, Any] = {}
        transient_workspace = Path()

        def validate_transient(check: Check) -> None:
            nonlocal transient_item, transient_workspace
            transient_item, _, _ = self._validate_real_run(check, transient, scratch=True)
            transient_workspace = Path(str(transient_item.get("workspace_path") or ""))
            candidates = delivery_candidates(transient_workspace, "water-note.txt")
            check.require(
                len(candidates) == 1,
                f"transient Draft expected one water-note.txt, found {len(candidates)}",
            )
            sentinel = candidates[0] if len(candidates) == 1 else Path()
            content = (
                sentinel.read_text(encoding="utf-8", errors="replace")
                if sentinel.is_file()
                else ""
            )
            check.require(
                content.strip() == "drink water",
                "transient Draft delivery content is not exactly 'drink water'",
            )
            check.require(
                not _same_path(timer_workspace, transient_workspace),
                "two unrelated Drafts shared one workspace",
            )
            check.evidence.update(
                {
                    "workspace": str(transient_workspace),
                    "delivery_path": str(sentinel),
                }
            )

        self.checked(
            "promotion-create-transient-draft",
            validate_transient,
            depends_on=("promotion-create-timer-draft",),
        )
        transient_item_id = str(transient_item.get("work_item_id") or "")
        transient_project_before = str(transient_item.get("project_id") or "")

        if self.probe is None:
            raise InfrastructureFailure("WebSocket probe is not connected")
        promoted_response = await self.probe.request(
            "work.promote",
            {"work_item_id": timer_item_id, "surface": "electron"},
        )
        promoted = (
            promoted_response.get("promoted")
            if isinstance(promoted_response.get("promoted"), dict)
            else {}
        )
        after_promotion = _ledger_snapshot(self.ledger_path)
        promoted_item = next(
            (
                row
                for row in after_promotion["work_items"]
                if str(row.get("work_item_id") or "") == timer_item_id
            ),
            {},
        )
        transient_after = next(
            (
                row
                for row in after_promotion["work_items"]
                if str(row.get("work_item_id") or "") == transient_item_id
            ),
            {},
        )
        promoted_project_id = str(promoted.get("projectId") or "")
        promoted_name = str(promoted.get("projectName") or "")
        self.checked(
            "promotion-keeps-place-and-refiles-only-that-workspace",
            lambda check: (
                check.require(bool(promoted_project_id), "work.promote returned no Project id"),
                check.require(bool(promoted_name), "work.promote returned no Project name"),
                check.require(
                    str(promoted.get("workItemId") or "") == timer_item_id,
                    "work.promote returned a different WorkItem",
                ),
                check.require(
                    _same_path(str(promoted.get("workspacePath") or ""), timer_workspace),
                    "promotion moved or substituted the Draft workspace",
                ),
                check.require(
                    str(promoted_item.get("project_id") or "") == promoted_project_id
                    and promoted_project_id != timer_project_before,
                    "timer WorkItem was not refiled under the persistent Project",
                ),
                check.require(
                    str(transient_after.get("project_id") or "")
                    == transient_project_before,
                    "promotion incorrectly swept a different Draft workspace into the Project",
                ),
                check.require(
                    timer_sentinel.is_file(),
                    "promotion lost the real Draft artifact",
                ),
            ),
            depends_on=(
                "promotion-create-timer-draft",
                "promotion-create-transient-draft",
            ),
        )

        fresh_session = await self.begin_fresh_session("draft-promotion-fresh")
        promoted_query = await self.turn(
            "promotion-query-from-fresh-session",
            (
                f"查询我保存的 “{promoted_name}” 项目现在是什么状态。只汇报 Project 和相关"
                " WorkItem 的已有事实，不要修改、重试或新建任务。"
            ),
            provider_expected=False,
        )
        self.checked(
            "promotion-project-survives-new-session-as-read-only-reference",
            lambda check: (
                check.require(
                    fresh_session != original_session,
                    "promotion boundary was not tested from a new Session",
                ),
                check.require(
                    not promoted_query.provider_run_ids,
                    "read-only promoted Project query started Codex",
                ),
                check.require(
                    not promoted_query.new_work_item_ids,
                    "read-only promoted Project query created a WorkItem",
                ),
                check.require(
                    bool(promoted_query.reply.strip()),
                    "promoted Project query returned no user-visible answer",
                ),
                check.require(
                    not str(promoted_query.projection.get("destinationLabel") or ""),
                    "querying a Project unexpectedly bound the fresh Session",
                ),
            ),
            depends_on=("promotion-keeps-place-and-refiles-only-that-workspace",),
        )

        stale_draft = await self.turn(
            "promotion-old-draft-is-session-scoped",
            (
                "切到上一个会话里没有保存成项目的那条饮水提醒草稿。找不到就直接说明找不到，"
                "不要猜成别的项目，也不要启动任何工作。"
            ),
            provider_expected=False,
        )
        self.checked(
            "promotion-unpromoted-draft-is-not-cross-session-routeable",
            lambda check: (
                check.require(
                    not stale_draft.provider_run_ids,
                    "cross-Session Draft reference started Codex",
                ),
                check.require(
                    not stale_draft.new_work_item_ids,
                    "cross-Session Draft reference created a WorkItem",
                ),
                check.require(
                    not str(stale_draft.projection.get("destinationLabel") or ""),
                    "cross-Session Draft reference guessed a persistent Project",
                ),
            ),
            depends_on=("promotion-project-survives-new-session-as-read-only-reference",),
        )

    async def _run_promoted_project_history_journey(self) -> None:
        """Exercise Project source authority against misleading delivery history."""

        fixture = _ledger_snapshot(self.ledger_path)
        historical_ids = set(self.historical_loop_work_item_ids)
        self.checked(
            "history-fixture-models-promoted-project",
            lambda check: (
                check.require(
                    len(historical_ids) == 2,
                    "history fixture does not contain two independent deliveries",
                ),
                check.require(
                    all(
                        str(row.get("project_id") or "") == self.loop_project_id
                        and _same_path(row.get("workspace_path") or "", self.loop_path)
                        for row in fixture["work_items"]
                        if str(row.get("work_item_id") or "") in historical_ids
                    ),
                    "historical deliveries are not refiled under the persistent Project",
                ),
                check.require(
                    {
                        str(row.get("work_item_id") or "")
                        for row in fixture["artifacts"]
                        if Path(str(row.get("path") or "")).name == self.loop_filename
                    }
                    == historical_ids,
                    "both historical deliveries must index the same current filename",
                ),
                check.require(
                    (self.loop_path / self.loop_filename).is_file(),
                    "persistent Project current source is missing",
                ),
            ),
        )
        session_id = await self.begin_fresh_session("promoted-history")
        before = _ledger_snapshot(self.ledger_path)
        self.checked(
            "history-session-starts-unbound",
            lambda check: (
                check.require(
                    not any(
                        str(row.get("session_id") or "") == session_id
                        for row in before["conversation_bindings"]
                    ),
                    "fresh Session unexpectedly inherited a Project binding",
                ),
                check.require(
                    not any(
                        str(row.get("session_id") or "") == session_id
                        for row in before["session_work_contexts"]
                    ),
                    "fresh Session unexpectedly inherited a WorkItem context",
                ),
            ),
            depends_on=("history-fixture-models-promoted-project",),
        )

        switch = await self.turn(
            "history-natural-alias-switch",
            "帮我切换到 endless game 项目可以吗？",
            provider_expected=False,
        )

        def validate_switch(check: Check) -> None:
            check.require(not switch.provider_run_ids, "pure alias switch started Codex")
            check.require(not switch.new_work_item_ids, "pure alias switch created a WorkItem")
            check.require(
                str(switch.projection.get("destinationLabel") or "") == "ETERNAL_LOOP",
                "natural Project alias did not bind ETERNAL_LOOP in one user turn",
            )
            confirmations = [
                row
                for row in switch.observer_decisions
                if str(row.get("source") or "")
                in {"session_focus_result", "reference_selection"}
            ]
            check.require(
                len(confirmations) == 1,
                "successful Project switch did not publish exactly one character-lane confirmation",
            )
            if confirmations:
                check.require(
                    "ETERNAL_LOOP" in str(confirmations[0].get("display_text") or ""),
                    "Project switch confirmation did not name the selected Project",
                )
            check.evidence = {
                "role_reply": _short(switch.reply, 600),
                "observer_decisions": switch.observer_decisions,
                "destination": switch.projection.get("destinationLabel") or "",
            }

        self.checked("history-natural-alias-switch", validate_switch)

        source_path = self.loop_path / self.loop_filename
        before_text = source_path.read_text(encoding="utf-8", errors="replace")
        before_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        amend = await self.turn(
            "history-current-project-source-amend",
            (
                "把双人迷宫游戏的 two_player_maze.html 从三分获胜改成一分获胜，"
                "直接修改这个 Project 的当前版本，不要新建副本。"
            ),
            provider_expected=True,
        )

        def validate_source_amend(check: Check) -> None:
            item, attempt, _ = self._validate_real_run(
                check,
                amend,
                expected_project_id=self.loop_project_id,
                expected_workspace=self.loop_path,
                expected_intent="amend",
            )
            attempt_metadata = (
                attempt.get("metadata")
                if isinstance(attempt.get("metadata"), dict)
                else {}
            )
            item_id = str(item.get("work_item_id") or "")
            check.require(
                item_id not in set(self.historical_loop_work_item_ids),
                "current Project source amend resumed a historical delivery",
            )
            check.require(
                attempt_metadata.get("project_source_amend") is True,
                "ledger lost the Project-source amendment audit fact",
            )
            check.require(
                not str(attempt_metadata.get("related_work_item_id") or ""),
                "Project-source amendment incorrectly chose a historical WorkItem lineage",
            )
            after_text = source_path.read_text(encoding="utf-8", errors="replace")
            after_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            check.require(
                after_hash != before_hash,
                f"real {self.execution_provider} did not modify the current source file",
            )
            check.require(
                bool(re.search(r"\bWIN_SCORE\s*=\s*1\b", after_text)),
                "current source does not encode one-point victory after the amend",
            )
            check.require(
                not re.search(r"\bWIN_SCORE\s*=\s*3\b", after_text),
                "the old three-point victory rule remains active",
            )
            html_files = sorted(
                str(path.relative_to(self.loop_path)).replace("\\", "/")
                for path in self.loop_path.rglob("*.html")
            )
            check.require(
                html_files == [self.loop_filename],
                f"Provider created a duplicate HTML delivery instead of editing current source: {html_files}",
            )
            check.require(
                str(amend.projection.get("destinationLabel") or "") == "ETERNAL_LOOP",
                "Project source amend lost the selected Session destination",
            )
            check.evidence.update(
                {
                    "historical_work_item_ids": self.historical_loop_work_item_ids,
                    "new_work_item_id": item_id,
                    "source_before": _short(before_text, 500),
                    "source_after": _short(after_text, 500),
                    "source_before_sha256": before_hash,
                    "source_after_sha256": after_hash,
                    "html_files": html_files,
                }
            )

        self.checked(
            "history-current-project-source-amend",
            validate_source_amend,
            depends_on=("history-natural-alias-switch",),
        )

    async def _run_external_export_journey(self) -> None:
        """Amend one approved Desktop delivery without touching Project source."""

        session_id = await self.begin_fresh_session("external-export")
        focused = await self.turn(
            "external-export-focus-project",
            "切换到 endless game 项目。",
            provider_expected=False,
        )
        self.checked(
            "external-export-focus-project",
            lambda check: (
                check.require(not focused.provider_run_ids, "pure focus started a Provider"),
                check.require(
                    str(focused.projection.get("destinationLabel") or "")
                    == "ETERNAL_LOOP",
                    "fixture Session did not bind the parent Project",
                ),
            ),
        )

        before = _ledger_snapshot(self.ledger_path)
        before_attempt_ids = {
            str(row.get("attempt_id") or "")
            for row in before["attempts"]
            if str(row.get("work_item_id") or "")
            == self.desktop_export_work_item_id
        }
        project_source_before = (self.loop_path / self.loop_filename).read_bytes()
        amend = await self.turn(
            "external-export-owner-amend",
            (
                "桌面上那个双人迷宫游戏 two_player_maze.html，"
                "把获胜条件从三次改成一次就赢；保留其他功能并验证。"
            ),
            provider_expected=True,
        )

        after_attempts = [
            row
            for row in amend.ledger["attempts"]
            if str(row.get("work_item_id") or "")
            == self.desktop_export_work_item_id
            and str(row.get("attempt_id") or "") not in before_attempt_ids
        ]
        attempt = after_attempts[0] if len(after_attempts) == 1 else {}
        attempt_metadata = (
            attempt.get("metadata")
            if isinstance(attempt.get("metadata"), dict)
            else {}
        )
        export_plan = (
            attempt_metadata.get("export_plan")
            if isinstance(attempt_metadata.get("export_plan"), dict)
            else {}
        )
        pending_permissions = [
            row
            for row in amend.ledger.get("permissions", [])
            if str(row.get("work_item_id") or "")
            == self.desktop_export_work_item_id
            and str(row.get("attempt_id") or "")
            == str(attempt.get("attempt_id") or "")
            and str(row.get("status") or "") == "pending"
        ]
        staged_item = _item_by_id(
            amend.projection,
            self.desktop_export_work_item_id,
        )

        def validate_amend(check: Check) -> None:
            check.require(
                len(amend.provider_run_ids) == 1,
                "Desktop amendment did not start exactly one real Codex run",
            )
            check.require(
                len(amend.new_work_item_ids) == 0,
                "Desktop amendment forked a sibling WorkItem",
            )
            check.require(
                len(after_attempts) == 1,
                "Desktop amendment did not create exactly one new Attempt on its owner",
            )
            check.require(
                str(attempt.get("execution_status") or "") == "succeeded",
                "Desktop staging Attempt did not succeed",
            )
            check.require(
                str(attempt_metadata.get("intent") or "") == "amend",
                "Desktop continuation lost amend intent",
            )
            check.require(
                export_plan.get("replace_existing") is True,
                "Desktop continuation did not use the replacement transaction",
            )
            check.require(
                _same_path(
                    export_plan.get("inherited_target_path") or "",
                    self.desktop_target,
                ),
                "replacement transaction targeted the wrong external file",
            )
            check.require(
                str(export_plan.get("expected_target_sha256") or "")
                == self.desktop_seed_sha256,
                "replacement transaction did not pin the approved target hash",
            )
            check.require(
                len(pending_permissions) == 1,
                "successful staging did not expose one immutable export approval",
            )
            check.require(
                str(staged_item.get("completion") or "") == "partial"
                and str(staged_item.get("attention") or "") == "permission",
                "Slice claimed completion before the external write was approved",
            )
            check.require(
                self.desktop_target.is_file()
                and hashlib.sha256(self.desktop_target.read_bytes()).hexdigest()
                == self.desktop_seed_sha256,
                "Desktop target changed before approval",
            )
            check.require(
                (self.loop_path / self.loop_filename).read_bytes()
                == project_source_before,
                "Desktop amendment incorrectly modified same-named Project source",
            )
            check.evidence = {
                "session_id": session_id,
                "work_item_id": self.desktop_export_work_item_id,
                "attempt_id": attempt.get("attempt_id") or "",
                "provider_run_ids": amend.provider_run_ids,
                "provider_cwds": amend.provider_cwds,
                "export_plan": {
                    key: export_plan.get(key)
                    for key in (
                        "replace_existing",
                        "inherited_target_path",
                        "expected_target_sha256",
                        "inherited_from_work_item_id",
                        "requested_filename",
                    )
                },
                "permission": pending_permissions[0] if pending_permissions else {},
                "pre_approval_projection": {
                    "execution": staged_item.get("execution") or "",
                    "completion": staged_item.get("completion") or "",
                    "attention": staged_item.get("attention") or "",
                },
                "observer_decisions": amend.observer_decisions,
            }

        self.checked(
            "external-export-owner-amend",
            validate_amend,
            depends_on=("external-export-focus-project",),
        )
        if not attempt or len(pending_permissions) != 1:
            return

        if self.probe is None:
            raise InfrastructureFailure("WebSocket probe disconnected before approval")
        focus_response = await self.probe.request(
            "work.focus",
            {"work_item_id": self.desktop_export_work_item_id},
        )
        focus_projection = _work_projection(focus_response)
        selected = _item_by_id(focus_projection, self.desktop_export_work_item_id)
        request_id = str(pending_permissions[0].get("request_id") or "")
        allow_result = await self.probe.request(
            "work.permission.resolve",
            {
                "permission_request_id": request_id,
                "work_item_id": self.desktop_export_work_item_id,
                "attempt_id": str(attempt.get("attempt_id") or ""),
                "revision": str(focus_projection.get("revision") or ""),
                "decision": "allow_once",
            },
        )
        after_allow = _ledger_snapshot(self.ledger_path)
        allowed = next(
            (
                row
                for row in after_allow.get("permissions", [])
                if str(row.get("request_id") or "") == request_id
            ),
            {},
        )
        final_attempt = next(
            (
                row
                for row in after_allow["attempts"]
                if str(row.get("attempt_id") or "")
                == str(attempt.get("attempt_id") or "")
            ),
            {},
        )
        final_metadata = (
            final_attempt.get("metadata")
            if isinstance(final_attempt.get("metadata"), dict)
            else {}
        )
        export_delta = (
            final_metadata.get("export_delta")
            if isinstance(final_metadata.get("export_delta"), dict)
            else {}
        )
        target_text = (
            self.desktop_target.read_text(encoding="utf-8", errors="replace")
            if self.desktop_target.is_file()
            else ""
        )

        def validate_approval(check: Check) -> None:
            check.require(allow_result.get("ok") is not False, "allow-once RPC failed")
            check.require(
                str(selected.get("pendingPermissionRequestId") or "") == request_id,
                "Slice projection did not expose the exact pending request",
            )
            check.require(
                str(allowed.get("status") or "") == "allowed",
                "immutable export request was not marked allowed",
            )
            check.require(
                bool(re.search(r"\bWIN_SCORE\s*=\s*1\b", target_text)),
                "approved Desktop file does not contain the one-win rule",
            )
            check.require(
                hashlib.sha256(self.desktop_target.read_bytes()).hexdigest()
                != self.desktop_seed_sha256,
                "approved replacement did not change the Desktop bytes",
            )
            check.require(
                str(export_delta.get("reason") or "") == "external_export_complete",
                "Attempt diff did not close on the external export transaction",
            )
            check.require(
                not list(export_delta.get("ambiguous_paths") or []),
                "approved external diff retained ambiguous path ownership",
            )
            check.require(
                (self.loop_path / self.loop_filename).read_bytes()
                == project_source_before,
                "approval changed the Project source instead of the Desktop target",
            )
            check.evidence = {
                "permission_request_id": request_id,
                "permission_status": allowed.get("status") or "",
                "exported_paths": list(allow_result.get("exportedPaths") or []),
                "desktop_sha256": hashlib.sha256(
                    self.desktop_target.read_bytes()
                ).hexdigest(),
                "target_excerpt": _short(target_text, 700),
                "export_delta": {
                    key: export_delta.get(key)
                    for key in (
                        "reason",
                        "available",
                        "pending_export",
                        "changed_files",
                        "ambiguous_paths",
                        "conflicts",
                    )
                },
            }

        self.checked(
            "external-export-allow-once",
            validate_approval,
            depends_on=("external-export-owner-amend",),
        )


def _preflight(execution_provider: str) -> dict[str, Any]:
    if execution_provider == "codex":
        from tools.e2e_direct_codex_conversation import _sdk_preflight

        checks = _sdk_preflight()
    else:
        from tools.e2e_real_work_conversation import _preflight as real_preflight

        checks = real_preflight()
    if shutil.which("git") is None:
        raise InfrastructureFailure("git is not available")
    checks["execution_provider"] = execution_provider
    return checks


def _default_fixture_base() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        # Codex workspace-write deliberately excludes the OS temp root.  A
        # real Windows sandbox can therefore never validate a repository under
        # tempfile.gettempdir(), even though that is the right default for
        # non-sandboxed probes.
        return Path(os.environ["LOCALAPPDATA"]).resolve() / "Amadeus" / "e2e-fixtures"
    return Path(tempfile.gettempdir()).resolve()


def _remove_tree(path: Path, *, allowed_root: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    fence = allowed_root.resolve()
    if not _path_is_within(resolved, fence) or resolved == fence:
        raise SafetyViolation(f"refusing to remove path outside fixture fence: {resolved}")

    def clear_readonly(function: Callable[..., Any], target: str, _error: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    # Windows can retain a just-closed redirected stdout handle for a short
    # interval after the backend process has exited. Retry the exact fenced
    # tree; never broaden the target or treat a surviving process as success.
    deadline = time.monotonic() + 5.0
    while True:
        try:
            shutil.rmtree(resolved, onerror=clear_readonly)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def _create_run_fixture(fixture_base: Path, run_id: str) -> tuple[Path, Path]:
    """Create one disposable execution root behind an exclusive per-run fence.

    The stable ``current`` child matters: a provider that inspects its immediate
    parent can only discover repositories from this run, never a stale sibling
    batch left behind after an interrupted cleanup.
    """

    if os.name == "nt":
        # Python's tempfile.mkdtemp intentionally creates a private Windows
        # directory. Codex's unelevated sandbox account then cannot traverse
        # it, so tool processes fall back to the PowerShell installation cwd
        # and report false path/permission failures. A normal mkdir inherits
        # the already-approved fixture-base ACL while UUID keeps the run
        # exclusive without weakening product permissions.
        fence = fixture_base / f"{run_id}_{uuid.uuid4().hex[:8]}"
        fence.mkdir(parents=False, exist_ok=False)
        fence = fence.resolve()
    else:
        fence = Path(
            tempfile.mkdtemp(prefix=f"{run_id}_", dir=str(fixture_base))
        ).resolve()
    root = fence / "current"
    root.mkdir(parents=False, exist_ok=False)
    if (
        not _path_is_within(fence, fixture_base)
        or not _path_is_within(root, fence)
        or _path_is_within(fence, ROOT)
    ):
        raise SafetyViolation(f"disposable run fence escaped fixture boundary: {fence}")
    return fence, root


_SANDBOX_LOG_RECORD = re.compile(
    r"\[(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+"
    r"(?P<process>[^\]]+)\]\s*(?P<message>.*?)"
    r"(?=\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+[^\]]+\]|\Z)",
    re.DOTALL,
)


def _sandbox_log_events(text: str) -> list[dict[str, Any]]:
    """Extract lifecycle metadata without retaining provider command bodies."""

    events: list[dict[str, Any]] = []
    for match in _SANDBOX_LOG_RECORD.finditer(text):
        message = " ".join(match.group("message").split())
        event: dict[str, Any] = {
            "timestamp": match.group("timestamp"),
            "process": ntpath.basename(match.group("process")),
        }
        lifecycle = re.match(r"(?i)^(START|SUCCESS|FAILURE):\s*(.*)$", message)
        if lifecycle:
            event["kind"] = lifecycle.group(1).lower()
            command = lifecycle.group(2)
            executable = re.match(r'^"?(.+?\.exe)"?(?:\s|$)', command, re.IGNORECASE)
            if executable:
                event["executable"] = ntpath.basename(executable.group(1))
            exit_code = re.search(r"(?i)\(exit code\s+(-?\d+)\)\s*$", command)
            if exit_code:
                event["exit_code"] = int(exit_code.group(1))
        else:
            lowered = message.lower()
            if "sandbox setup required" in lowered:
                event["kind"] = "setup-required"
            elif "setup refresh" in lowered:
                event["kind"] = "setup-refresh"
            elif "windows sandbox failed" in lowered:
                event["kind"] = "sandbox-failure"
            else:
                event["kind"] = "diagnostic"
            error_code = re.search(
                r"(?i)\b((?:[a-z]+_){1,}[a-z]+|os error\s+\d+|error\s+\d+)\b",
                message,
            )
            if error_code:
                event["error_code"] = error_code.group(1).lower()
        events.append(event)
    return events


async def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    preflight = _preflight(args.execution_provider)
    fixture_base = (
        Path(args.fixture_base).resolve()
        if args.fixture_base
        else _default_fixture_base()
    )
    if _path_is_within(fixture_base, ROOT):
        raise SafetyViolation(
            f"fixture base must be outside source checkout: {fixture_base}"
        )
    fixture_base.mkdir(parents=True, exist_ok=True)
    report_dir = (
        Path(args.report_dir).resolve()
        if args.report_dir
        else Path(tempfile.gettempdir()).resolve() / "amadeus_project_provider_reports"
    )
    if _path_is_within(report_dir, ROOT):
        raise SafetyViolation(f"report directory must be outside source checkout: {report_dir}")
    report_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"project_provider_{_utc_stamp()}_{uuid.uuid4().hex[:6]}"
    report_path = report_dir / f"{run_id}.json"
    run_fence, root = _create_run_fixture(fixture_base, run_id)
    source_before = _source_snapshot()
    report: dict[str, Any] = {
        "schema": "amadeus.project-provider-matrix.v1",
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": (
            "history-canary"
            if args.history_canary
            else "promotion-canary"
            if args.promotion_canary
            else "cross-domain-canary"
            if args.cross_domain_canary
            else "external-export-canary"
            if args.external_export_canary
            else "canary"
            if args.canary
            else "full"
        ),
        "preflight": preflight,
        "paths": {
            "fixture_base": str(fixture_base),
            "run_fence": str(run_fence),
            "temporary_root": str(root),
            "report": str(report_path),
        },
        "safety": {
            "run_fence_inside_fixture_base": _path_is_within(run_fence, fixture_base),
            "temporary_root_inside_run_fence": _path_is_within(root, run_fence),
            "temporary_root_inside_fixture_base": _path_is_within(root, fixture_base),
            "temporary_root_outside_source": not _path_is_within(root, ROOT),
            "source_before": source_before,
        },
        "checks": [],
        "turns": [],
    }
    matrix = ProjectProviderMatrix(
        root,
        report_dir=report_dir,
        chat_provider=args.provider,
        execution_provider=args.execution_provider,
        chat_timeout=args.chat_timeout,
        dispatch_timeout=args.dispatch_timeout,
        provider_timeout=args.provider_timeout,
    )
    exit_code = 2
    try:
        matrix.prepare(external_export_fixture=args.external_export_canary)
        report["execution_provider_projects"] = matrix.register_execution_projects()
        await matrix.start()
        await matrix.run(
            canary=args.canary,
            history_only=args.history_canary,
            promotion_only=args.promotion_canary,
            cross_domain_only=args.cross_domain_canary,
            external_export_only=args.external_export_canary,
        )
        # Include shutdown in the source-integrity window.  A process that
        # writes during teardown is no safer than one that writes mid-run.
        await matrix.stop()
        source_after = _source_snapshot()
        source_check = matrix.checked(
            "source-checkout-untouched",
            lambda check: check.require(
                source_after == source_before,
                "source checkout changed while a real Provider was running",
            ),
        )
        source_check.evidence = {"before": source_before, "after": source_after}
        report["status"] = "passed" if all(check.ok for check in matrix.checks) else "failed"
        exit_code = 0 if report["status"] == "passed" else 1
    except (SafetyViolation, InfrastructureFailure) as exc:
        report["status"] = "infrastructure_or_safety_error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        matrix.checked(
            "journey-runtime-completed",
            lambda check: check.require(False, report["error"]),
        )
    except Exception as exc:
        report["status"] = "error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        matrix.checked(
            "journey-runtime-completed",
            lambda check: check.require(False, report["error"]),
        )
    finally:
        try:
            await matrix.stop()
        except Exception as exc:
            report.setdefault("cleanup_errors", []).append(f"stop: {type(exc).__name__}: {exc}")
            exit_code = 2
        log_path = matrix.isolation / "server.log"
        stdout_path = matrix.isolation / "backend.stdout.log"
        report["diagnostics"] = {
            "server_log_tail": _short(
                log_path.read_text(encoding="utf-8", errors="replace")[-12000:]
                if log_path.is_file()
                else "",
                12000,
            ),
            "backend_stdout_tail": _short(
                stdout_path.read_text(encoding="utf-8", errors="replace")[-8000:]
                if stdout_path.is_file()
                else "",
                8000,
            ),
        }
        try:
            _remove_tree(run_fence, allowed_root=fixture_base)
            report["cleanup"] = {
                "run_fence_removed": not run_fence.exists(),
                "temporary_root_removed": not root.exists(),
                "recoverable": False,
            }
        except Exception as exc:
            report["cleanup"] = {
                "run_fence_removed": not run_fence.exists(),
                "temporary_root_removed": not root.exists(),
                "recoverable": True,
                "recoverable_path": str(run_fence),
                "error": f"{type(exc).__name__}: {exc}",
            }
            exit_code = 2
        # Serialize evidence only after cleanup.  A report bug must never leave
        # writable repositories behind, which the 2026-08-04 second full run
        # demonstrated before this ordering was enforced.
        report["checks"] = [check.to_dict() for check in matrix.checks]
        report["turns"] = [turn.to_dict() for turn in matrix.turns]
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["summary"] = {
            "checks_passed": sum(check.ok for check in matrix.checks),
            "checks_total": len(matrix.checks),
            "root_failures": sum(
                not check.ok and not check.cascade_from for check in matrix.checks
            ),
            "cascaded_failures": sum(bool(check.cascade_from) for check in matrix.checks),
            "real_provider_runs": sum(
                len(turn.provider_run_ids) for turn in matrix.turns
            ),
            "model_turns": len(matrix.turns),
        }
        journey_id = (
            "J1"
            if args.history_canary
            else "J2"
            if args.promotion_canary
            else "J5"
            if args.cross_domain_canary
            else ""
        )
        if journey_id and report.get("status") in {"passed", "failed"} and matrix.checks:
            check_by_name = {check.name: check for check in matrix.checks}
            if journey_id == "J1":
                artifact_hashes = {
                    "two_player_maze.html": str(
                        check_by_name.get("history-current-project-source-amend", Check("missing"))
                        .evidence.get("source_after_sha256")
                        or ""
                    )
                }
            elif journey_id == "J2":
                artifact_hashes = {
                    "pomodoro.txt": str(
                        check_by_name.get("promotion-create-timer-draft", Check("missing"))
                        .evidence.get("pomodoro_sha256")
                        or ""
                    )
                }
            else:
                artifact_hashes = {
                    "browser-return/README.md": str(
                        check_by_name.get(
                            "cross-domain-return-project-work", Check("missing")
                        ).evidence.get("readme_sha256")
                        or ""
                    ),
                    "openclaw-return/README.md": str(
                        check_by_name.get(
                            "cross-domain-openclaw-return-project-work",
                            Check("missing"),
                        ).evidence.get("readme_sha256")
                        or ""
                    ),
                }
            latest_ledger = matrix.turns[-1].ledger if matrix.turns else {}
            try:
                report["semantic_evidence"] = build_evidence(
                    root=ROOT,
                    journey_id=journey_id,
                    status=str(report["status"]),
                    test_level="L3",
                    provider=(
                        f"browser+openclaw+{matrix.execution_provider}"
                        if journey_id == "J5"
                        else matrix.execution_provider
                    ),
                    model=str(args.provider),
                    report_path=report_path,
                    isolation_root=root,
                    checks=report["checks"],
                    started_at=str(report["started_at"]),
                    finished_at=str(report["finished_at"]),
                    artifact_hashes={
                        key: value for key, value in artifact_hashes.items() if value
                    },
                    ledger_ids={
                        "work_item_ids": [
                            str(row.get("work_item_id") or "")
                            for row in latest_ledger.get("work_items", [])
                        ],
                        "attempt_ids": [
                            str(row.get("attempt_id") or "")
                            for row in latest_ledger.get("attempts", [])
                        ],
                    },
                    manual_acceptance="pending",
                    notes=(
                        "Electron layout, card click feel, and spoken Japanese remain L4.",
                    ),
                )
            except Exception as exc:
                report["status"] = "evidence_error"
                report["evidence_error"] = f"{type(exc).__name__}: {exc}"
                exit_code = 2
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "deepseek"))
    parser.add_argument(
        "--execution-provider",
        choices=("codex",),
        default="codex",
        help="shipping coding Provider under test",
    )
    parser.add_argument("--chat-timeout", type=float, default=180.0)
    parser.add_argument(
        "--dispatch-timeout",
        type=float,
        default=30.0,
        help="seconds to wait for provider run.created after chat completion",
    )
    parser.add_argument("--provider-timeout", type=float, default=900.0)
    parser.add_argument("--report-dir", default="")
    parser.add_argument(
        "--fixture-base",
        default=os.environ.get("AMADEUS_E2E_FIXTURE_BASE", ""),
        help=(
            "parent directory for disposable repositories; Windows defaults "
            "outside the OS temp root so Codex workspace-write can access it"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--canary",
        action="store_true",
        help="run through the first real switch-and-work only",
    )
    mode.add_argument(
        "--history-canary",
        action="store_true",
        help=(
            "run only the fresh-Session alias switch and promoted-Project "
            "current-source amendment"
        ),
    )
    mode.add_argument(
        "--promotion-canary",
        action="store_true",
        help=(
            "run only the Session Draft, Keep as Project, and fresh-Session "
            "lookup boundary"
        ),
    )
    mode.add_argument(
        "--cross-domain-canary",
        action="store_true",
        help=(
            "run only a real Browser open/continue branch followed by a "
            "return to bound-Project Codex work"
        ),
    )
    mode.add_argument(
        "--external-export-canary",
        action="store_true",
        help=(
            "run only the approved Desktop WorkItem amendment, immutable "
            "allow-once export, Attempt diff, and same-named Project-source boundary"
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        exit_code, report = asyncio.run(_run(args))
    except Exception as exc:
        # Failures before the disposable root/report can be constructed are
        # infrastructure or safety failures, not routing evidence.
        exit_code = 2
        report = {
            "status": "infrastructure_or_safety_error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(
        json.dumps(
            {
                "status": report.get("status"),
                "report": report.get("paths", {}).get("report"),
                "summary": report.get("summary", {}),
                "cleanup": report.get("cleanup", {}),
                "error": report.get("error", ""),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
