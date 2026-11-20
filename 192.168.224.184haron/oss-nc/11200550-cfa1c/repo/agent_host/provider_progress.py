"""Provider-neutral contract for sparse, factual execution milestones.

This is an execution-output contract, not a task-routing vocabulary.  It gives
providers three ways to report facts that are useful to a person following a
run, while leaving task intent and completion authority unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from agent_host.provider_types import ProviderActivityEvidence


ProgressMilestoneKind = Literal["design", "diagnostic", "capability", "validation"]

PROGRESS_MILESTONE_KINDS = frozenset(
    {"design", "diagnostic", "capability", "validation"}
)
_MAX_PROGRESS_SUMMARY = 320
_PROGRESS_LINE = re.compile(
    r"^\s*\[progress:(design|diagnostic|capability|validation)\]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_PROGRESS_TOKEN = re.compile(
    r"\[progress:(design|diagnostic|capability|validation)\]\s*",
    re.IGNORECASE,
)
_PROGRESS_STREAM_PREFIX = "[progress:"

PROVIDER_PROGRESS_CONTRACT = """
Progress reporting contract (this does not change the requested task):
As soon as a new factual milestone becomes true, emit one standalone line in one of
these exact forms:
[PROGRESS:DESIGN] <what was decided and why>
[PROGRESS:DIAGNOSTIC] <what unexpected fact was observed, its impact, and the recovery direction>
[PROGRESS:CAPABILITY] <what user-visible capability is now implemented>
[PROGRESS:VALIDATION] <what was checked, the result, and any problem found>
Do not postpone or batch milestone lines into the final response. Do not report
filenames or tool actions by themselves, or repeat the same update. Treat the
lines as phase checkpoints: emit DESIGN as soon as the initial execution
direction is selected, before lengthy inspection or implementation. DESIGN may
say what you are about to inspect, build, or make sure to validate and why, but
must use current/future wording and must not imply that work already succeeded.
Emit another DESIGN only if that direction materially changes. Before a
nontrivial validation phase, that may be a DESIGN update explaining what now
needs to be verified; later VALIDATION must report the actual check result.
Emit DIAGNOSTIC immediately when an objective mismatch, failed check, or
recoverable execution problem changes what you will do next. Report the
observation and recovery direction, never a request for new authority. Any new
action still follows the normal approval and execution contract.
Emit CAPABILITY immediately after the first coherent user-visible behavior
exists and before moving on to validation; emit VALIDATION immediately after
the check finishes.
Do not start the next phase while its preceding checkpoint is still unreported.
Your first visible provider output must be DESIGN before the first tool call or
lengthy implementation work. Hidden reasoning is not a progress update.
Keep each description to one concise, user-facing sentence suitable for display
above a compact progress bar.
These lines are intermediate output, not a stopping point. After emitting a
milestone, continue the same Provider turn immediately. For a workspace
mutation, never end the turn after only DESIGN or another progress line;
continue until the requested artifact and validation exist, or report one
concrete blocker that actually prevents further work. Do not use these lines as
a final completion claim.
""".strip()

_PRESENTATION_LANGUAGES = {
    "en-US": "English",
    "zh-CN": "Simplified Chinese",
    "ja-JP": "Japanese",
}


def with_progress_contract(task: str, *, presentation_locale: object = None) -> str:
    """Append the reporting contract without changing the task's authority."""

    body = str(task or "").rstrip()
    locale = _normalize_presentation_locale(presentation_locale)
    language = _PRESENTATION_LANGUAGES[locale]
    language_contract = (
        "Presentation language contract (reporting only; this does not change the requested task):\n"
        f"Write milestone descriptions and the final user-facing result summary in {language}. "
        "Keep code identifiers, commands, paths, URLs, quoted source text, and artifact contents verbatim."
    )
    contracts = f"{PROVIDER_PROGRESS_CONTRACT}\n\n{language_contract}"
    if not body:
        return contracts
    return f"{body}\n\n{contracts}"


