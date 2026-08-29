"""Data-driven, isolated routing E2E testbed.

This module is deliberately a measuring instrument.  It observes existing
routing behaviour and never imports or patches production routing policy.
Replay mode is the deterministic regression gate; real mode records evidence
from an isolated backend and scratch git repository.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.semantic_journey_evidence import build_evidence, write_evidence

SCENARIO_DIR = ROOT / "tools" / "routing_scenarios"
FIXTURE_DIR = ROOT / "tools" / "routing_fixtures"
DEFAULT_REPORT_DIR = ROOT / "runtime" / "e2e_reports" / "routing"
SCRATCH_TARGET = ROOT / "runtime" / "e2e_routing_target"
# `readonly_ref` = the utterance refers to an existing task without instructing
# any change (status/progress/result questions). The routing contract requires
# these to be answered from ledger facts with no DELEGATE, so they leave no
# routing trace at all — labelling them `continue` made correct behaviour score
# as a mismatch. They keep the `chat` hard invariant: never create work.
#
# `retract` = the utterance withdraws an instruction that is already in flight
# ("wait, not that file", "never mind, stop"). It is neither a continuation nor
# a close nor chat: the correct outcome is that the running attempt is cancelled
# and no new work is created. Voice barge-in makes this common, and until now
# the taxonomy had no cell for it, so the failure was invisible to measurement.
VALID_LABELS = {
    "continue",
    "new",
    "close",
    "chat",
    "ambiguous",
    "readonly_ref",
    "retract",
}
VALID_WAITS = {"chat_complete", "provider_terminal", "work_note", "none"}
VALID_ACTIONS = {
    "snapshot_ledger",
    "ws_interrupt",
    "kill_provider",
    "restart_backend",
    "sleep",
}
LONG_SILENCE_SCENARIOS = {"C2_long_silence", "J6_failure_recovery_journey"}
REAL_ONLY_SCENARIOS = {"J6_failure_recovery_journey"}


class InfrastructureError(RuntimeError):
    """The run never got to exercise routing at all.

    A dead LLM endpoint produces no reply, so no DELEGATE tag, so no provider
    run — which then looks exactly like a routing failure. On 2026-07-31 an
    exhausted API balance burned two 300s timeouts per run and was recorded as
    `provider_terminal_not_succeeded`, i.e. an infrastructure outage masquerading
    as evidence. These runs must abort fast and be reported as skipped, never
    scored.
    """


class BootstrapUnrecoverable(RuntimeError):
    """The run's first delegate was blocked, so nothing can route afterwards.

    Workspace candidates are derived from existing WorkItems, so an empty
    ledger yields zero candidates and the host refuses with
    `no_allowlisted_project`. That refusal is correct, but it means the first
    WorkItem is never created — and every later turn is refused for the same
    reason. On 2026-07-31 one D1 run emitted five DELEGATEs, none carrying
    `cwd`, and ground through nineteen turns of 300s timeouts before being
    killed, having recorded nothing but "the model routed nothing".

    Unlike InfrastructureError this is per-run variance (whether the model
    echoed `cwd` on turn one), not an outage, so the campaign continues with
    the next repeat instead of circuit-breaking.
    """


# Substrings that mean "the harness could not run", not "routing was wrong".
INFRA_ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Insufficient Balance", "LLM account balance exhausted"),
    ("Error code: 401", "LLM rejected the credentials"),
    ("Error code: 402", "LLM billing refused the request"),
    ("Error code: 429", "LLM rate limited the request"),
    ("APIConnectionError", "LLM endpoint unreachable"),
    ("APITimeoutError", "LLM endpoint timed out"),
)


def infrastructure_error(lines: Iterable[str]) -> str | None:
    """Return a reason when the log shows the harness itself is unusable."""

    for line in lines:
        for needle, reason in INFRA_ERROR_PATTERNS:
            if needle in line:
                return f"{reason} [{needle}]"
    return None
TERMINAL_PROVIDER_STATUSES = {
    "done",
    "succeeded",
    "completed",
    "error",
    "failed",
    "cancelled",
    "canceled",
    "denied",
}
SUCCESS_PROVIDER_STATUSES = {"done", "succeeded", "completed"}
FAILED_PROVIDER_STATUSES = {
    "error",
    "failed",
    "cancelled",
    "canceled",
    "denied",
}
MIN_REAL_MODE_FREE_BYTES = 5 * 1024**3
LOG_ANCHORS = (
    ("llm branch=close handled; closed=", "close"),
    ("branch region squashed", "close"),
    ("branch=continue without active branch; falling through as new run", "new"),
    ("llm-routed branch continuation", "continue"),
    ("routing chat turn into browser interaction branch", "continue"),
)


class ScenarioError(ValueError):
    """A scenario file does not conform to schema version 1."""


def _event_parts(event: Any) -> tuple[str, dict[str, Any]]:
    """Return a uniform method/params view for live and recorded WS events."""

    if isinstance(event, dict):
        method = str(event.get("method") or "")
        params = event.get("params")
    else:
        method = str(getattr(event, "method", "") or "")
        params = getattr(event, "params", None)
    return method, params if isinstance(params, dict) else {}


def _provider_wait_state(
    events: Iterable[Any],
    *,
    after: int,
    wait: str,
    known_run_ids: Iterable[str] = (),
    active_run_ids: Iterable[str] = (),
) -> tuple[str | None, bool]:
    """Track only the provider run created by the current scenario step.

    A previous step may still emit work notes or a terminal event after the next
    utterance is sent.  Those late events must never satisfy the next step's
    wait condition.
    """

    current_run_id: str | None = None
    previous_run_ids = {str(run_id) for run_id in known_run_ids if str(run_id)}
    continuable_run_ids = {str(run_id) for run_id in active_run_ids if str(run_id)}
    for event in list(events)[after:]:
        method, params = _event_parts(event)
        if (
            wait == "provider_terminal"
            and method == "chat.observer_decision"
            and str(params.get("source") or "").strip().lower()
            == "reference_clarification"
        ):
            # The Host has already reached a visible, fail-closed control
            # outcome. Waiting another five minutes for a Provider terminal
            # that cannot exist hides the actual product failure behind an
            # unrelated timeout.
            return current_run_id, True
        run_id = str(params.get("run_id") or "")
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        metadata = (
            params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        )
        if (
            current_run_id is None
            and method == "provider.event"
            and str(params.get("type") or "").lower() == "run.created"
            and run_id
            and run_id not in previous_run_ids
        ):
            current_run_id = run_id
        if (
            current_run_id is None
            and method == "provider.event"
            and str(params.get("type") or "").lower() == "run.status"
            and str(payload.get("stage") or "").lower()
            in {"steer_queued", "steer_applied"}
            and run_id in continuable_run_ids
        ):
            # Codex App Server can continue the run that was already active
            # before this utterance.  The steer receipt, not another
            # run.created event, proves that this step now owns its terminal
            # wait.  Finished historical runs remain excluded.
            current_run_id = run_id
        if not current_run_id or run_id != current_run_id:
            continue
        if wait == "work_note" and method == "chat.work_note":
            return current_run_id, True
        if wait == "provider_terminal" and (
            method == "provider.result"
            or (
                method == "provider.event"
                and str(params.get("type") or "").lower()
                in {"run.finished", "run.failed", "run.cancelled", "run.status"}
                and str(payload.get("status") or "").lower()
                in TERMINAL_PROVIDER_STATUSES
            )
            or (
                method == "chat.work_note"
                and (
                    str(params.get("phase") or "").lower() == "result"
                    or str(metadata.get("narration_keypoint") or "").lower()
                    == "terminal"
                )
            )
        ):
            return current_run_id, True
    return current_run_id, False


# A/B on whether the harness is manufacturing the tag omission it measures.
# `permissive` is the original framing: "if delegation is needed" reads as an
# option, and the clause before it can be heard as "do not act". `imperative`
# states the same requirement as an obligation. Only this clause differs — the
# routing rules appended after it are byte-identical in both arms, so a change
# in omission rate is attributable to the framing alone.
PREAMBLE_VARIANTS = {
    "permissive": "这是路由协议测试；不要在主对话中直接执行任务。若需要委托，",
    "imperative": (
        "这是路由协议测试；凡是创建、修改、运行文件或代码的要求，"
        "本轮回复必须发出 DELEGATE 标签，口头答应不算执行。"
    ),
}
_preamble_variant = "permissive"


def set_preamble_variant(name: str) -> None:
    if name not in PREAMBLE_VARIANTS:
        raise ScenarioError(f"unknown preamble variant {name!r}")
    global _preamble_variant
    _preamble_variant = name


def current_preamble_variant() -> str:
    return _preamble_variant


def _prepare_real_utterance(
    say: str,
    *,
    execution_provider: str = "codex",
    with_preamble: bool = True,
) -> str:
    """Attach the isolated scratch route without truncating the task itself.

    Carried once per run, not on every utterance. Prepending it each time put
    ~120 characters of protocol boilerplate in front of the actual instruction
    on every delegating turn, and that leaked into what was being measured: the
    repair net titled a WorkItem with it (2026-08-01, F1), a synthesised task
    carried it through to the provider which then acted on it (G1), and turns
    about workspace paths were where the model kept dropping `cwd`.

    The wording was A/B'd on 2026-07-31 and cleared; its placement and
    frequency never were.
    """

    text = str(say or "")
    if not with_preamble or "scratch 仓" not in text:
        return text
    return (
        PREAMBLE_VARIANTS[_preamble_variant]
        + f'DELEGATE 标签必须包含 provider="{execution_provider}"，并将 cwd 属性原样设为 '
        f'"{SCRATCH_TARGET.as_posix()}"；task 属性必须完整保留用户要求的操作、'
        "文件名和内容，其中的位置只能表述为“在当前工作目录”，不得在 task 中"
        "重复该绝对路径，因为实际执行目录会被安全地分配为 worktree。"
        + text
    )


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScenarioError(f"{path}: root must be an object")
    return value


def validate_scenario(value: dict[str, Any], source: str = "<memory>") -> dict[str, Any]:
    """Validate and return a schema-v1 scenario without changing it."""

    if value.get("schema") != 1:
        raise ScenarioError(f"{source}: schema must be 1")
    for key in ("id", "category", "notes", "steps"):
        if key not in value:
            raise ScenarioError(f"{source}: missing {key}")
    if not isinstance(value["id"], str) or not value["id"].strip():
        raise ScenarioError(f"{source}: id must be a non-empty string")
    if not isinstance(value["category"], str) or not value["category"].strip():
        raise ScenarioError(f"{source}: category must be a non-empty string")
    if not isinstance(value["notes"], str):
        raise ScenarioError(f"{source}: notes must be a string")
    if not isinstance(value["steps"], list) or not value["steps"]:
        raise ScenarioError(f"{source}: steps must be a non-empty array")
    for index, step in enumerate(value["steps"], 1):
        where = f"{source}: step {index}"
        if not isinstance(step, dict):
            raise ScenarioError(f"{where} must be an object")
        has_say = "say" in step
        has_action = "action" in step
        if has_say == has_action:
            raise ScenarioError(f"{where} must have exactly one of say/action")
        if has_say:
            if not isinstance(step["say"], str) or not step["say"].strip():
                raise ScenarioError(f"{where}: say must be non-empty")
            if step.get("label") not in VALID_LABELS:
                raise ScenarioError(f"{where}: invalid label {step.get('label')!r}")
            if step.get("wait", "chat_complete") not in VALID_WAITS:
                raise ScenarioError(f"{where}: invalid wait {step.get('wait')!r}")
            timeout = step.get("timeout_s", 300)
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ScenarioError(f"{where}: timeout_s must be positive")
            # One utterance can carry two intents ("change a.txt, and by the way
            # how is b going"). `label` covers the actionable half, which the
            # ledger shows; the spoken half leaves no routing trace, so it is
            # asserted against the reply instead. Without this, dropping the
            # answered half was invisible.
            mentions = step.get("expect_reply_mentions", [])
            if not isinstance(mentions, list) or any(
                not isinstance(item, str) or not item.strip() for item in mentions
            ):
                raise ScenarioError(
                    f"{where}: expect_reply_mentions must be non-empty strings"
                )
            accepted = step.get("accept_labels", [])
            if not isinstance(accepted, list) or any(item not in VALID_LABELS - {"ambiguous"} for item in accepted):
                raise ScenarioError(f"{where}: invalid accept_labels")
            if step["label"] == "ambiguous" and not accepted:
                raise ScenarioError(f"{where}: ambiguous requires accept_labels")
            # What the character was given to say about how the task ended.
            # Asserted on the ledger's note rather than the spoken line: the
            # observer rewrites the note into the character's voice, so the
            # spoken text is not stable, while the note is deterministic and is
            # what the voice is derived from.
            narration = step.get("expect_narration")
            if narration is not None:
                if not isinstance(narration, dict):
                    raise ScenarioError(f"{where}: expect_narration must be an object")
                attention = narration.get("attention")
                if attention is not None and not str(attention or "").strip():
                    raise ScenarioError(f"{where}: expect_narration.attention must be a token")
                source = narration.get("summary_from")
                if source is not None and source not in {"assessment", "provider"}:
                    raise ScenarioError(
                        f"{where}: expect_narration.summary_from must be assessment or provider"
                    )
            expected_files = step.get("expect_files", [])
            if not isinstance(expected_files, list):
                raise ScenarioError(f"{where}: expect_files must be an array")
            for expected_file in expected_files:
                if not isinstance(expected_file, dict):
                    raise ScenarioError(f"{where}: expect_files entries must be objects")
                relative_path = str(expected_file.get("path") or "")
                if (
                    not relative_path
                    or Path(relative_path).is_absolute()
                    or ".." in Path(relative_path).parts
                ):
                    raise ScenarioError(
                        f"{where}: expect_files path must stay inside the workspace"
                    )
                if not isinstance(expected_file.get("content"), str):
                    raise ScenarioError(
                        f"{where}: expect_files content must be a string"
                    )
        elif step.get("action") not in VALID_ACTIONS:
            raise ScenarioError(f"{where}: invalid action {step.get('action')!r}")
    return value


def load_scenario(path: Path) -> dict[str, Any]:
    return validate_scenario(_read_json(path), str(path))


# Run the categories that carry the most unexamined contract surface first.
# A campaign that dies partway should leave the findings behind, not the
# confirmations: on 2026-07-31 alphabetical order put `composite` last, so a
# stall after D1 cost both E scenarios — the two that had never run at all —
# while five repeats of the most mature lifecycle cases had already completed.
CATEGORY_RUN_ORDER: tuple[str, ...] = (
    "composite",
    "mixed",
    "reference",
    "transport",
    "lifecycle",
)


def _scenario_run_rank(scenario: dict[str, Any]) -> tuple[int, str]:
    category = str(scenario.get("category") or "")
    try:
        rank = CATEGORY_RUN_ORDER.index(category)
    except ValueError:
        rank = len(CATEGORY_RUN_ORDER)
    return rank, str(scenario.get("id") or "")


def discover_scenarios(*, include_long_silence: bool = False) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    ids: set[str] = set()
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        scenario = load_scenario(path)
        if scenario["id"].startswith("_"):
            continue
        if scenario["id"] in LONG_SILENCE_SCENARIOS and not include_long_silence:
            continue
        if scenario["id"] in ids:
            raise ScenarioError(f"duplicate scenario id: {scenario['id']}")
        ids.add(scenario["id"])
        found.append((path, scenario))
    found.sort(key=lambda item: _scenario_run_rank(item[1]))
    return found


def resolve_scenarios(args: argparse.Namespace) -> list[tuple[Path, dict[str, Any]]]:
    if bool(args.scenario) == bool(args.all):
        raise ScenarioError("select exactly one of --scenario or --all")
    if args.all:
        found = discover_scenarios(include_long_silence=args.long_silence)
        if args.mode == "replay":
            found = [item for item in found if item[1]["id"] not in REAL_ONLY_SCENARIOS]
        return found
    candidate = Path(args.scenario)
    if not candidate.is_file():
        candidate = SCENARIO_DIR / f"{args.scenario}.json"
    if not candidate.is_file():
        raise ScenarioError(f"scenario not found: {args.scenario}")
    scenario = load_scenario(candidate)
    if scenario["id"] in LONG_SILENCE_SCENARIOS and not args.long_silence:
        raise ScenarioError(f"{scenario['id']} requires --long-silence")
    if args.mode == "replay" and scenario["id"] in REAL_ONLY_SCENARIOS:
        raise ScenarioError(
            f"{scenario['id']} is real-only; its fault boundaries cannot be replayed"
        )
    return [(candidate, scenario)]


def _ids(snapshot: dict[str, Any], key: str) -> set[str]:
    return {
        str(row.get({
            "work_items": "work_item_id",
            "attempts": "attempt_id",
            "amendments": "amendment_id",
            "artifacts": "artifact_id",
        }[key]) or "")
        for row in snapshot.get(key, [])
        if isinstance(row, dict)
    } - {""}


def _run_created_belongs_to_current_step(
    event: dict[str, Any],
    before: dict[str, Any],
) -> bool:
    """Reject late/replayed run.created events already present before the step."""

    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    if (
        str(event.get("method") or "") != "provider.event"
        or str(params.get("type") or "") != "run.created"
    ):
        return False
    before_attempts = {
        str(row.get("attempt_id") or "")
        for row in before.get("attempts", [])
        if isinstance(row, dict)
    } - {""}
    before_work_items = _ids(before, "work_items")
    before_run_ids = {
        str(row.get("provider_run_id") or "")
        for row in before.get("attempts", [])
        if isinstance(row, dict)
    } - {""}
    metadata = (
        params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    )
    work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
    attempt_id = str(work.get("attempt_id") or "")
    work_item_id = str(work.get("work_item_id") or "")
    run_id = str(params.get("run_id") or "")
    return not (
        (attempt_id and attempt_id in before_attempts)
        or (work_item_id and work_item_id in before_work_items)
        or (run_id and run_id in before_run_ids)
    )


def _run_created_continues_existing_work(
    event: dict[str, Any],
    before: dict[str, Any],
) -> bool:
    """Recognize an explicitly linked follow-up even when intake creates a new WorkItem."""

    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    metadata = (
        params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    )
    work = metadata.get("work") if isinstance(metadata.get("work"), dict) else {}
    attrs = (
        metadata.get("delegate_attrs")
        if isinstance(metadata.get("delegate_attrs"), dict)
        else {}
    )
    related_id = str(
        metadata.get("related_work_item_id")
        or work.get("workspace_ref")
        or attrs.get("workspace_ref")
        or attrs.get("workspaceRef")
        or ""
    )
    return bool(related_id and related_id in _ids(before, "work_items"))


def _native_steer_continues_active_work(
    event: dict[str, Any],
    before: dict[str, Any],
) -> bool:
    """Recognize same-run continuation without inventing a new Attempt."""

    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    if (
        str(event.get("method") or "") != "provider.event"
        or str(params.get("type") or "").lower() != "run.status"
        or str(payload.get("stage") or "").lower()
        not in {"steer_queued", "steer_applied"}
    ):
        return False
    run_id = str(params.get("run_id") or "")
    return bool(
        run_id
        and any(
            str(row.get("provider_run_id") or "") == run_id
            and str(row.get("execution_status") or "").strip().lower()
            in {"queued", "starting", "running", "active"}
            for row in before.get("attempts", [])
            if isinstance(row, dict)
        )
    )


def _reply_text(events: Iterable[dict[str, Any]], turn_id: str) -> str:
    """The spoken half of the turn.

    `chat.token` carries the accumulated visible text, so the last one for the
    turn is the whole reply. Routing evidence lives in the ledger; whether the
    assistant also *answered* lives only here, and an utterance can ask for
    both at once.
    """

    latest = ""
    for event in events:
        if not isinstance(event, dict) or event.get("method") != "chat.token":
            continue
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        if turn_id and str(params.get("turn_id") or "") != turn_id:
            continue
        # chat.token carries the accumulated visible text, not a delta.
        text = str(params.get("token") or params.get("text") or "")
        if len(text) >= len(latest):
            latest = text
    return latest


def _cancelled_in_step(events: Iterable[dict[str, Any]]) -> bool:
    for event in events:
        if not isinstance(event, dict):
            continue
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        if str(params.get("type") or "") == "run.cancelled":
            return True
        if str(payload.get("status") or "").lower() in {"cancelled", "canceled"}:
            return True
    return False


def extract_observed(step: dict[str, Any]) -> tuple[str, list[str]]:
    """Extract a route in strict evidence priority order; never guess."""

    evidence: list[str] = []
    logs = [str(line) for line in step.get("logs", [])]
    for anchor, route in LOG_ANCHORS:
        matches = [line for line in logs if anchor in line]
        if matches:
            evidence.append(f"log:{anchor}")
            return route, evidence

    before = step.get("ledger_before") if isinstance(step.get("ledger_before"), dict) else {}
    after = step.get("ledger_after") if isinstance(step.get("ledger_after"), dict) else {}
    events = [event for event in step.get("events", []) if isinstance(event, dict)]
    for event in events:
        if _run_created_belongs_to_current_step(event, before):
            if _run_created_continues_existing_work(event, before):
                evidence.append("event:provider.event/run.created:related_work_item")
                return "continue", evidence
            evidence.append("event:provider.event/run.created")
            return "new", evidence
    for event in events:
        if _native_steer_continues_active_work(event, before):
            evidence.append("event:provider.event/run.status:steer")
            return "continue", evidence

    new_items = _ids(after, "work_items") - _ids(before, "work_items")
    new_attempts = _ids(after, "attempts") - _ids(before, "attempts")
    new_amendments = _ids(after, "amendments") - _ids(before, "amendments")
    # A withdrawal stops work without starting any. Requiring "no new rows"
    # keeps this apart from an interrupt that is immediately followed by a
    # fresh instruction, which is a different turn shape entirely.
    if _cancelled_in_step(events) and not (new_items or new_attempts or new_amendments):
        evidence.append("event:run.cancelled")
        return "retract", evidence
    if new_items:
        evidence.append(f"ledger:new_work_item:{sorted(new_items)[0]}")
        return "new", evidence
    if new_amendments or new_attempts:
        item_ids = {
            str(row.get("work_item_id") or "")
            for row in after.get("attempts", []) + after.get("amendments", [])
            if isinstance(row, dict)
            and (
                str(row.get("attempt_id") or "") in new_attempts
                or str(row.get("amendment_id") or "") in new_amendments
            )
        } - {""}
        if item_ids and item_ids <= _ids(before, "work_items"):
            evidence.append(f"ledger:existing_item_activity:{sorted(item_ids)[0]}")
            return "continue", evidence

    has_chat_complete = any(str(event.get("method") or "") == "chat.complete" for event in events)
    routing_ledger_unchanged = bool(before or after) and all(
        _ids(before, key) == _ids(after, key)
        for key in ("work_items", "attempts", "amendments")
    )
    session = step.get("session") if isinstance(step.get("session"), dict) else {}
    branch_marked = bool(session.get("branch_active") or session.get("branch_summary"))
    if has_chat_complete and routing_ledger_unchanged and not branch_marked:
        evidence.extend(
            ("event:chat.complete", "ledger:no_routing_change", "session:no_branch_mark")
        )
        return "chat", evidence
    evidence.append("none:no_conclusive_anchor")
    return "none", evidence


def step_was_host_repaired(step: dict[str, Any]) -> bool:
    """True when a host recovery path carried a delegate omission.

    A repaired delegate produces a ledger row indistinguishable from one the
    model emitted itself, so route extraction alone credits the safety net to
    the model and reports a routing accuracy the model does not have. The host
    logs every repair/resend under stable prefixes precisely so the two can be
    told apart; consuming them here is what makes the omission rate measurable.
    """

    for line in step.get("logs", []):
        text = str(line)
        if "[DELEGATE-REPAIR]" in text and "repaired" in text:
            return True
        if "[DELEGATE-RESEND]" in text and (
            "re-emitted after omission" in text
            or "restored a structured action after omission" in text
        ):
            return True
    return False


def bootstrap_block_is_unrecoverable(
    logs: list[str], ledger_after: dict[str, Any]
) -> bool:
    """True when the ledger is still empty after a `no_allowlisted_project` block.

    With at least one WorkItem the same block is an ordinary routing outcome
    the next turn can recover from; with none, candidate resolution can never
    succeed again in this run.
    """

    if not any("no_allowlisted_project" in str(line) for line in logs):
        return False
    return not (ledger_after.get("work_items") or [])


def _path_is_within(path: str, root: str) -> bool:
    if not path or not root:
        return True
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
        str(Path(right).resolve())
    )


def _workspace_has_scratch_git_origin(
    workspace: str,
    common_dir: str,
    scratch: str,
) -> bool:
    """Accept a scratch worktree or an independent Draft below its fence."""

    if not workspace or not common_dir or not scratch:
        return False
    scratch_common = Path(scratch).resolve() / ".git"
    if _same_path(common_dir, str(scratch_common)):
        return True
    return _path_is_within(workspace, scratch) and _same_path(
        common_dir,
        str(Path(workspace).resolve() / ".git"),
    )


def _git_common_dir(workspace: str) -> str:
    if not workspace or not Path(workspace).is_dir():
        return ""
    result = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    if result.returncode:
        return ""
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = Path(workspace) / common
    return str(common.resolve())


def _provider_terminal_statuses(events: Iterable[dict[str, Any]]) -> set[str]:
    statuses: set[str] = set()
    for event in events:
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
        status = ""
        if method == "provider.result":
            status = str(params.get("status") or "")
        elif method == "provider.event":
            event_type = str(params.get("type") or "").lower()
            if event_type not in {
                "run.finished",
                "run.failed",
                "run.cancelled",
                "run.canceled",
                "run.status",
                "run.completed",
                "run.succeeded",
            }:
                continue
            status = str(payload.get("status") or "")
            if not status and event_type == "run.failed":
                status = "failed"
            elif not status and event_type in {"run.completed", "run.succeeded"}:
                status = "succeeded"
            elif not status and event_type in {"run.cancelled", "run.canceled"}:
                status = "cancelled"
        status = status.lower()
        if status in TERMINAL_PROVIDER_STATUSES:
            statuses.add(status)
    return statuses


def _has_reference_clarification(events: Iterable[dict[str, Any]]) -> bool:
    """Whether this turn stopped at the Host's structured ambiguity boundary."""

    return any(
        method == "chat.observer_decision"
        and str(params.get("source") or "").strip().lower()
        == "reference_clarification"
        for method, params in (_event_parts(event) for event in events)
    )


