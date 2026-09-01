"""Provider-neutral projection of execution events into user-facing progress facts.

Provider events are deliberately mechanical: a healthy coding run can emit
dozens of tool records without saying anything a user should hear.  This module
recognises only bounded facts the host can defend from the canonical event
contract.  It never infers goal completion and never exposes model reasoning or
raw command output.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Literal

from agent_host.provider_progress import valid_progress_milestone


_VALIDATION_COMMAND_RE = re.compile(
    r"(?:"
    r"(?:^|[;&|]\s*)(?:python(?:\.exe)?\s+-m\s+)?(?:pytest|unittest)\b"
    r"|(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|check|lint|build|typecheck)\b"
    r"|(?:^|[;&|]\s*)(?:cargo|go|dotnet|mvnw?|gradlew?)\s+(?:test|check|build)\b"
    r"|(?:^|[;&|]\s*)(?:ruff|eslint|mypy|pyright|tsc)\b"
    r")",
    re.IGNORECASE,
)
_FILE_TOOLS = frozenset({"write", "edit", "multiedit", "file_change", "apply_patch"})
_RUNTIME_ARTIFACT_ROLES = frozenset({"request", "events", "result", "manifest", "runtime"})
_MAX_SUMMARY = 240
_MAX_TOOL_CONTEXTS = 12


@dataclass(frozen=True, slots=True)
class SemanticProgressFact:
    """One bounded semantic signal with an explicit evidence strength."""

    key: str
    summary: str
    source: str
    explicit: bool = False
    verified: bool = True
    milestone: str = ""
    evidence: Literal["candidate", "reported", "observed"] = "observed"


def remember_tool_call(
    current: dict[str, Any] | None,
    payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return a bounded copy of active tool contexts including ``payload``."""

    contexts = {
        str(key): dict(value)
        for key, value in (current or {}).items()
        if str(key) and isinstance(value, dict)
    }
    key = tool_identity(payload)
    if key:
        contexts[key] = _bounded_tool_context(payload)
    while len(contexts) > _MAX_TOOL_CONTEXTS:
        contexts.pop(next(iter(contexts)))
    return contexts


