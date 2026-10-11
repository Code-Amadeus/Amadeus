"""Read registered Work files using ledger identity and workspace containment."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_host.work_ledger_types import canonicalize_path, path_is_within


TEXT_SUFFIXES = frozenset({".json", ".csv", ".tsv", ".txt", ".md", ".markdown"})
TEXT_LIMIT_BYTES = 512 * 1024


def read_registered_text(store: Any, work_item_id: str, artifact_id: str) -> dict[str, Any]:
    """Preview a current registered revision, never a caller-supplied path.

    Plain text is returned as data, including Markdown. Reading a file neither
    executes it nor accepts the WorkItem. Large and non-UTF-8 files stay listed
    but do not enter this bounded text preview.
    """
    record = store.get_artifact(artifact_id)
    if record is None or record.work_item_id != work_item_id:
        raise ValueError("Artifact does not belong to this WorkItem.")
    item = store.get_work_item(work_item_id)
    if item is None or record.kind != "business.file" or record.status != "registered":
        raise ValueError("Artifact is not a registered workspace file.")
    current = latest_artifacts_by_path(store, store.list_artifacts(work_item_id))
    if artifact_id not in {row.artifact_id for row in current}:
        raise ValueError("A newer artifact revision exists. Refresh the artifact list.")
    path = contained_artifact_path(item.workspace_path, record.path)
    if path is None:
        raise ValueError("File is missing or no longer inside its WorkItem workspace.")
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError("Preview supports UTF-8 JSON, CSV, TSV, Markdown and TXT files.")
    try:
        with path.open("rb") as stream:
            raw = stream.read(TEXT_LIMIT_BYTES + 1)
    except OSError as exc:
        raise ValueError("File cannot be read.") from exc
    if len(raw) > TEXT_LIMIT_BYTES:
        raise ValueError("File exceeds the 512 KiB text preview limit.")
    digest = hashlib.sha256(raw).hexdigest()
    if not record.sha256 or record.sha256 != digest:
        raise ValueError("File no longer matches its registered revision.")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("File is not UTF-8 text.") from exc
    if "\0" in content:
        raise ValueError("File is not UTF-8 text.")
    return {"artifactId": artifact_id, "workItemId": work_item_id,
        "title": record.title or path.name, "content": content,
        "sha256": digest, "sizeBytes": len(raw)}


def latest_artifacts_by_path(store: Any, records: list[Any]) -> list[Any]:
    """Collapse attempt history into each artifact kind's current path view.

    Artifact timestamps can tie at the platform clock's resolution.  Attempt
    number is the ledger's stable causal order, so a later delivery always
    supersedes an older record for the same path.  Timestamps only order
    updates within one attempt.  Narrow simulation stores without attempt
    records retain deterministic fallback ordering.
    """

    attempt_numbers: dict[str, int] = {}

    def revision_key(record: Any) -> tuple[int, float, float, str]:
        attempt_id = str(getattr(record, "attempt_id", "") or "").strip()
        number = attempt_numbers.get(attempt_id)
        if number is None:
            number = 0
            get_attempt = getattr(store, "get_attempt", None)
            if attempt_id and callable(get_attempt):
                attempt = get_attempt(attempt_id)
                number = int(getattr(attempt, "attempt_number", 0) or 0)
            attempt_numbers[attempt_id] = number
        return (
            number,
            float(getattr(record, "updated_at", 0.0) or 0.0),
            float(getattr(record, "created_at", 0.0) or 0.0),
            str(getattr(record, "artifact_id", "") or ""),
        )

    ordered = sorted(
        records,
        key=revision_key,
        reverse=True,
    )
    latest: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for record in ordered:
        path = str(getattr(record, "path", "") or "").strip()
        identity = str(getattr(record, "path_identity", "") or "").strip()
        path_key = identity or artifact_path_key(path)
        # A later Provider declaration is not a new Host business.file revision.
        key = str(getattr(record, "kind", "")), path_key
        if not path_key or key in seen:
            continue
        seen.add(key)
        latest.append(record)
    return latest


def artifact_path_key(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    try:
        return canonicalize_path(clean).identity_key
    except (OSError, ValueError):
        return clean.casefold()


def contained_artifact_path(workspace_path: str, artifact_path: str) -> Path | None:
    """Resolve the artifact inside its workspace, or refuse.

    The registry writes absolute paths, so this is not translating a relative
    path -- it is checking that a stored one still points where it claims,
    before the host reads it on a renderer's request.
    """

    root_text = str(workspace_path or "").strip()
    target_text = str(artifact_path or "").strip()
    if not root_text or not target_text:
        return None
    try:
        root = canonicalize_path(root_text)
        candidate = canonicalize_path(target_text)
    except (OSError, ValueError):
        return None
    if candidate.identity_key == root.identity_key or not path_is_within(
        candidate.canonical_path,
        root.canonical_path,
    ):
        return None
    candidate_path = Path(candidate.canonical_path)
    if not candidate_path.is_file():
        return None
    return candidate_path
