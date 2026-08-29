"""Runtime schemas for Amadeus' AI OS work surface.

This module intentionally uses stdlib TypedDicts and small normalizers instead
of a heavy validation dependency. The goal is to keep a stable, provider-neutral
contract across current and future AUIP providers while still
returning plain dict payloads that the existing event bus and renderer consume.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

try:
    from typing import NotRequired
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    from typing_extensions import NotRequired  # type: ignore[assignment]


SCHEMA_VERSION = "amadeus.ai_os.v1"

ProviderStatus = Literal["queued", "running", "done", "error", "cancelled"]
WorkPhase = Literal[
    "Intake",
    "Contract",
    "Work",
    "Checkpoint",
    "Preview",
    "Review",
    "Result",
    "Archive",
]
Importance = Literal["ambient", "normal", "important", "blocking", "urgent", "error"]
CanvasMode = Literal["workflow", "browser", "markdown", "diff", "html", "image", "table", "code", "permission"]
CanvasLifecycle = Literal["ambient", "ephemeral", "pinned"]
ActionRisk = Literal["none", "local_view", "local_execution", "external_side_effect", "sensitive"]
ActionKind = Literal[
    "file",
    "folder",
    "url",
    "command",
    "browser",
    "provider",
    "artifact",
    "permission",
    "work_item",
]
EvidenceKind = Literal[
    "status",
    "tool",
    "file",
    "diff",
    "terminal",
    "test",
    "artifact",
    "permission",
    "source",
    "browser",
    "command",
    "run",
    "report",
]
ObserverPolicy = Literal["auto", "silent"]
ObserverAction = Literal["silent", "canvas_update", "subtitle", "speak", "ask_user", "final_report"]


class ActionRef(TypedDict):
    schema_id: str
    kind: ActionKind
    label: str
    defaultAction: str
    actions: list[str]
    risk: ActionRisk
    uri: NotRequired[str]
    path: NotRequired[str]
    url: NotRequired[str]
    command: NotRequired[str]
    ref: NotRequired[str]
    metadata: NotRequired[dict[str, Any]]


class WorkSignal(TypedDict):
    schema_id: str
    kind: EvidenceKind
    label: str
    text: str
    detail: NotRequired[str]
    importance: NotRequired[Importance]
    ref: NotRequired[str]
    actions: NotRequired[list[ActionRef]]
    metadata: NotRequired[dict[str, Any]]


class CanvasArtifact(TypedDict):
    schema_id: str
    kind: str
    title: str
    summary: NotRequired[str]
    uri: NotRequired[str]
    mime_type: NotRequired[str]
    content: NotRequired[dict[str, Any]]
    refs: NotRequired[list[ActionRef]]
    metadata: NotRequired[dict[str, Any]]


class WallpaperCanvasPayload(TypedDict):
    schema_id: str
    mode: CanvasMode
    phase: WorkPhase
    title: str
    lead: str
    progress: NotRequired[int]
    signals: NotRequired[list[WorkSignal]]
    actions: NotRequired[list[ActionRef]]
    artifact: NotRequired[CanvasArtifact]
    lifecycle: NotRequired[CanvasLifecycle]
    sizePreset: NotRequired[str]
    size: NotRequired[dict[str, int]]
    open: NotRequired[bool]
    visible: NotRequired[bool]
    clear: NotRequired[bool]
    ttlMs: NotRequired[int]
    metadata: NotRequired[dict[str, Any]]
    # Renderer compatibility fields. These mirror current CRT canvas inputs and
    # should shrink over time as the renderer consumes artifact/actions directly.
    markdown: NotRequired[str]
    diff: NotRequired[dict[str, Any]]
    reportMarkdown: NotRequired[str]
    reportView: NotRequired[dict[str, Any]]
    html: NotRequired[str]
    url: NotRequired[str]
    browserSessionId: NotRequired[str]
    pageTitle: NotRequired[str]
    excerpt: NotRequired[str]
    links: NotRequired[list[dict[str, str]]]
    screenshot: NotRequired[str]
    permissionVisible: NotRequired[bool]
    permissionRequest: NotRequired[dict[str, Any]]
    # Provider-neutral task navigation. The backend owns this projection and
    # sends it together with the selected canvas; the renderer never infers
    # task completion or workspace ownership from presentation fields.
    workContext: NotRequired[dict[str, Any]]
    taskDock: NotRequired[dict[str, Any]]


class WorkNote(TypedDict):
    schema_id: str
    source: str
    provider: str
    run_id: str
    session_id: str
    phase: WorkPhase
    title: str
    summary: str
    signals: list[WorkSignal]
    importance: Importance
    observer_policy: ObserverPolicy
    metadata: dict[str, Any]
    speak: bool


class ObserverDecision(TypedDict):
    schema_id: str
    source: str
    run_id: str
    session_id: str
    provider: str
    action: ObserverAction
    terminal: bool
    append_to_main_chat: bool
    speak: bool
    display_text: str
    main_chat_entry: str
    reason: str
    note_count: NotRequired[int]


class PermissionRequest(TypedDict):
    schema_id: str
    id: str
    provider: str
    run_id: str
    capability: str
    action: str
    level: int
    scope: list[str]
    reason: str
    reversibility: str
    status: Literal["pending", "allowed", "denied", "expired"]
    options: list[str]
    metadata: NotRequired[dict[str, Any]]


_PHASE_MAP: dict[str, WorkPhase] = {
    "intake": "Intake",
    "contract": "Contract",
    "work": "Work",
    "active": "Work",
    "active_work": "Work",
    "checkpoint": "Checkpoint",
    "permission": "Checkpoint",
    "preview": "Preview",
    "review": "Review",
    "result": "Result",
    "done": "Result",
    "archive": "Archive",
    "archived": "Archive",
}

_MODES: set[str] = {"workflow", "browser", "markdown", "diff", "html", "image", "table", "code", "permission"}
_IMPORTANCE: set[str] = {"ambient", "normal", "important", "blocking", "urgent", "error"}
_LIFECYCLE: set[str] = {"ambient", "ephemeral", "pinned"}
_OBSERVER_POLICY: set[str] = {"auto", "silent"}
_EVIDENCE_KINDS: set[str] = {
    "status",
    "tool",
    "file",
    "diff",
    "terminal",
    "test",
    "artifact",
    "permission",
    "source",
    "browser",
    "command",
    "run",
    "report",
}


def normalize_phase(value: Any) -> WorkPhase:
    text = str(value or "Work").strip()
    if text in _PHASE_MAP.values():
        return text  # type: ignore[return-value]
    return _PHASE_MAP.get(text.lower(), "Work")


def normalize_canvas_mode(value: Any) -> CanvasMode:
    text = str(value or "workflow").strip().lower().replace("-", "_")
    if text in {"work", "work_signal", "provider_work"}:
        return "workflow"
    if text in {"web", "webview", "page", "browser_snapshot"}:
        return "browser"
    if text in _MODES:
        return text  # type: ignore[return-value]
    return "workflow"


def normalize_importance(value: Any) -> Importance:
    text = str(value or "normal").strip().lower()
    if text in _IMPORTANCE:
        return text  # type: ignore[return-value]
    return "normal"


def normalize_evidence_kind(value: Any) -> EvidenceKind:
    text = str(value or "status").strip().lower().replace("-", "_")
    if text in _EVIDENCE_KINDS:
        return text  # type: ignore[return-value]
    return "status"


def normalize_lifecycle(value: Any) -> CanvasLifecycle:
    text = str(value or "ephemeral").strip().lower()
    if text in _LIFECYCLE:
        return text  # type: ignore[return-value]
    return "ephemeral"


def normalize_observer_policy(value: Any) -> ObserverPolicy:
    text = str(value or "auto").strip().lower()
    if text in _OBSERVER_POLICY:
        return text  # type: ignore[return-value]
    return "auto"


def trim_text(value: Any, limit: int = 240) -> str:
    cleaned = " ".join(str(value or "").split())
    if limit <= 0 or len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def bounded_progress(value: Any, default: int = 0) -> int:
    try:
        number = int(float(value))
    except Exception:
        number = default
    return max(0, min(100, number))


def action_ref(
    *,
    kind: ActionKind,
    label: str,
    default_action: str,
    actions: list[str],
    risk: ActionRisk = "none",
    uri: str = "",
    path: str = "",
    url: str = "",
    command: str = "",
    ref: str = "",
    metadata: dict[str, Any] | None = None,
) -> ActionRef:
    item: ActionRef = {
        "schema_id": SCHEMA_VERSION,
        "kind": kind,
        "label": trim_text(label, 120),
        "defaultAction": str(default_action or "").strip(),
        "actions": [str(action) for action in actions if str(action).strip()],
        "risk": risk,
    }
    for key, value in (("uri", uri), ("path", path), ("url", url), ("command", command), ("ref", ref)):
        if value:
            item[key] = str(value)  # type: ignore[literal-required]
    if metadata:
        item["metadata"] = dict(metadata)
    return item


def presentation_message(key: str, /, **params: Any) -> dict[str, Any]:
    """Describe host-owned copy without turning it into durable content.

    The English string fields remain the wire-compatible fallback.  This
    descriptor is presentation evidence only: the shared Canvas projection
    resolves it at the last boundary before either wallpaper host renders it.
    """

    message: dict[str, Any] = {"key": str(key or "").strip()}
    clean_params = {
        str(name): value
        for name, value in params.items()
        if str(name).strip() and value is not None
    }
    if clean_params:
        message["params"] = clean_params
    return message


def _with_presentation_metadata(
    metadata: dict[str, Any] | None,
    presentation: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    output = dict(metadata or {})
    if presentation:
        output["presentation"] = {
            str(field): dict(message)
            for field, message in presentation.items()
            if str(field).strip() and isinstance(message, dict) and message.get("key")
        }
    return output or None


def work_signal(
    *,
    label: str,
    text: str,
    detail: str = "",
    kind: EvidenceKind = "status",
    importance: Importance = "normal",
    ref: str = "",
    actions: list[ActionRef] | None = None,
    metadata: dict[str, Any] | None = None,
    presentation: dict[str, dict[str, Any]] | None = None,
) -> WorkSignal:
    signal: WorkSignal = {
        "schema_id": SCHEMA_VERSION,
        "kind": normalize_evidence_kind(kind),
        "label": trim_text(label, 48),
        "text": trim_text(text, 260),
        "importance": normalize_importance(importance),
    }
    if detail:
        signal["detail"] = trim_text(detail, 160)
    if ref:
        signal["ref"] = str(ref)
    if actions:
        signal["actions"] = actions
    signal_metadata = _with_presentation_metadata(metadata, presentation)
    if signal_metadata:
        signal["metadata"] = signal_metadata
    return signal


def normalize_signal(value: dict[str, Any]) -> WorkSignal:
    return work_signal(
        kind=normalize_evidence_kind(value.get("kind") or value.get("type") or "status"),
        label=str(value.get("label") or "signal"),
        text=str(value.get("text") or value.get("summary") or ""),
        detail=str(value.get("detail") or ""),
        importance=normalize_importance(value.get("importance")),
        ref=str(value.get("ref") or ""),
        actions=value.get("actions") if isinstance(value.get("actions"), list) else None,
        metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else None,
    )


def canvas_artifact(
    *,
    kind: str,
    title: str,
    summary: str = "",
    uri: str = "",
    mime_type: str = "",
    content: dict[str, Any] | None = None,
    refs: list[ActionRef] | None = None,
    metadata: dict[str, Any] | None = None,
) -> CanvasArtifact:
    artifact: CanvasArtifact = {
        "schema_id": SCHEMA_VERSION,
        "kind": str(kind or "generic"),
        "title": trim_text(title, 160),
    }
    if summary:
        artifact["summary"] = trim_text(summary, 520)
    if uri:
        artifact["uri"] = str(uri)
    if mime_type:
        artifact["mime_type"] = str(mime_type)
    if content:
        artifact["content"] = dict(content)
    if refs:
        artifact["refs"] = refs
    if metadata:
        artifact["metadata"] = dict(metadata)
    return artifact


def canvas_payload(
    *,
    mode: CanvasMode = "workflow",
    phase: Any = "Work",
    title: str,
    lead: str = "",
    progress: Any = None,
    signals: list[dict[str, Any]] | list[WorkSignal] | None = None,
    actions: list[ActionRef] | None = None,
    artifact: CanvasArtifact | None = None,
    lifecycle: CanvasLifecycle = "ephemeral",
    size_preset: str = "compact",
    open: bool = True,
    visible: bool | None = None,
    ttl_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
    presentation: dict[str, dict[str, Any]] | None = None,
) -> WallpaperCanvasPayload:
    payload: WallpaperCanvasPayload = {
        "schema_id": SCHEMA_VERSION,
        "mode": normalize_canvas_mode(mode),
        "phase": normalize_phase(phase),
        "title": trim_text(title, 160),
        "lead": trim_text(lead, 700),
        "signals": [normalize_signal(dict(item)) for item in (signals or [])],
        "lifecycle": normalize_lifecycle(lifecycle),
        "sizePreset": "wide" if size_preset == "wide" else "compact",
        "open": bool(open),
    }
    if progress is not None:
        payload["progress"] = bounded_progress(progress)
    if actions:
        payload["actions"] = actions
    if artifact:
        payload["artifact"] = artifact
    if visible is not None:
        payload["visible"] = bool(visible)
    if ttl_ms is not None:
        payload["ttlMs"] = max(0, int(ttl_ms))
    payload_metadata = _with_presentation_metadata(metadata, presentation)
    if payload_metadata:
        payload["metadata"] = payload_metadata
    return payload


def browser_canvas_payload(
    *,
    phase: Any,
    title: str,
    excerpt: str,
    url: str,
    browser_session_id: str,
    links: list[dict[str, str]] | None = None,
    screenshot: str = "",
    signals: list[dict[str, Any]] | list[WorkSignal] | None = None,
    progress: Any = 0,
    result_text: str = "",
    size_preset: str = "wide",
    metadata: dict[str, Any] | None = None,
    presentation: dict[str, dict[str, Any]] | None = None,
) -> WallpaperCanvasPayload:
    summary = excerpt or result_text or title
    artifact = canvas_artifact(
        kind="browser.snapshot",
        title=title,
        summary=summary,
        uri=url,
        mime_type="text/html",
        content={
            "url": url,
            "browserSessionId": browser_session_id,
            "excerpt": excerpt,
            "links": list(links or []),
            "screenshot": screenshot,
        },
    )
    payload = canvas_payload(
        mode="browser",
        phase=phase,
        title=title,
        lead=summary,
        progress=progress,
        signals=signals,
        artifact=artifact,
        size_preset=size_preset,
        open=True,
        metadata=metadata,
        presentation=presentation,
    )
    payload.update(
        {
            "url": url,
            "browserSessionId": browser_session_id,
            "pageTitle": title,
            "excerpt": trim_text(excerpt or result_text, 700),
            "links": list(links or [])[:8],
            "screenshot": screenshot,
        }
    )
    return payload


def markdown_canvas_payload(
    *,
    phase: Any,
    title: str,
    lead: str,
    markdown: str,
    signals: list[dict[str, Any]] | list[WorkSignal] | None = None,
    actions: list[ActionRef] | None = None,
    progress: Any = 100,
    size_preset: str = "compact",
    open: bool = True,
    metadata: dict[str, Any] | None = None,
    presentation: dict[str, dict[str, Any]] | None = None,
) -> WallpaperCanvasPayload:
    artifact = canvas_artifact(
        kind="markdown.report",
        title=title,
        summary=lead,
        mime_type="text/markdown",
        content={"markdown": markdown},
        refs=actions,
    )
    payload = canvas_payload(
        mode="markdown",
        phase=phase,
        title=title,
        lead=lead,
        progress=progress,
        signals=signals,
        actions=actions,
        artifact=artifact,
        size_preset=size_preset,
        open=open,
        metadata=metadata,
        presentation=presentation,
    )
    payload["markdown"] = markdown
    return payload


def diff_canvas_payload(
    *,
    phase: Any,
    title: str,
    lead: str,
    diff: dict[str, Any],
    report_markdown: str = "",
    report_view: dict[str, Any] | None = None,
    signals: list[dict[str, Any]] | list[WorkSignal] | None = None,
    actions: list[ActionRef] | None = None,
    progress: Any = 100,
    size_preset: str = "wide",
    open: bool = True,
    metadata: dict[str, Any] | None = None,
    presentation: dict[str, dict[str, Any]] | None = None,
) -> WallpaperCanvasPayload:
    structured = dict(diff or {})
    artifact = canvas_artifact(
        kind="code.diff",
        title=title,
        summary=lead,
        mime_type="text/x-diff",
        content={"diff": structured},
        refs=actions,
    )
    payload = canvas_payload(
        mode="diff",
        phase=phase,
        title=title,
        lead=lead,
        progress=progress,
        signals=signals,
        actions=actions,
        artifact=artifact,
        size_preset=size_preset,
        open=open,
        metadata=metadata,
        presentation=presentation,
    )
    payload["diff"] = structured
    payload["size"] = {"width": 680, "height": 580}
    if report_markdown:
        payload["reportMarkdown"] = str(report_markdown)
    if report_view:
        payload["reportView"] = dict(report_view)
    return payload


def work_note_payload(
    *,
    source: str,
    provider: str,
    run_id: str,
    session_id: str,
    phase: Any,
    title: str,
    summary: str,
    signals: list[dict[str, Any]] | list[WorkSignal],
    importance: Any = "normal",
    observer_policy: Any = "auto",
    metadata: dict[str, Any] | None = None,
    speak: bool = False,
) -> WorkNote:
    return {
        "schema_id": SCHEMA_VERSION,
        "source": str(source or "provider"),
        "provider": str(provider or "provider"),
        "run_id": str(run_id or ""),
        "session_id": str(session_id or ""),
        "phase": normalize_phase(phase),
        "title": trim_text(title, 160),
        "summary": trim_text(summary, 520),
        "signals": [normalize_signal(dict(item)) for item in signals],
        "importance": normalize_importance(importance),
        "observer_policy": normalize_observer_policy(observer_policy),
        "metadata": dict(metadata or {}),
        "speak": bool(speak),
    }