def _normalize_presentation_locale(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if raw in {"zh", "zh-cn", "chinese", "simplified-chinese"}:
        return "zh-CN"
    if raw in {"ja", "jp", "ja-jp", "japanese"}:
        return "ja-JP"
    return "en-US"


def split_progress_milestones(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Remove complete milestone lines and return their canonical payloads."""

    raw = str(text or "")
    kept: list[str] = []
    milestones: list[dict[str, Any]] = []
    for line in raw.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        match = _PROGRESS_LINE.match(content)
        token_matches = list(_PROGRESS_TOKEN.finditer(content))
        if not match and not token_matches:
            kept.append(line)
            continue
        if token_matches:
            visible_prefix = content[: token_matches[0].start()]
            if visible_prefix:
                kept.append(visible_prefix + line[len(content) :])
            for index, token in enumerate(token_matches):
                end = (
                    token_matches[index + 1].start()
                    if index + 1 < len(token_matches)
                    else len(content)
                )
                summary = _compact(content[token.end() : end], _MAX_PROGRESS_SUMMARY)
                if summary:
                    milestones.append(
                        progress_payload(
                            token.group(1),
                            summary,
                            source="provider_explicit_progress",
                            explicit=True,
                            verified=False,
                        )
                    )
        elif match:
            summary = _compact(match.group(2), _MAX_PROGRESS_SUMMARY)
            if summary:
                milestones.append(
                    progress_payload(
                        match.group(1),
                        summary,
                        source="provider_explicit_progress",
                        explicit=True,
                        verified=False,
                    )
                )
    if not milestones:
        return raw, []
    return "".join(kept), milestones


def split_progress_stream(
    pending_text: str,
    text: str,
    *,
    final: bool = False,
) -> tuple[str, list[dict[str, Any]], str]:
    """Filter progress markers at a streaming Provider boundary.

    A transport may split ``[PROGRESS:...]`` at any byte-to-text chunk
    boundary.  Only a tail that could still become a contract marker is held;
    ordinary assistant text remains streaming.  The return value is visible
    text, canonical milestone payloads, and the next pending tail.
    """

    incoming = str(text or "")
    buffered = str(pending_text or "")
    if buffered and incoming.lstrip().casefold().startswith(_PROGRESS_STREAM_PREFIX):
        combined = f"{buffered}\n{incoming}"
    else:
        combined = f"{buffered}{incoming}"
    if not combined:
        return "", [], ""

    next_pending = ""
    if not final and not combined.endswith(("\n", "\r")):
        lines = combined.splitlines(keepends=True)
        tail = lines.pop() if lines else combined
        candidate = tail.casefold()
        possible_marker = _PROGRESS_STREAM_PREFIX in candidate or any(
            candidate.endswith(_PROGRESS_STREAM_PREFIX[:length])
            for length in range(1, len(_PROGRESS_STREAM_PREFIX))
        )
        if possible_marker:
            next_pending = tail
            combined = "".join(lines)

    visible, milestones = split_progress_milestones(combined)
    return visible, milestones, next_pending


def progress_payload(
    milestone: str,
    summary: str,
    *,
    source: str,
    explicit: bool,
    verified: bool,
    status: str = "reported",
) -> dict[str, Any]:
    """Build a bounded canonical ``semantic.progress`` payload."""

    kind = str(milestone or "").strip().lower()
    if kind not in PROGRESS_MILESTONE_KINDS:
        raise ValueError(f"Unsupported progress milestone: {milestone!r}")
    text = _compact(summary, _MAX_PROGRESS_SUMMARY)
    if not text:
        raise ValueError("Progress milestone summary must not be empty")
    return {
        "milestone": kind,
        "summary": text,
        "source": _compact(source, 80) or "provider",
        "explicit": bool(explicit),
        "verified": bool(verified),
        "status": _compact(status, 32) or "reported",
    }


def valid_progress_milestone(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in PROGRESS_MILESTONE_KINDS else ""


def is_progress_only_workspace_completion(
    *,
    status: object,
    result_text: object,
    task_kind: object,
    workspace_access: object,
    activity_evidence: ProviderActivityEvidence | None,
) -> bool:
    """Recognize a completed write turn that stopped at progress reporting.

    The predicate is intentionally narrow. Missing or provider-authored
    evidence cannot authorize a continuation, and read-only work may
    legitimately finish after a plan or another reported milestone.
    """

    return bool(
        str(status or "").strip().lower() == "done"
        and not str(result_text or "").strip()
        and str(task_kind or "").strip().lower() == "workspace_mutation"
        and str(workspace_access or "").strip().lower() == "write"
        and isinstance(activity_evidence, ProviderActivityEvidence)
        and activity_evidence.observation_authority == "host"
        and activity_evidence.terminal_observed
        and activity_evidence.progress_milestones > 0
        and activity_evidence.execution_items == 0
    )


def _compact(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