def consume_tool_call(
    current: dict[str, Any] | None,
    payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return ``(remaining, matching_context)`` for one tool result."""

    contexts = {
        str(key): dict(value)
        for key, value in (current or {}).items()
        if str(key) and isinstance(value, dict)
    }
    key = tool_identity(payload)
    context = contexts.pop(key, {}) if key else {}
    if not context:
        tool = _tool_name(payload)
        for candidate_key in reversed(list(contexts)):
            candidate = contexts[candidate_key]
            if tool and _tool_name(candidate) == tool:
                context = contexts.pop(candidate_key)
                break
    return contexts, context


def semantic_progress_fact(
    event_type: str,
    payload: dict[str, Any] | None,
    *,
    tool_context: dict[str, Any] | None = None,
) -> SemanticProgressFact | None:
    """Project a canonical event into a conservative semantic progress fact."""

    kind = str(event_type or "").strip().lower()
    source = payload if isinstance(payload, dict) else {}
    context = tool_context if isinstance(tool_context, dict) else {}

    if kind == "semantic.progress":
        summary = _summary(source.get("summary") or source.get("text"))
        milestone = valid_progress_milestone(source.get("milestone"))
        verified = source.get("verified") is True
        return _fact(
            f"provider-{milestone or 'progress'}",
            summary,
            str(source.get("source") or "provider"),
            explicit=source.get("explicit") is not False,
            verified=verified,
            milestone=milestone,
            evidence="observed" if verified else "reported",
        )

    if kind == "assistant.update":
        summary = _summary(source.get("text") or source.get("summary"))
        return _fact(
            "provider-update",
            summary,
            str(source.get("source") or "provider_assistant_update"),
            explicit=False,
            verified=False,
            evidence="candidate",
        )

    if kind in {"permission.requested", "permission.required"} and _diagnostic_only(source):
        operation = _tool_name(source) or _compact(
            source.get("action") or source.get("capability") or "provider action",
            80,
        )
        summary = (
            f"Provider policy blocked {operation}; this run cannot approve that operation "
            "in place and may continue with a narrower alternative."
        )
        return _fact(
            "permission-blocked",
            summary,
            "host.permission_diagnostic",
            identity={
                "capability": source.get("capability"),
                "action": source.get("action"),
                "scope": source.get("scope"),
                "reason": source.get("reason"),
            },
        )

    if kind in {"permission.requested", "permission.required"}:
        operation = _tool_name(source) or _compact(
            source.get("action") or source.get("capability") or "continue",
            80,
        )
        return _fact(
            "permission-pending",
            f"Provider is waiting for permission to {operation}.",
            "host.permission_request",
            identity={
                "id": source.get("id")
                or source.get("request_id")
                or source.get("requestId"),
                "capability": source.get("capability"),
                "action": source.get("action"),
                "scope": source.get("scope"),
            },
        )

    if kind == "tool.call":
        command = _command(source) or _command(context)
        if command and _is_validation_command(command):
            return _fact(
                "validation-started",
                "Project validation started.",
                "host.tool_observation",
                identity={"tool_id": tool_identity(source), "command_class": "validation"},
                milestone="validation",
            )
        return None

    if kind == "tool.result":
        merged = {**context, **source}
        tool = _tool_name(merged)
        ok = _tool_succeeded(merged)
        command = _command(merged)
        if command and _is_validation_command(command):
            return _fact(
                "validation-finished",
                (
                    "Project validation passed."
                    if ok
                    else "Project validation failed; the provider is inspecting the result."
                ),
                "host.tool_observation",
                identity={
                    "tool_id": tool_identity(merged),
                    "ok": ok,
                    "exit_code": merged.get("exit_code"),
                },
                milestone="validation",
            )
        if tool in _FILE_TOOLS and ok:
            files = _changed_files(merged)
            if files:
                return _fact(
                    "files-updated",
                    _file_summary(files),
                    "host.tool_observation",
                    identity={"files": files, "tool_id": tool_identity(merged)},
                )
        return None

    if kind == "artifact.created":
        role = str(source.get("role") or "").strip().lower()
        artifact_type = str(
            source.get("artifact_type") or source.get("type") or source.get("kind") or ""
        ).strip().lower()
        if role in _RUNTIME_ARTIFACT_ROLES or artifact_type.startswith("runtime."):
            return None
        if artifact_type == "browser.snapshot":
            label = _compact(source.get("title") or source.get("url") or "a new page", 120)
            return _fact(
                "browser-snapshot",
                f"Browser reached {label}.",
                "host.artifact_observation",
                identity={"url": source.get("url"), "title": source.get("title")},
                milestone="capability",
            )
        files = _changed_files(source)
        if artifact_type in {"file", "business.file"} and files:
            return _fact(
                "files-updated",
                _file_summary(files),
                "host.artifact_observation",
                identity={"files": files},
            )
        if "diff" in artifact_type:
            count = len(files)
            summary = (
                f"A reviewable diff is available for {count} file{'s' if count != 1 else ''}."
                if count
                else "A reviewable diff is available."
            )
            return _fact(
                "diff-ready",
                summary,
                "host.artifact_observation",
                identity={"files": files, "artifact_type": artifact_type},
            )
    return None


def tool_identity(payload: dict[str, Any] | None) -> str:
    source = payload if isinstance(payload, dict) else {}
    raw = source.get("raw") if isinstance(source.get("raw"), dict) else {}
    value = (
        source.get("item_id")
        or source.get("tool_use_id")
        or source.get("toolUseId")
        or raw.get("item_id")
        or raw.get("tool_use_id")
        or raw.get("toolUseId")
    )
    return _compact(value, 200)


def _fact(
    prefix: str,
    summary: str,
    source: str,
    *,
    identity: Any = None,
    explicit: bool = False,
    verified: bool = True,
    milestone: str = "",
    evidence: Literal["candidate", "reported", "observed"] = "observed",
) -> SemanticProgressFact | None:
    text = _summary(summary)
    if not text:
        return None
    material = identity if identity is not None else text.casefold()
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:16]
    return SemanticProgressFact(
        key=f"{prefix}:{digest}",
        summary=text,
        source=_compact(source, 80) or "host",
        explicit=bool(explicit),
        verified=bool(verified),
        milestone=valid_progress_milestone(milestone),
        evidence=evidence,
    )


def _bounded_tool_context(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    return {
        key: value
        for key, value in {
            "tool": _tool_name(payload),
            "item_id": _compact(payload.get("item_id"), 200),
            "tool_use_id": _compact(
                payload.get("tool_use_id") or payload.get("toolUseId"), 200
            ),
            "command": _compact(payload.get("command"), 1200),
            "changes": payload.get("changes") if isinstance(payload.get("changes"), list) else [],
            "scope": payload.get("scope") if isinstance(payload.get("scope"), (dict, list)) else raw.get("scope"),
            "raw": {
                key: raw.get(key)
                for key in ("toolName", "toolUseId", "capability", "action", "scope")
                if raw.get(key) not in (None, "")
            },
        }.items()
        if value not in (None, "", [], {})
    }


def _tool_name(payload: dict[str, Any]) -> str:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    return _compact(
        payload.get("tool")
        or payload.get("toolName")
        or payload.get("name")
        or raw.get("tool")
        or raw.get("toolName")
        or raw.get("name"),
        80,
    ).lower()


def _command(payload: dict[str, Any]) -> str:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    return _compact(payload.get("command") or raw.get("command"), 1200)


def _is_validation_command(command: str) -> bool:
    return bool(_VALIDATION_COMMAND_RE.search(str(command or "")))


def _tool_succeeded(payload: dict[str, Any]) -> bool:
    if "ok" in payload:
        return payload.get("ok") is True
    if "success" in payload:
        return payload.get("success") is True
    if payload.get("exit_code") is not None:
        try:
            return int(payload.get("exit_code")) == 0
        except (TypeError, ValueError):
            return False
    return str(payload.get("status") or "").strip().lower() in {
        "completed",
        "done",
        "success",
        "succeeded",
    }


def _diagnostic_only(payload: dict[str, Any]) -> bool:
    return payload.get("diagnosticOnly") is True or payload.get("diagnostic_only") is True


def _changed_files(payload: dict[str, Any]) -> list[str]:
    output: list[str] = []
    changes = payload.get("changes") if isinstance(payload.get("changes"), list) else []
    for item in changes:
        if not isinstance(item, dict):
            continue
        path = _compact(item.get("path") or item.get("file"), 2048)
        if path and path not in output:
            output.append(path)
    scope = payload.get("scope")
    if isinstance(scope, dict):
        path = _compact(scope.get("path"), 2048)
        if path and path not in output:
            output.append(path)
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    raw_scope = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
    raw_path = _compact(raw_scope.get("path"), 2048)
    if raw_path and raw_path not in output:
        output.append(raw_path)
    direct = _compact(payload.get("path") or payload.get("file"), 2048)
    if direct and direct not in output:
        output.append(direct)
    return output[:12]


def _file_summary(files: list[str]) -> str:
    names = []
    for value in files:
        name = PurePath(str(value).replace("\\", "/")).name or str(value)
        if name and name not in names:
            names.append(name)
    if not names:
        return "Project files were updated."
    shown = ", ".join(names[:3])
    suffix = f" and {len(names) - 3} more" if len(names) > 3 else ""
    return f"Updated project files: {shown}{suffix}."


def _summary(value: Any) -> str:
    return _compact(value, _MAX_SUMMARY)


def _compact(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