def _capture_expected_files(
    definition: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    expected_files = definition.get("expect_files", [])
    if not isinstance(expected_files, list) or not expected_files:
        return []
    work_items = [
        row
        for row in snapshot.get("work_items", [])
        if isinstance(row, dict) and row.get("workspace_path")
    ]
    workspace = Path(str(work_items[-1]["workspace_path"])).resolve() if work_items else None
    checks: list[dict[str, Any]] = []
    for expected in expected_files:
        relative_path = str(expected.get("path") or "")
        expected_content = str(expected.get("content") or "")
        target = (workspace / relative_path).resolve() if workspace else None
        inside = bool(target and workspace and _path_is_within(str(target), str(workspace)))
        exists = bool(inside and target and target.is_file())
        actual_content = (
            target.read_text(encoding="utf-8", errors="replace") if exists and target else ""
        )
        checks.append(
            {
                "path": relative_path,
                "workspace_path": str(workspace or ""),
                "inside_workspace": inside,
                "exists": exists,
                "expected_content": expected_content,
                "actual_content": actual_content,
            }
        )
    return checks


def _terminal_notes(*sources: dict[str, Any]) -> list[dict[str, Any]]:
    """Closing notes across the given steps, newest last.

    Deliberately not scoped to one step. `wait: provider_terminal` is satisfied
    by provider.result itself, while the ledger's note waits on an assessment
    that cross-checks the git delta -- so the note lands after the step window
    has closed, and a per-step check could never go green no matter how well
    the system behaved. "Did the ledger narrate this ending" is a fact about
    the run.
    """

    notes: list[dict[str, Any]] = []
    for step in sources:
        for event in step.get("events", []):
            if not isinstance(event, dict):
                continue
            if str(event.get("method")) != "chat.work_note":
                continue
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            metadata = (
                params.get("metadata")
                if isinstance(params.get("metadata"), dict)
                else {}
            )
            keypoint = str(metadata.get("narration_keypoint") or "").lower()
            phase = str(params.get("phase") or "").lower()
            if keypoint == "terminal" or phase == "result":
                notes.append({"params": params, "metadata": metadata})
    return notes


def narration_failures(
    notes: list[dict[str, Any]], expected: dict[str, Any]
) -> list[str]:
    """Check what the character was handed to say about how the task ended.

    Asserted against the ledger's note, not the spoken line: the observer
    rewrites notes into the character's voice, so the spoken text is not
    stable, while the note is deterministic and is what the voice comes from.
    The note carries the assessment's rationale, so "did the assessment do the
    talking" is an equality rather than a substring guess.
    """

    if not notes:
        return ["narration_missing"]
    wanted_attention = str(expected.get("attention") or "").strip().lower()
    source = str(expected.get("summary_from") or "").strip().lower()
    failures: list[str] = []
    if wanted_attention:
        seen = {str(note["metadata"].get("attention") or "").lower() for note in notes}
        if wanted_attention not in seen:
            failures.append("narration_attention_mismatch")
    if source:
        def matches(note: dict[str, Any]) -> bool:
            summary = " ".join(str(note["params"].get("summary") or "").split())
            rationale = " ".join(str(note["metadata"].get("rationale") or "").split())
            if not rationale:
                return False
            return (summary == rationale) if source == "assessment" else (summary != rationale)

        if not any(matches(note) for note in notes):
            failures.append(f"narration_not_from_{source}")
    return failures


def _file_content_differs(actual: str, expected: str) -> bool:
    """Compare file contents without failing on incidental line endings.

    Whether an agent leaves a trailing newline, or whether Windows turns \\n
    into \\r\\n on the way to disk, says nothing about whether the work landed
    — and on 2026-08-01 the real-mode smoke failed on exactly that: smoke.txt
    existed, inside the workspace, holding "routing smoke" against an expected
    "routing smoke\\n". A hard failure means fact-layer pollution, so it must
    not fire on a difference no user could notice. Everything else still has to
    match exactly.
    """

    def normalise(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

    return normalise(actual) != normalise(expected)


def score_recording(recording: dict[str, Any], scenario: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score one recording.  Hard facts and soft route mismatches stay separate."""

    scenario_steps = scenario.get("steps", []) if scenario else []
    execution_mode = str(recording.get("provider_execution") or "real")
    results: list[dict[str, Any]] = []
    hard_failures: list[dict[str, Any]] = []
    say_index = 0
    run_terminal_notes = _terminal_notes(
        *(step for step in recording.get("steps", []) if isinstance(step, dict))
    )
    for position, raw in enumerate(recording.get("steps", []), 1):
        if not isinstance(raw, dict) or "say" not in raw:
            continue
        definition = scenario_steps[position - 1] if position <= len(scenario_steps) else {}
        label = str(raw.get("label") or definition.get("label") or "")
        accepted = list(raw.get("accept_labels") or definition.get("accept_labels") or [])
        observed, evidence = extract_observed(raw)
        host_repaired = step_was_host_repaired(raw)
        if label == "readonly_ref":
            # Answering from ledger facts without emitting DELEGATE is the
            # contract-correct outcome; the extractor sees that as `chat`.
            soft_match = observed == "chat"
        elif accepted:
            # Any label may name more than one acceptable outcome, not just
            # `ambiguous`: a withdrawal is satisfied either by a real
            # cancellation or — when nothing is running — by saying so and
            # starting nothing. Consulting `accept_labels` only for `ambiguous`
            # silently ignored them everywhere else.
            soft_match = observed in accepted or observed == label
        else:
            soft_match = observed == label
        before = raw.get("ledger_before") if isinstance(raw.get("ledger_before"), dict) else {}
        after = raw.get("ledger_after") if isinstance(raw.get("ledger_after"), dict) else {}

        step_hard: list[str] = []
        events = [event for event in raw.get("events", []) if isinstance(event, dict)]
        terminal_statuses = _provider_terminal_statuses(events)
        reference_clarification = _has_reference_clarification(events)
        if reference_clarification and label != "ambiguous":
            step_hard.append("unexpected_reference_clarification")
        expected_cancellation = (
            label == "retract"
            and bool(terminal_statuses)
            and terminal_statuses <= {"cancelled", "canceled"}
        )
        if terminal_statuses & FAILED_PROVIDER_STATUSES and not expected_cancellation:
            step_hard.append("provider_terminal_failed")
        elif (
            definition.get("wait") == "provider_terminal"
            and isinstance(recording.get("paths"), dict)
            and recording["paths"].get("scratch")
            and not reference_clarification
            and not terminal_statuses & SUCCESS_PROVIDER_STATUSES
        ):
            step_hard.append("provider_terminal_not_succeeded")
        if raw.get("delegate_errors"):
            step_hard.append("provider_delegate_error")
        # File evidence is the question "did the work land correctly", which a
        # stubbed coding agent cannot answer and was never asked to. Routing
        # scoring above stays fully in force.
        # Unlike file evidence, this does not need a real executor: the note is
        # produced by the ledger, which is real in both modes. Gating it on the
        # provider was copied from the file check by reflex, and it excluded the
        # one setup that can reach the contradicted ending on demand -- a stub
        # emitting denied tools while exiting 0.
        expected_narration = definition.get("expect_narration")
        if isinstance(expected_narration, dict):
            step_hard.extend(
                narration_failures(run_terminal_notes, expected_narration)
            )
        if (
            definition.get("expect_files")
            and isinstance(recording.get("paths"), dict)
            and recording["paths"].get("scratch")
            and execution_mode != "stub"
        ):
            file_checks = raw.get("file_checks")
            if not isinstance(file_checks, list) or not file_checks:
                step_hard.append("expected_file_evidence_missing")
            else:
                for check in file_checks:
                    if not isinstance(check, dict) or not check.get("inside_workspace"):
                        step_hard.append("expected_file_path_escaped")
                    elif not check.get("exists"):
                        step_hard.append("expected_file_missing")
                    elif _file_content_differs(
                        str(check.get("actual_content") or ""),
                        str(check.get("expected_content") or ""),
                    ):
                        step_hard.append("expected_file_content_mismatch")
        attempts = {
            str(row.get("attempt_id") or ""): row
            for row in after.get("attempts", [])
            if isinstance(row, dict) and row.get("attempt_id")
        }
        workspaces = {
            str(row.get("work_item_id") or ""): str(row.get("workspace_path") or "")
            for row in after.get("work_items", [])
            if isinstance(row, dict)
        }
        paths = recording.get("paths") if isinstance(recording.get("paths"), dict) else {}
        scratch = str(paths.get("scratch") or "")
        if scratch:
            new_attempt_ids = _ids(after, "attempts") - _ids(before, "attempts")
            execution_provider = str(
                recording.get("execution_provider") or "codex"
            ).lower()
            provider_work_item_ids = {
                str(attempt.get("work_item_id") or "")
                for attempt in after.get("attempts", [])
                if isinstance(attempt, dict)
                and str(attempt.get("attempt_id") or "") in new_attempt_ids
                and (
                    str(attempt.get("provider") or "").lower()
                    or str(attempt.get("provider_run_id") or "")
                    .partition("_")[0]
                    .lower()
                )
                == execution_provider
            } - {""}
            for event in events:
                params = event.get("params") if isinstance(event.get("params"), dict) else {}
                if str(params.get("provider") or "").lower() != execution_provider:
                    continue
                metadata = (
                    params.get("metadata")
                    if isinstance(params.get("metadata"), dict)
                    else {}
                )
                work = (
                    metadata.get("work")
                    if isinstance(metadata.get("work"), dict)
                    else {}
                )
                work_item_id = str(work.get("work_item_id") or "")
                if work_item_id:
                    provider_work_item_ids.add(work_item_id)
            for work_item in after.get("work_items", []):
                if (
                    not isinstance(work_item, dict)
                    or str(work_item.get("work_item_id") or "")
                    not in provider_work_item_ids
                    or not work_item.get("workspace_path")
                ):
                    continue
                common_dir = str(work_item.get("git_common_dir") or "")
                if not common_dir:
                    step_hard.append("workspace_origin_unverified")
                elif not _workspace_has_scratch_git_origin(
                    str(work_item.get("workspace_path") or ""),
                    common_dir,
                    scratch,
                ):
                    step_hard.append("workspace_wrong_git_origin")
        for amendment in after.get("amendments", []):
            if not isinstance(amendment, dict):
                continue
            attempt = attempts.get(str(amendment.get("attempt_id") or ""))
            if attempt is None or str(attempt.get("work_item_id") or "") != str(amendment.get("work_item_id") or ""):
                step_hard.append("amendment_lineage_contradiction")
        for artifact in after.get("artifacts", []):
            if not isinstance(artifact, dict):
                continue
            attempt = attempts.get(str(artifact.get("attempt_id") or ""))
            if attempt is None:
                step_hard.append("artifact_attempt_missing")
                continue
            expected_workspace = workspaces.get(str(attempt.get("work_item_id") or ""), "")
            if not _path_is_within(str(artifact.get("workspace_path") or ""), expected_workspace):
                step_hard.append("artifact_wrong_workspace")
        if label in ("chat", "readonly_ref", "retract"):
            # Same invariant for all three: an utterance that is not an
            # instruction must never create work, however the router labelled
            # it. A retraction that spawns work is the worst of the three.
            if _ids(after, "attempts") - _ids(before, "attempts"):
                step_hard.append("chat_created_attempt")
            if _ids(after, "amendments") - _ids(before, "amendments"):
                step_hard.append("chat_created_amendment")
        record_category = str(recording.get("category") or (scenario or {}).get("category") or "")
        if raw.get("timed_out") and record_category == "transport":
            statuses = {
                str(row.get("execution_status") or "")
                for row in after.get("attempts", [])
                if isinstance(row, dict)
            }
            if not (statuses & {"orphaned", "stalled"}):
                step_hard.append("transport_hang_without_orphan_or_stalled")
        recovery = raw.get("recovery") if isinstance(raw.get("recovery"), dict) else {}
        if recovery:
            before_recovery = recovery.get("before") or {}
            after_recovery = recovery.get("after") or {}
            for key in ("workspace_paths", "focus"):
                if before_recovery.get(key) != after_recovery.get(key):
                    step_hard.append(f"restart_lost_{key}")
        # The spoken half of a multi-intent turn leaves no routing trace, so it
        # is checked against the reply instead. Dropping it is a UX failure,
        # not fact-layer pollution: it stays a separate soft signal and must
        # never be folded into the route mismatch rate.
        reply_text = str(raw.get("reply_text") or "")
        reply_gaps = [
            str(mention)
            for mention in (definition.get("expect_reply_mentions") or [])
            if str(mention) not in reply_text
        ]

        step_hard = sorted(set(step_hard))
        for failure in step_hard:
            hard_failures.append({"step": position, "code": failure})
        say_index += 1
        results.append(
            {
                "step": position,
                "say": str(raw.get("say") or definition.get("say") or ""),
                "label": label,
                "accept_labels": accepted,
                "observed": observed,
                "evidence": evidence,
                "soft_match": soft_match,
                "host_repaired": host_repaired,
                "hard_failures": step_hard,
                "reply_gaps": reply_gaps,
            }
        )
    mismatches = sum(not item["soft_match"] for item in results)
    host_repaired_steps = sum(1 for item in results if item["host_repaired"])
    # A step the host had to repair is not evidence that the model routed, so
    # it is excluded from the unaided rate rather than counted as a success.
    model_alone = sum(
        1 for item in results if item["soft_match"] and not item["host_repaired"]
    )
    none_count = sum(item["observed"] == "none" for item in results)
    ambiguous = sum(item["label"] == "ambiguous" for item in results)
    readonly_ref = sum(item["label"] == "readonly_ref" for item in results)
    reply_gap_steps = sum(1 for item in results if item["reply_gaps"])
    return {
        "schema": "amadeus.routing-score.v1",
        "scenario_id": str(recording.get("scenario_id") or (scenario or {}).get("id") or ""),
        "category": str(recording.get("category") or (scenario or {}).get("category") or ""),
        "run_id": str(recording.get("run_id") or ""),
        "provider_execution": execution_mode,
        "status": "failed" if hard_failures else "passed",
        "steps": results,
        "counts": {
            "steps": len(results),
            "mismatches": mismatches,
            "none": none_count,
            "ambiguous": ambiguous,
            "readonly_ref": readonly_ref,
            "reply_gaps": reply_gap_steps,
            "host_repaired": host_repaired_steps,
            "model_alone_matches": model_alone,
        },
        "hard_failures": hard_failures,
    }


def aggregate_scores(scores: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(scores)
    totals = {
        "runs": len(rows),
        "steps": 0,
        "mismatches": 0,
        "none": 0,
        "ambiguous": 0,
        "readonly_ref": 0,
        "reply_gaps": 0,
        "host_repaired": 0,
        "model_alone_matches": 0,
        "hard_failures": 0,
    }
    groups: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {
            "runs": 0,
            "steps": 0,
            "mismatches": 0,
            "none": 0,
            "ambiguous": 0,
            "readonly_ref": 0,
            "reply_gaps": 0,
            "host_repaired": 0,
            "model_alone_matches": 0,
            "hard_failures": 0,
        }
    )
    failures: list[dict[str, Any]] = []
    for score in rows:
        group = groups[(str(score.get("category") or ""), str(score.get("scenario_id") or ""))]
        for target in (totals, group):
            target["steps"] += int(score["counts"]["steps"])
            target["mismatches"] += int(score["counts"]["mismatches"])
            target["none"] += int(score["counts"]["none"])
            target["ambiguous"] += int(score["counts"]["ambiguous"])
            target["readonly_ref"] += int(score["counts"].get("readonly_ref", 0))
            target["reply_gaps"] += int(score["counts"].get("reply_gaps", 0))
            target["host_repaired"] += int(score["counts"].get("host_repaired", 0))
            target["model_alone_matches"] += int(
                score["counts"].get("model_alone_matches", 0)
            )
            target["hard_failures"] += len(score.get("hard_failures", []))
        group["runs"] += 1
        for failure in score.get("hard_failures", []):
            failures.append({"scenario_id": score.get("scenario_id"), "run_id": score.get("run_id"), **failure})
    return {
        "schema": "amadeus.routing-summary.v1",
        "provider_execution": sorted(
            {str(score.get("provider_execution") or "real") for score in rows}
        ),
        "totals": totals,
        "groups": [
            {"category": category, "scenario_id": scenario_id, **counts}
            for (category, scenario_id), counts in sorted(groups.items())
        ],
        "hard_failures": failures,
    }


def render_summary(summary: dict[str, Any], *, mode: str, repeat: int) -> str:
    totals = summary["totals"]
    def rate(part: int, whole: int) -> str:
        return f"{(100.0 * part / whole):.1f}%" if whole else "0.0%"
    lines = [
        "# Routing E2E summary",
        "",
        f"- Mode: `{mode}`",
        (
            "- Provider execution: `"
            + ", ".join(summary.get("provider_execution") or ["real"])
            + "` (stub = routing measured, work not executed; no file evidence)"
        ),
        f"- Repeat: `{repeat}`",
        f"- Runs: {totals['runs']}",
        f"- Scored utterances: {totals['steps']}",
        f"- Soft mismatch rate: {rate(totals['mismatches'], totals['steps'])}",
        f"- `none` rate: {rate(totals['none'], totals['steps'])}",
        f"- Ambiguous share: {rate(totals['ambiguous'], totals['steps'])}",
        f"- Read-only reference share: {rate(totals.get('readonly_ref', 0), totals['steps'])}",
        f"- Dropped spoken half: {totals.get('reply_gaps', 0)} step(s) "
        "(a multi-intent turn that routed correctly but never answered)",
        # Accepted counts every step the ledger shows routed correctly; unaided
        # excludes the ones the host had to repair. The gap between them is the
        # omission rate the FROZEN keyword tables are currently absorbing, and
        # the number their retirement has to be argued against.
        f"- Accepted (incl. host repair): "
        f"{rate(totals['steps'] - totals['mismatches'], totals['steps'])}",
        f"- Accepted by the model alone: "
        f"{rate(totals.get('model_alone_matches', 0), totals['steps'])} "
        f"({totals.get('host_repaired', 0)} step(s) carried by DELEGATE-REPAIR)",
        f"- Hard failures: {totals['hard_failures']}",
        "",
        "| Category | Scenario | Runs | Steps | Mismatch | None | Ambiguous | Repaired | Hard |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for group in summary["groups"]:
        lines.append(
            f"| {group['category']} | {group['scenario_id']} | {group['runs']} | {group['steps']} | "
            f"{rate(group['mismatches'], group['steps'])} | {rate(group['none'], group['steps'])} | "
            f"{rate(group['ambiguous'], group['steps'])} | {group.get('host_repaired', 0)} | "
            f"{group['hard_failures']} |"
        )
    lines.extend(("", "## Hard failure details", ""))
    if summary["hard_failures"]:
        lines.extend(
            f"- `{item['scenario_id']}` / `{item['run_id']}` step {item['step']}: `{item['code']}`"
            for item in summary["hard_failures"]
        )
    else:
        lines.append("- None.")
    lines.extend(("", "## Skipped real runs", ""))
    skipped = summary.get("skipped", [])
    if skipped:
        lines.extend(f"- `{item['scenario_id']}`: {item['reason']}" for item in skipped)
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def load_recordings(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_no}: recording must be an object")
        rows.append(value)
    return rows


def snapshot_ledger_readonly(path: Path) -> dict[str, Any]:
    """Read the isolated ledger through SQLite URI read-only mode."""

    empty = {"work_items": [], "attempts": [], "amendments": [], "artifacts": [], "focus": {}}
    if not path.is_file():
        return empty
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    try:
        work_items = [
            {
                **dict(row),
                "git_common_dir": _git_common_dir(str(row["workspace_path"] or "")),
            }
            for row in connection.execute(
                "SELECT work_item_id, workspace_path, project_id, state FROM work_items ORDER BY created_at"
            )
        ]
        attempt_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(run_attempts)")
        }
        provider_projection = "provider" if "provider" in attempt_columns else "'' AS provider"
        attempts = [
            dict(row)
            for row in connection.execute(
                "SELECT attempt_id, work_item_id, attempt_number, execution_status, "
                f"provider_run_id, {provider_projection} "
                "FROM run_attempts ORDER BY created_at"
            )
        ]
        workspace_by_item = {row["work_item_id"]: row["workspace_path"] for row in work_items}
        artifacts = [
            {
                **dict(row),
                "workspace_path": str(
                    Path(workspace_by_item.get(row["work_item_id"], "")) / str(row["path"] or "")
                    if row["path"] and not Path(str(row["path"])).is_absolute()
                    else row["path"] or ""
                ),
            }
            for row in connection.execute(
                "SELECT artifact_id, attempt_id, work_item_id, path FROM artifacts ORDER BY created_at"
            )
        ]
        focus_rows = [dict(row) for row in connection.execute("SELECT surface, work_item_id, mode FROM focus_slots")]
    finally:
        connection.close()
    # WorkLedger represents a follow-up as a later attempt; expose a derived,
    # read-only amendment view so the scorer can test lineage uniformly.
    amendments = [
        {
            "amendment_id": f"attempt:{row['attempt_id']}",
            "attempt_id": row["attempt_id"],
            "work_item_id": row["work_item_id"],
        }
        for row in attempts
        if int(row.get("attempt_number") or 0) > 1
    ]
    return {
        "work_items": work_items,
        "attempts": attempts,
        "amendments": amendments,
        "artifacts": artifacts,
        "focus": {row["surface"]: {"work_item_id": row["work_item_id"], "mode": row["mode"]} for row in focus_rows},
    }


def _cleanup_scratch_worktrees() -> list[str]:
    """Remove only linked worktrees whose common repository is the test scratch."""

    if not (SCRATCH_TARGET / ".git").exists():
        return []
    listed = subprocess.run(
        ["git", "-C", str(SCRATCH_TARGET), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if listed.returncode:
        return []
    removed: list[str] = []
    for line in listed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = line[len("worktree ") :].strip()
        if _same_path(candidate, str(SCRATCH_TARGET)):
            continue
        if not _same_path(_git_common_dir(candidate), str(SCRATCH_TARGET / ".git")):
            continue
        result = subprocess.run(
            [
                "git",
                "-C",
                str(SCRATCH_TARGET),
                "worktree",
                "remove",
                "--force",
                candidate,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if result.returncode == 0:
            removed.append(candidate)
    subprocess.run(
        ["git", "-C", str(SCRATCH_TARGET), "worktree", "prune"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return removed


def _cleanup_nested_scratch_repositories(recording: dict[str, Any]) -> list[str]:
    """Remove only independent Draft repositories created inside the test root."""

    scratch = SCRATCH_TARGET.resolve()
    candidates: set[Path] = set()
    for step in recording.get("steps", []):
        if not isinstance(step, dict):
            continue
        ledger = step.get("ledger_after")
        if not isinstance(ledger, dict):
            continue
        for item in ledger.get("work_items", []):
            if not isinstance(item, dict):
                continue
            raw = str(item.get("workspace_path") or "").strip()
            if not raw:
                continue
            candidate = Path(raw).resolve()
            if candidate == scratch or not _path_is_within(str(candidate), str(scratch)):
                continue
            if _same_path(_git_common_dir(str(candidate)), str(candidate / ".git")):
                candidates.add(candidate)
    removed: list[str] = []
    for candidate in sorted(candidates, key=lambda path: len(path.parts), reverse=True):
        if not candidate.is_dir():
            continue

        def clear_readonly(function: Any, path: str, _error: Any) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(candidate, onerror=clear_readonly)
        removed.append(str(candidate))
    return removed


def _ensure_real_mode_capacity() -> None:
    worktree_home = Path.home() / ".21st"
    probe = worktree_home if worktree_home.exists() else Path.home()
    free = shutil.disk_usage(probe).free
    if free < MIN_REAL_MODE_FREE_BYTES:
        raise RuntimeError(
            "real mode refused: worktree volume has "
            f"{free / 1024**3:.2f} GiB free; at least "
            f"{MIN_REAL_MODE_FREE_BYTES / 1024**3:.0f} GiB is required"
        )


def _safe_rebuild_scratch() -> None:
    runtime = (ROOT / "runtime").resolve()
    target = SCRATCH_TARGET.resolve()
    target.relative_to(runtime)
    _cleanup_scratch_worktrees()
    if target.exists():
        def clear_readonly(function: Any, path: str, _error: Any) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(target, onerror=clear_readonly)
    target.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "Amadeus E2E"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "e2e@invalid.local"], cwd=target, check=True)
    (target / "README.md").write_text("# Isolated routing target\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=target, check=True)


def _seed_scenario_fixture(scenario: dict[str, Any]) -> None:
    """Overlay bounded scenario-owned text fixtures on the disposable repo."""

    raw = scenario.get("fixture_files")
    if not isinstance(raw, dict) or not raw:
        return
    root = SCRATCH_TARGET.resolve()
    for relative, content in raw.items():
        candidate = (root / str(relative or "")).resolve()
        candidate.relative_to(root)
        if not isinstance(content, str):
            raise ScenarioError(f"fixture file {relative!r} must contain text")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "scenario fixture"],
        cwd=root,
        check=True,
    )


def _server_env(isolation: Path, *, execution_provider: str = "codex") -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AMADEUS_HEADLESS": "1",
            "AMADEUS_E2E_NO_TTS": "1",
            "AMADEUS_PRE_TRANSLATION_ENABLED": "0",
            # Named explicitly rather than relying on the backend writing a
            # relative "server.log" into its cwd: that reliance is what let the
            # test suite and a live session share one file (2026-08-02).
            "AMADEUS_SERVER_LOG": str(isolation / "server.log"),
            "AMADEUS_SESSION_DIR": str(isolation / "sessions"),
            "AMADEUS_WORK_LEDGER_PATH": str(isolation / "work_ledger.sqlite3"),
            "WORK_PROJECT_ALLOWLIST": str(SCRATCH_TARGET),
            # Session Drafts are independent repositories, not worktrees of
            # the named target.  Fence them below the same disposable,
            # registered root instead of the developer's shared scratch dir.
            # The registered fixture repository is a Project. Scratch is a
            # container and is intentionally excluded from the Project
            # catalog, so using the same path for both makes the fixture
            # invisible to ControlDecision and turns every valid file request
            # into a zero-candidate reference failure.
            "WORK_SCRATCH_ROOT": str(SCRATCH_TARGET / "_drafts"),
            "WORK_WORKTREE_ISOLATION": "1",
            "CODEX_APP_SERVER_PROVIDER_ENABLED": "1",
            "CODEX_APP_SERVER_APPROVAL_MODE": "host",
            "DIRECT_CODEX_PROVIDER_ENABLED": "0",
            "VTS_ENABLED": "0",
            "WAKE_ENABLED": "0",
            "AEC_REALTIME_ENABLED": "0",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        }
    )
    return env


def _snapshot_sessions(session_dir: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    branch_active = False
    branch_summary = False
    if session_dir.is_dir():
        for path in sorted(session_dir.rglob("*.json")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                value = json.loads(text)
            except (OSError, json.JSONDecodeError):
                continue
            files.append({"path": str(path), "value": value})
            lowered = text.lower()
            branch_active = branch_active or '"branch_active": true' in lowered
            branch_summary = branch_summary or "[branch_summary]" in lowered
    return {
        "files": files,
        "branch_active": branch_active,
        "branch_summary": branch_summary,
    }


async def _real_recording(
    scenario: dict[str, Any],
    report_dir: Path,
    *,
    chat_provider: str,
    execution_provider: str = "codex",
) -> dict[str, Any]:
    from e2e_real_work_conversation import (
        WsProbe,
        _free_port,
        _stop_server,
        _wait_for_health,
    )

    from e2e_direct_codex_conversation import _sdk_preflight

    _sdk_preflight()
    _safe_rebuild_scratch()
    _seed_scenario_fixture(scenario)
    run_id = f"{scenario['id']}_{_utc_stamp()}_{uuid.uuid4().hex[:6]}"
    isolation = ROOT / "runtime" / "e2e_routing_runs" / run_id
    isolation.mkdir(parents=True)
    execution_projects: list[dict[str, Any]] = []
    ledger = isolation / "work_ledger.sqlite3"
    stdout_path = isolation / "backend.stdout.log"
    server_log = isolation / "server.log"
    session_id = f"routing-e2e-{uuid.uuid4().hex}"
    if bool(scenario.get("bind_scratch_project")):
        from agent_host.work_ledger_store import WorkLedgerStore

        with WorkLedgerStore(ledger) as store:
            project = store.create_or_get_project(
                SCRATCH_TARGET,
                name="Amadeus role-contract E2E fixture",
            )
            store.bind_conversation(
                session_id,
                project.project_id,
                metadata={"source": "codex_role_contract_experiment"},
            )
    process: subprocess.Popen[Any] | None = None
    handle: Any = None
    port = 0

    async def start() -> tuple[subprocess.Popen[Any], Any, int]:
        next_port = _free_port()
        output = stdout_path.open("a", encoding="utf-8", newline="\n")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        child_env = _server_env(isolation, execution_provider=execution_provider)
        if bool(scenario.get("role_trace")):
            child_env["AMADEUS_E2E_ROLE_TRACE"] = "1"
        child = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "server.app", "--port", str(next_port)],
            cwd=isolation,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        await _wait_for_health(next_port, child)
        ready_deadline = time.monotonic() + 120.0
        while time.monotonic() < ready_deadline:
            if child.poll() is not None:
                raise RuntimeError(
                    f"isolated backend exited during readiness with code {child.returncode}"
                )
            if server_log.is_file() and "backend server ready" in server_log.read_text(
                encoding="utf-8", errors="replace"
            ):
                break
            await asyncio.sleep(0.25)
        else:
            raise TimeoutError("isolated backend health was ok but chat runtime never became ready")
        return child, output, next_port

    def log_delta(offset: int) -> tuple[list[str], int]:
        if not server_log.is_file():
            return [], offset
        text = server_log.read_text(encoding="utf-8", errors="replace")
        return text[offset:].splitlines(), len(text)

    async def guarded_wait(awaitable, *, offset: int, poll_s: float = 1.0):
        """Await a step, but abandon it the moment the log shows an outage.

        Without this, a dead endpoint is indistinguishable from a silent
        provider and the step simply burns its whole timeout.
        """

        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=poll_s)
                if done:
                    return task.result()
                reason = infrastructure_error(log_delta(offset)[0])
                if reason:
                    raise InfrastructureError(reason)
        finally:
            if not task.done():
                task.cancel()

    async def wait_current_provider(
        probe: Any,
        *,
        after: int,
        timeout: float,
        description: str,
        wait: str,
        known_run_ids: set[str],
        active_run_ids: set[str],
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _run_id, satisfied = _provider_wait_state(
                probe.state.events,
                after=after,
                wait=wait,
                known_run_ids=known_run_ids,
                active_run_ids=active_run_ids,
            )
            if satisfied:
                return
            if server_log.is_file():
                text = server_log.read_text(encoding="utf-8", errors="replace")
                failure_lines = [
                    line.strip()
                    for line in text.splitlines()
                    if "worktree isolation is enabled but workspace ensure failed" in line
                ]
                if failure_lines:
                    return
            if probe.reader_task is not None and probe.reader_task.done():
                raise ConnectionError(
                    f"backend WebSocket closed while waiting for {description}"
                )
            await asyncio.sleep(0.25)
        raise TimeoutError(f"timed out waiting for {description}")

    recording: dict[str, Any] = {
        "schema": "amadeus.routing-recording.v1",
        "scenario_id": scenario["id"],
        "category": scenario["category"],
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "chat_provider": chat_provider,
        "preamble_variant": current_preamble_variant(),
        "provider_execution": "real",
        "execution_provider": execution_provider,
        "execution_provider_projects": execution_projects,
        "paths": {
            "isolation": str(isolation),
            "scratch": str(SCRATCH_TARGET),
            "backend_stdout_stderr": str(stdout_path),
            "server_log": str(server_log),
        },
        "steps": [],
    }
    log_offset = 0
    pending_recovery: dict[str, Any] | None = None
    # The routing contract is established once. Repeating it on every utterance
    # is what put it inside titles, synthesised tasks and the model's attention
    # budget for the whole run.
    preamble_sent = False
    try:
        process, handle, port = await start()
        probe = WsProbe(f"ws://127.0.0.1:{port}/ws")
        await probe.__aenter__()
        try:
            for index, definition in enumerate(scenario["steps"], 1):
                if "action" in definition:
                    action = definition["action"]
                    # Actions are where the slow consequences land: a `sleep`
                    # exists precisely to let the ledger finish assessing and
                    # narrate, and a cancel completes seconds after the tag.
                    # Recording only the say steps made all of that invisible,
                    # so checks on it could never pass however well the system
                    # behaved.
                    action_event_start = len(probe.state.events)
                    action_row: dict[str, Any] = {"step": index, "action": action}
                    if action == "snapshot_ledger":
                        action_row["ledger"] = snapshot_ledger_readonly(ledger)
                    elif action == "ws_interrupt":
                        action_row["response"] = await probe.request("tts.interrupt", {})
                    elif action == "sleep":
                        action_row["seconds"] = float(definition.get("seconds", 1))
                        await asyncio.sleep(action_row["seconds"])
                    elif action == "kill_provider":
                        try:
                            import psutil
                            killed: list[int] = []
                            for child in psutil.Process(process.pid).children(recursive=True):
                                command = " ".join(child.cmdline()).lower()
                                matches = "codex" in command and "app-server" in command
                                if matches:
                                    child.kill()
                                    killed.append(child.pid)
                            action_row["killed_pids"] = killed
                            if not killed:
                                action_row["warning"] = (
                                    f"no {execution_provider} provider child was found"
                                )
                        except Exception as exc:
                            action_row["error"] = f"{type(exc).__name__}: {exc}"
                    elif action == "restart_backend":
                        before = snapshot_ledger_readonly(ledger)
                        await probe.__aexit__(None, None, None)
                        await _stop_server(port, process)
                        handle.close()
                        process, handle, port = await start()
                        probe = WsProbe(f"ws://127.0.0.1:{port}/ws")
                        await probe.__aenter__()
                        after = snapshot_ledger_readonly(ledger)
                        action_row["recovery"] = {
                            "before": {
                                "workspace_paths": sorted(row["workspace_path"] for row in before["work_items"]),
                                "focus": before["focus"],
                            },
                            "after": {
                                "workspace_paths": sorted(row["workspace_path"] for row in after["work_items"]),
                                "focus": after["focus"],
                            },
                        }
                        pending_recovery = action_row["recovery"]
                    action_row["events"] = [
                        event.to_dict() for event in probe.state.events[action_event_start:]
                    ]
                    recording["steps"].append(action_row)
                    continue

                before = snapshot_ledger_readonly(ledger)
                event_start = len(probe.state.events)
                turn_id = f"{run_id}-{index}"
                turn_sent_elapsed = time.monotonic() - probe.state.started_at
                sent_text = (
                    str(definition["say"])
                    if bool(scenario.get("raw_utterances"))
                    else _prepare_real_utterance(
                        definition["say"],
                        execution_provider=execution_provider,
                        with_preamble=not preamble_sent,
                    )
                )
                if sent_text != definition["say"]:
                    preamble_sent = True
                await probe.request(
                    "chat.send",
                    {
                        "text": sent_text,
                        "provider": chat_provider,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "source": "e2e_routing_matrix",
                    },
                )
                timed_out = False
                wait = definition.get("wait", "chat_complete")
                timeout = float(definition.get("timeout_s", 300))
                try:
                    if wait == "chat_complete":
                        await guarded_wait(
                            probe.wait_event(
                                lambda event: event.method == "chat.complete" and event.params.get("turn_id") == turn_id,
                                timeout=timeout,
                                after=event_start,
                                description=f"{scenario['id']} step {index} chat.complete",
                            ),
                            offset=log_offset,
                        )
                    elif wait in {"provider_terminal", "work_note"}:
                        await guarded_wait(
                            wait_current_provider(
                                probe,
                                after=event_start,
                                timeout=timeout,
                                description=f"{scenario['id']} step {index} {wait}",
                                wait=wait,
                                known_run_ids={
                                    str(row.get("provider_run_id") or "")
                                    for row in before.get("attempts", [])
                                    if isinstance(row, dict)
                                }
                                - {""},
                                active_run_ids={
                                    str(row.get("provider_run_id") or "")
                                    for row in before.get("attempts", [])
                                    if isinstance(row, dict)
                                    and str(row.get("execution_status") or "")
                                    .strip()
                                    .lower()
                                    in {"queued", "starting", "running", "active"}
                                }
                                - {""},
                            ),
                            offset=log_offset,
                        )
                except TimeoutError:
                    timed_out = True
                await asyncio.sleep(0.2)
                logs, log_offset = log_delta(log_offset)
                delegate_errors = [
                    line.strip()
                    for line in logs
                    if "provider delegate failed" in line
                    or "worktree isolation is enabled but workspace ensure failed" in line
                ]
                after = snapshot_ledger_readonly(ledger)
                events = [event.to_dict() for event in probe.state.events[event_start:]]
                raw_role_chunks: list[str] = []
                raw_marker = f"[ROLE-RAW] turn_id={turn_id} chunk="
                for line in logs:
                    marker_at = line.find(raw_marker)
                    if marker_at < 0:
                        continue
                    encoded = line[marker_at + len(raw_marker) :].strip()
                    try:
                        raw_role_chunks.append(str(json.loads(encoded)))
                    except json.JSONDecodeError:
                        continue

                def latency_ms(method: str, *, complete_sentence: bool = False) -> float | None:
                    for event in events:
                        if str(event.get("method") or "") != method:
                            continue
                        params = event.get("params") if isinstance(event.get("params"), dict) else {}
                        if str(params.get("turn_id") or "") != turn_id:
                            continue
                        if method == "chat.token":
                            token = str(params.get("token") or "")
                            if not token.strip():
                                continue
                            if complete_sentence and not any(mark in token for mark in "。！？!?"):
                                continue
                        return round(
                            max(0.0, float(event.get("elapsed_s") or 0.0) - turn_sent_elapsed)
                            * 1000.0,
                            1,
                        )
                    return None

                delegate_start_ms: float | None = None
                for event in events:
                    if str(event.get("method") or "") != "provider.event":
                        continue
                    params = event.get("params") if isinstance(event.get("params"), dict) else {}
                    nested = params.get("event") if isinstance(params.get("event"), dict) else params
                    if str(nested.get("type") or "") != "run.created":
                        continue
                    delegate_start_ms = round(
                        max(0.0, float(event.get("elapsed_s") or 0.0) - turn_sent_elapsed)
                        * 1000.0,
                        1,
                    )
                    break
                recording["steps"].append(
                    {
                        "step": index,
                        "turn_id": turn_id,
                        "say": sent_text,
                        "script_say": definition["say"],
                        "label": definition["label"],
                        "accept_labels": definition.get("accept_labels", []),
                        "logs": logs,
                        "events": events,
                        "ledger_before": before,
                        "ledger_after": after,
                        "session": _snapshot_sessions(isolation / "sessions"),
                        "reply_text": _reply_text(events, turn_id),
                        "raw_role_text": "".join(raw_role_chunks),
                        "first_visible_ms": latency_ms("chat.token"),
                        "first_sentence_ms": latency_ms(
                            "chat.token", complete_sentence=True
                        ),
                        "turn_total_ms": latency_ms("chat.complete"),
                        "delegate_start_ms": delegate_start_ms,
                        "timed_out": timed_out,
                        "delegate_errors": delegate_errors,
                        "file_checks": _capture_expected_files(definition, after),
                        **({"recovery": pending_recovery} if pending_recovery else {}),
                    }
                )
                pending_recovery = None
                if bootstrap_block_is_unrecoverable(logs, after):
                    raise BootstrapUnrecoverable(
                        f"step {index}: first delegate blocked with an empty ledger; "
                        "no later turn in this run can resolve a workspace"
                    )
        finally:
            await probe.__aexit__(None, None, None)
    finally:
        if process is not None:
            await _stop_server(port, process)
        if handle is not None:
            handle.close()
        recording["cleanup"] = {
            "removed_scratch_worktrees": _cleanup_scratch_worktrees(),
            "removed_nested_drafts": _cleanup_nested_scratch_repositories(recording),
        }
        recording["finished_at"] = datetime.now(timezone.utc).isoformat()
    return recording


def _fixture_for_scenario(scenario_id: str) -> dict[str, Any] | None:
    for row in load_recordings(FIXTURE_DIR / "routing_matrix_replay.jsonl"):
        if row.get("scenario_id") == scenario_id:
            return row
    return None


def _j6_evidence_facts(
    recording: dict[str, Any],
    score: dict[str, Any],
) -> tuple[dict[str, bool], dict[str, str], dict[str, Any]]:
    """Extract J6 facts without asking another model to judge the run."""

    steps = [step for step in recording.get("steps", []) if isinstance(step, dict)]
    actions = [step for step in steps if step.get("action")]
    say_steps = [step for step in steps if step.get("say")]
    observation_rows = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("action") == "sleep" and float(step.get("seconds") or 0) >= 90
    ]
    liveness_keypoints: list[str] = []
    liveness_budget_covered = False
    if observation_rows:
        observation_index, observation = observation_rows[0]
        duration = float(observation.get("seconds") or 0)
        prior_elapsed = [
            float(event.get("elapsed_s") or 0)
            for step in steps[:observation_index]
            for event in step.get("events", [])
            if isinstance(event, dict)
        ]
        observation_start = max(prior_elapsed, default=0.0)
        observation_end = observation_start + duration
        checkpoints = [observation_start, observation_end]
        for event in observation.get("events", []):
            if not isinstance(event, dict) or str(event.get("method") or "") != "chat.work_note":
                continue
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            metadata = (
                params.get("metadata")
                if isinstance(params.get("metadata"), dict)
                else {}
            )
            keypoint = str(metadata.get("narration_keypoint") or "").strip().lower()
            if keypoint not in {"semantic_progress", "quiet_monitoring", "stalled"}:
                continue
            elapsed = float(event.get("elapsed_s") or 0)
            if observation_start <= elapsed <= observation_end:
                checkpoints.append(elapsed)
                liveness_keypoints.append(keypoint)
        checkpoints.sort()
        liveness_budget_covered = bool(liveness_keypoints) and max(
            later - earlier for earlier, later in zip(checkpoints, checkpoints[1:])
        ) <= 120
    killed = [
        int(pid)
        for step in actions
        if step.get("action") == "kill_provider"
        for pid in step.get("killed_pids", [])
    ]
    restart_rows = [
        step.get("recovery")
        for step in actions
        if step.get("action") == "restart_backend"
        and isinstance(step.get("recovery"), dict)
    ]
    restart_preserved = bool(restart_rows) and all(
        recovery.get("before", {}).get("workspace_paths")
        == recovery.get("after", {}).get("workspace_paths")
        and recovery.get("before", {}).get("focus")
        == recovery.get("after", {}).get("focus")
        for recovery in restart_rows
    )
    final = say_steps[-1] if say_steps else {}
    final_before = final.get("ledger_before") if isinstance(final.get("ledger_before"), dict) else {}
    final_after = final.get("ledger_after") if isinstance(final.get("ledger_after"), dict) else {}
    before_items = _ids(final_before, "work_items")
    after_items = _ids(final_after, "work_items")
    before_attempts = _ids(final_before, "attempts")
    after_attempts = _ids(final_after, "attempts")
    final_statuses = _provider_terminal_statuses(
        [event for event in final.get("events", []) if isinstance(event, dict)]
    )
    file_checks = [
        item for item in final.get("file_checks", []) if isinstance(item, dict)
    ]
    files_ok = bool(file_checks) and all(
        item.get("inside_workspace") is True
        and item.get("exists") is True
        and not _file_content_differs(
            str(item.get("actual_content") or ""),
            str(item.get("expected_content") or ""),
        )
        for item in file_checks
    )
    post_kill_queries = [
        step
        for step in say_steps
        if "中断后" in str(step.get("script_say") or step.get("say") or "")
    ]
    post_kill_not_success = bool(post_kill_queries) and all(
        all(
            str(row.get("execution_status") or "").lower()
            not in SUCCESS_PROVIDER_STATUSES
            for row in step.get("ledger_after", {}).get("attempts", [])
            if isinstance(row, dict)
        )
        for step in post_kill_queries
    )
    readonly_rows = [row for row in score.get("steps", []) if row.get("label") == "readonly_ref"]
    checks = {
        "real_provider_execution_not_stubbed": recording.get("provider_execution") == "real",
        "semantic_liveness_budget_was_covered": liveness_budget_covered,
        "provider_process_was_actually_killed": bool(killed),
        "post_kill_status_did_not_claim_success": post_kill_not_success,
        "backend_restart_preserved_workspace_and_focus": restart_preserved,
        "status_queries_created_no_work": len(readonly_rows) >= 3
        and all(not row.get("hard_failures") for row in readonly_rows),
        "retry_reused_work_item_and_created_one_attempt": bool(before_items)
        and before_items == after_items
        and len(after_attempts - before_attempts) == 1,
        "retry_reached_real_success_boundary": bool(
            final_statuses & SUCCESS_PROVIDER_STATUSES
        ),
        "recovered_artifact_matches_contract": files_ok,
        "routing_and_fact_scoring_had_no_hard_failure": not score.get("hard_failures"),
        "routing_labels_had_no_mismatch": int(score.get("counts", {}).get("mismatches") or 0)
        == 0,
    }
    artifact_hashes = {
        str(item.get("path") or ""): hashlib.sha256(
            str(item.get("actual_content") or "").encode("utf-8")
        ).hexdigest()
        for item in file_checks
        if item.get("exists")
    }
    ledger_ids = {
        "work_item_ids": sorted(after_items),
        "attempt_ids": sorted(after_attempts),
        "killed_pids": killed,
        "liveness_keypoints": liveness_keypoints,
    }
    return checks, artifact_hashes, ledger_ids


async def run(args: argparse.Namespace) -> int:
    if not 1 <= args.repeat <= 30:
        raise ScenarioError("--repeat must be between 1 and 30")
    if args.fuzz:
        needed = ("ROUTING_FUZZ_LLM_URL", "ROUTING_FUZZ_LLM_MODEL", "ROUTING_FUZZ_LLM_API_KEY")
        missing = [key for key in needed if not os.environ.get(key)]
        if missing:
            raise ScenarioError("--fuzz refused: missing " + ", ".join(missing))
        raise ScenarioError("--fuzz is intentionally deferred in testbed v1")
    if args.mode == "real":
        _ensure_real_mode_capacity()
    set_preamble_variant(getattr(args, "preamble", "permissive"))

    selected = resolve_scenarios(args)
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    scores: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    semantic_failures: list[dict[str, Any]] = []
    aborted: str = ""
    for _path, scenario in selected:
        if aborted:
            break
        for repeat_index in range(1, args.repeat + 1):
            if args.mode == "replay":
                recording = _fixture_for_scenario(scenario["id"])
                if recording is None:
                    raise RuntimeError(f"no replay fixture for {scenario['id']}")
                recording = json.loads(json.dumps(recording))
                recording["run_id"] = f"replay-{scenario['id']}-{repeat_index}"
            else:
                try:
                    recording = await _real_recording(
                        scenario,
                        report_dir,
                        chat_provider=args.provider,
                        execution_provider=args.execution_provider,
                    )
                except InfrastructureError as exc:
                    # Every remaining repeat would fail the same way, so stop
                    # the campaign instead of grinding through it and reporting
                    # an outage as routing evidence.
                    skipped.append(
                        {
                            "scenario_id": scenario["id"],
                            "reason": f"infrastructure: {exc}",
                        }
                    )
                    aborted = f"{exc}"
                    break
                except BootstrapUnrecoverable as exc:
                    # Per-run variance, not an outage: skip this repeat and let
                    # the next one try rather than grinding out a run whose
                    # every remaining turn is refused for the same reason.
                    skipped.append(
                        {
                            "scenario_id": scenario["id"],
                            "reason": f"bootstrap: {exc}",
                        }
                    )
                    continue
                except Exception as exc:
                    skipped.append(
                        {
                            "scenario_id": scenario["id"],
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
            score = score_recording(recording, scenario)
            scores.append(score)
            output = report_dir / f"{score['run_id']}.jsonl"
            payload = {
                "schema": "amadeus.routing-run-report.v1",
                "recording": recording,
                "score": score,
            }
            if (
                scenario["id"] == "J6_failure_recovery_journey"
                and args.mode == "real"
            ):
                checks, artifact_hashes, ledger_ids = _j6_evidence_facts(
                    recording, score
                )
                semantic_status = "passed" if all(checks.values()) else "failed"
                if semantic_status != "passed":
                    semantic_failures.append(
                        {
                            "journey_id": "J6",
                            "run_id": str(score.get("run_id") or ""),
                            "failed_assertions": [
                                name for name, passed in checks.items() if not passed
                            ],
                        }
                    )
                evidence = build_evidence(
                    root=ROOT,
                    journey_id="J6",
                    status=semantic_status,
                    test_level="L3",
                    provider=str(recording.get("execution_provider") or "codex"),
                    model=str(args.provider),
                    report_path=output,
                    isolation_root=str(recording.get("paths", {}).get("isolation") or ""),
                    checks=checks,
                    started_at=str(recording.get("started_at") or ""),
                    finished_at=str(recording.get("finished_at") or ""),
                    artifact_hashes=artifact_hashes,
                    ledger_ids=ledger_ids,
                    manual_acceptance="pending",
                    notes=(
                        "GUI responsiveness and spoken recovery wording remain L4.",
                    ),
                )
                payload["semantic_evidence"] = evidence
                write_evidence(
                    report_dir / f"{score['run_id']}.semantic-evidence.json",
                    evidence,
                )
            output.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = aggregate_scores(scores)
    summary["skipped"] = skipped
    summary["semantic_journey_failures"] = semantic_failures
    if aborted:
        summary["aborted"] = aborted
    summary_path = report_dir / "summary.md"
    summary_path.write_text(render_summary(summary, mode=args.mode, repeat=args.repeat), encoding="utf-8")
    manifest = report_dir / "summary.json"
    manifest.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"routing testbed: {summary['totals']['runs']} run(s), {summary['totals']['hard_failures']} hard failure(s)")
    print(f"summary: {summary_path}")
    for item in skipped:
        print(f"SKIPPED {item['scenario_id']}: {item['reason']}")
    if aborted:
        print(f"ABORTED: {aborted} — no routing conclusion can be drawn from this campaign")
    return 1 if summary["totals"]["hard_failures"] or semantic_failures or skipped else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--scenario", help="scenario id or JSON path")
    selection.add_argument("--all", action="store_true", help="run all non-smoke scenarios")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--mode", choices=("real", "replay"), default="replay")
    parser.add_argument("--provider", default=os.environ.get("LLM_PROVIDER", "deepseek"))
    parser.add_argument(
        "--execution-provider",
        choices=("codex",),
        default="codex",
        help="coding Provider exercised by real-mode scenarios",
    )
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument(
        "--preamble",
        choices=sorted(PREAMBLE_VARIANTS),
        default="permissive",
        help="real-mode framing of the routing protocol preamble (A/B)",
    )
    parser.add_argument("--long-silence", action="store_true")
    parser.add_argument("--fuzz", type=int, default=0)
    return parser


def main() -> int:
    try:
        return asyncio.run(run(build_parser().parse_args()))
    except (ScenarioError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"routing testbed error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
