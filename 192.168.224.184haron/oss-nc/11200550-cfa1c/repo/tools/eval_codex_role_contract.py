"""Evaluate one persistent Codex Kurisu root over a long Amadeus conversation.

The default mode validates the scenario and writes the exact developer contract
without contacting a model.  ``--live`` runs the scenario through the official
Codex App Server adapter in one persistent read-only thread, records native
subagent calls, and scores every root-visible message with the shipping Amadeus
stream parser.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm.codex_role_contract import (
    build_codex_root_role_contract,
    current_role_prompt,
    evaluate_role_output,
    prompt_fingerprint,
)
from llm.stream_parser import StreamTagParser


SCHEMA = "amadeus.codex-role-experiment.v1"
DEFAULT_SCENARIO = ROOT / "tools" / "codex_role_scenarios" / "kurisu_long_30_turns.json"
DEFAULT_REPORT_ROOT = ROOT / "runtime" / "e2e_reports" / "codex_role_contract"
_SENTENCE_END = tuple("。！？!?")
_SPAWN_NAMES = {
    "spawn_agent",
    "collaboration.spawn_agent",
    "collabagenttoolcall",
    # Codex App Server 0.147 presents one completed collaboration spawn as a
    # subAgentActivity item. One item is emitted per child, including parallel
    # children, so it is the observable spawn count at this adapter boundary.
    "subagentactivity",
}
_FORBIDDEN_USER_IDENTITY_NAMES = (
    "kurisu",
    "牧瀬",
    "红莉栖",
    "紅莉栖",
    "克里斯蒂娜",
    "christina",
)


@dataclass
class _LatencyTracker:
    started: float
    parser: StreamTagParser
    visible: str = ""
    first_visible_ms: float | None = None
    first_sentence_ms: float | None = None

    def feed(self, text: str) -> None:
        visible, _actions = self.parser.process_chunk(str(text or ""))
        if not visible:
            return
        self.visible += visible
        elapsed_ms = (time.monotonic() - self.started) * 1000.0
        if self.first_visible_ms is None and visible.strip():
            self.first_visible_ms = elapsed_ms
        if self.first_sentence_ms is None and any(mark in self.visible for mark in _SENTENCE_END):
            self.first_sentence_ms = elapsed_ms


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_scenario(value: dict[str, Any], *, source: str) -> dict[str, Any]:
    if value.get("schema") != "amadeus.codex-role-scenario.v1":
        raise ValueError(f"{source}: unsupported scenario schema")
    turns = value.get("turns")
    if not isinstance(turns, list) or len(turns) < 30:
        raise ValueError(f"{source}: long-session experiment requires at least 30 turns")
    fixture_files = value.get("fixture_files")
    if not isinstance(fixture_files, dict) or not fixture_files:
        raise ValueError(f"{source}: scenario requires fixture_files")
    for relative, content in fixture_files.items():
        candidate = Path(str(relative or ""))
        if candidate.is_absolute() or ".." in candidate.parts or not str(relative or "").strip():
            raise ValueError(f"{source}: unsafe fixture path: {relative!r}")
        if not isinstance(content, str):
            raise ValueError(f"{source}: fixture content must be text: {relative!r}")
    seen: set[str] = set()
    stages: dict[str, int] = {"early": 0, "mid": 0, "late": 0}
    for index, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            raise ValueError(f"{source}: turn {index} is not an object")
        turn_id = str(turn.get("id") or "").strip()
        if not turn_id or turn_id in seen:
            raise ValueError(f"{source}: turn {index} has missing/duplicate id")
        seen.add(turn_id)
        stage = turn_id.split("_", 1)[0]
        if stage not in stages:
            raise ValueError(f"{source}: turn {turn_id} has unsupported stage")
        stages[stage] += 1
        user_text = str(turn.get("user") or "").strip()
        if not user_text:
            raise ValueError(f"{source}: turn {turn_id} has no user text")
        lowered_user = user_text.casefold()
        leaked_names = [
            name for name in _FORBIDDEN_USER_IDENTITY_NAMES if name.casefold() in lowered_user
        ]
        if leaked_names:
            raise ValueError(
                f"{source}: turn {turn_id} re-anchors the persona by name: {leaked_names}"
            )
        minimum = int(turn.get("expected_min_spawns") or 0)
        maximum = int(turn.get("expected_max_spawns") or 0)
        if minimum < 0 or maximum < minimum:
            raise ValueError(f"{source}: turn {turn_id} has invalid spawn bounds")
        presets = turn.get("required_presets") or []
        if not isinstance(presets, list) or any(not isinstance(item, str) for item in presets):
            raise ValueError(f"{source}: turn {turn_id} has invalid required_presets")
    if len(set(stages.values())) != 1:
        raise ValueError(f"{source}: early/mid/late stages must be balanced: {stages}")
    return value


def _safe_workspace() -> Path:
    parent = Path(tempfile.gettempdir()).resolve()
    workspace = (parent / f"amadeus-codex-role-{uuid.uuid4().hex[:10]}").resolve()
    if workspace.parent != parent or not workspace.name.startswith("amadeus-codex-role-"):
        raise RuntimeError(f"unsafe experiment workspace: {workspace}")
    workspace.mkdir(parents=False, exist_ok=False)
    return workspace


def _initialize_fixture(workspace: Path, scenario: dict[str, Any]) -> None:
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(workspace),
        check=True,
        stdin=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "config", "user.email", "codex-role-eval@example.invalid"],
        cwd=str(workspace),
        check=True,
        stdin=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "config", "user.name", "Amadeus Role Eval"],
        cwd=str(workspace),
        check=True,
        stdin=subprocess.DEVNULL,
    )
    for relative, content in dict(scenario["fixture_files"]).items():
        target = (workspace / str(relative)).resolve()
        target.relative_to(workspace.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=str(workspace),
        check=True,
        stdin=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "fixture"],
        cwd=str(workspace),
        check=True,
        stdin=subprocess.DEVNULL,
    )


def _is_spawn_name(value: object) -> bool:
    clean = str(value or "").strip().lower()
    return (
        clean in _SPAWN_NAMES
        or "spawn_agent" in clean
        or "collabagent" in clean
        or "subagentactivity" in clean
    )


def _reclassify_recorded_tools(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the current native collaboration taxonomy to recorded rows."""

    for row in rows:
        existing_spawns = [str(value) for value in row.get("spawn_names") or []]
        direct = [str(value) for value in row.get("direct_tool_names") or []]
        recovered = [value for value in direct if _is_spawn_name(value)]
        remaining = [value for value in direct if not _is_spawn_name(value)]
        spawn_names = [*existing_spawns, *recovered]
        row["spawn_names"] = spawn_names
        row["direct_tool_names"] = remaining
        row["spawn_count"] = len(spawn_names)
        minimum = int(row.get("expected_min_spawns") or 0)
        maximum = int(row.get("expected_max_spawns") or 0)
        row["delegation_exact"] = minimum <= len(spawn_names) <= maximum
        expected_delegation = bool(row.get("expected_delegation", minimum > 0))
        delegated = bool(row.get("delegated", spawn_names))
        row["expected_delegation"] = expected_delegation
        row["delegated"] = delegated
        row["semantic_delegation_exact"] = delegated == expected_delegation
    return rows


def _median(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return round(statistics.median(values), 1) if values else None


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def summarize(selected: list[dict[str, Any]]) -> dict[str, Any]:
        count = len(selected)
        if not count:
            return {"turns": 0}
        return {
            "turns": count,
            "semantic_delegation_exact": sum(
                bool(row.get("semantic_delegation_exact")) for row in selected
            ),
            "semantic_delegation_accuracy": round(
                sum(bool(row.get("semantic_delegation_exact")) for row in selected) / count,
                4,
            ),
            "delegation_exact": sum(bool(row.get("delegation_exact")) for row in selected),
            "delegation_accuracy": round(
                sum(bool(row.get("delegation_exact")) for row in selected) / count,
                4,
            ),
            "final_contract_passes": sum(bool(row.get("final_contract_conformant")) for row in selected),
            "final_contract_rate": round(
                sum(bool(row.get("final_contract_conformant")) for row in selected) / count,
                4,
            ),
            "all_visible_contract_passes": sum(
                bool(row.get("all_visible_contract_conformant")) for row in selected
            ),
            "all_visible_contract_rate": round(
                sum(bool(row.get("all_visible_contract_conformant")) for row in selected) / count,
                4,
            ),
            "first_visible_median_ms": _median(selected, "first_visible_ms"),
            "first_sentence_median_ms": _median(selected, "first_sentence_ms"),
            "turn_total_median_ms": _median(selected, "turn_total_ms"),
        }

    by_stage = {
        stage: summarize([row for row in rows if row.get("stage") == stage])
        for stage in ("early", "mid", "late")
    }
    return {"overall": summarize(rows), "by_stage": by_stage}


def _human_review(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Blind human review sheet",
        "",
        "Score each final reply without looking at delegation or latency fields.",
        "",
        "| Turn | Persona consistency (1-5) | Language naturalness (1-5) | Emotion fit (1-5) | Out of character? | Invented execution? |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row['turn_id']} |  |  |  |  |  |")
        lines.extend(
            [
                "",
                f"> User: {row['user']}",
                ">",
                "> Reply: "
                + str(row.get("clean_text") or row.get("final_text") or "").replace("\n", " "),
                *[
                    "> Follow-up: " + str(value).replace("\n", " ")
                    for value in row.get("visible_followups") or []
                ],
                "",
            ]
        )
    return "\n".join(lines)


async def _run_live(
    scenario: dict[str, Any],
    *,
    contract: str,
    max_turns: int,
    timeout_s: float,
    model: str | None,
    reasoning_effort: str | None,
    service_tier: str | None,
    keep_workspace: bool,
) -> tuple[list[dict[str, Any]], str]:
    # Delay the SDK-backed import so dry-run contract generation works even in
    # a minimal test environment without the declared optional runtime package.
    from agent_host.adapters.codex_app_server import CodexAppServerAdapter
    from agent_host.provider_contract import ProviderRequirements
    from agent_host.provider_types import ProviderRunRequest, ProviderSessionHandle

    class RoleExperimentAdapter(CodexAppServerAdapter):
        @staticmethod
        def _codex_process_env(environ: dict[str, str] | None = None) -> dict[str, str] | None:
            """Preserve Windows identity paths while normalizing the shell PATH.

            The production adapter narrows its explicit override to PATH because
            the normal desktop launcher inherits the rest of the environment.
            The managed eval process receives the mapping as a complete child
            environment, so omitting USERPROFILE leaves Codex without a home.
            """

            source = dict(os.environ if environ is None else environ)
            normalized = dict(CodexAppServerAdapter._codex_process_env(source) or {})
            wanted = {
                "USERPROFILE",
                "HOMEDRIVE",
                "HOMEPATH",
                "APPDATA",
                "LOCALAPPDATA",
                "TEMP",
                "TMP",
                "SYSTEMROOT",
                "COMSPEC",
                "PATHEXT",
            }
            for key, value in source.items():
                if key.upper() in wanted and str(value or "").strip():
                    normalized[key] = value
            return normalized or None

        async def _prepare_desktop_handoff(
            self,
            codex: Any,
            thread: Any,
            request: ProviderRunRequest,
            *,
            rename_thread: bool,
        ) -> str:
            if rename_thread:
                inject = getattr(codex, "thread_inject_items", None)
                if not callable(inject):
                    raise RuntimeError("Codex SDK does not expose developer item injection")
                await inject(
                    str(thread.id),
                    [
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [{"type": "input_text", "text": contract}],
                        }
                    ],
                )
            return str(request.task or "")

    workspace = _safe_workspace()
    _initialize_fixture(workspace, scenario)
    adapter = RoleExperimentAdapter(
        model=model,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        turn_timeout_s=timeout_s,
        approval_mode="deny_all",
    )
    session: ProviderSessionHandle | None = None
    rows: list[dict[str, Any]] = []
    try:
        for index, definition in enumerate(scenario["turns"][:max_turns], 1):
            run_id = f"codex_role_{index:02d}_{uuid.uuid4().hex[:8]}"
            started = time.monotonic()
            tracker = _LatencyTracker(started=started, parser=StreamTagParser())
            event_names: list[str] = []
            commentary: list[str] = []
            spawn_names: list[str] = []
            direct_tool_names: list[str] = []
            first_delegate_ms: float | None = None

            async def emit(event: Any) -> None:
                nonlocal first_delegate_ms
                event_names.append(str(event.type))
                payload = dict(event.payload or {})
                if event.type == "assistant.delta":
                    tracker.feed(str(payload.get("text") or ""))
                elif event.type == "assistant.update":
                    text = str(payload.get("text") or "").strip()
                    if text:
                        commentary.append(text)
                elif event.type == "tool.call":
                    name = str(payload.get("name") or "")
                    if _is_spawn_name(name):
                        spawn_names.append(name)
                        if first_delegate_ms is None:
                            first_delegate_ms = (time.monotonic() - started) * 1000.0
                    else:
                        direct_tool_names.append(name)

            request = ProviderRunRequest(
                provider="codex",
                task=str(definition["user"]),
                cwd=str(workspace),
                session=session,
                requirements=ProviderRequirements(
                    task_kind="workspace_read",
                    workspace_access="read",
                    preferred_provider="codex",
                    preference_policy="require",
                ),
                metadata={"presentation_locale": "ja-JP"},
            )
            result = await adapter.run(request, run_id, emit)
            total_ms = (time.monotonic() - started) * 1000.0
            if result.session is not None:
                session = result.session

            required = tuple(str(value) for value in definition.get("required_presets") or [])
            final_eval = evaluate_role_output(
                str(result.result or ""),
                required_presets=required,
            )
            commentary_evals = [evaluate_role_output(text) for text in commentary]
            visible_evals = [*commentary_evals, final_eval]
            minimum = int(definition.get("expected_min_spawns") or 0)
            maximum = int(definition.get("expected_max_spawns") or 0)
            spawn_count = len(spawn_names)
            required_emotion_pass = not required or "required_emotion_missing" not in {
                violation.code for violation in final_eval.violations
            }
            rows.append(
                {
                    "turn": index,
                    "turn_id": str(definition["id"]),
                    "stage": str(definition["id"]).split("_", 1)[0],
                    "user": str(definition["user"]),
                    "provider_status": result.status,
                    "error": result.error,
                    "final_text": str(result.result or ""),
                    "clean_text": final_eval.clean_text,
                    "commentary": commentary,
                    "expected_min_spawns": minimum,
                    "expected_max_spawns": maximum,
                    "spawn_count": spawn_count,
                    "spawn_names": spawn_names,
                    "direct_tool_names": direct_tool_names,
                    "delegation_exact": minimum <= spawn_count <= maximum,
                    "required_presets": list(required),
                    "required_emotion_pass": required_emotion_pass,
                    "final_contract_conformant": final_eval.conformant,
                    "all_visible_contract_conformant": all(item.conformant for item in visible_evals),
                    "final_evaluation": final_eval.to_dict(),
                    "commentary_evaluations": [item.to_dict() for item in commentary_evals],
                    "first_visible_ms": round(tracker.first_visible_ms, 1)
                    if tracker.first_visible_ms is not None
                    else None,
                    "first_sentence_ms": round(tracker.first_sentence_ms, 1)
                    if tracker.first_sentence_ms is not None
                    else None,
                    "delegate_start_ms": round(first_delegate_ms, 1)
                    if first_delegate_ms is not None
                    else None,
                    "turn_total_ms": round(total_ms, 1),
                    "event_types": event_names,
                }
            )
    finally:
        await adapter.close()
        if not keep_workspace:
            expected_parent = Path(tempfile.gettempdir()).resolve()
            resolved = workspace.resolve()
            if resolved.parent == expected_parent and resolved.name.startswith("amadeus-codex-role-"):
                shutil.rmtree(resolved, ignore_errors=True)
    return rows, str(workspace)


def _amadeus_routing_scenario(
    scenario: dict[str, Any],
    *,
    max_turns: int,
    timeout_s: float,
) -> dict[str, Any]:
    """Translate the shared probes into the shipping routing harness schema.

    Codex-root can fan a two-file comparison out to two native children. The
    shipping Amadeus authority boundary delegates one user request as one Work
    Item, so this arm measures the common semantic decision (delegate or do not
    delegate) and records native fan-out separately.
    """

    steps: list[dict[str, Any]] = []
    for definition in scenario["turns"][:max_turns]:
        minimum = int(definition.get("expected_min_spawns") or 0)
        requires_work = minimum > 0
        turn_id = str(definition["id"])
        readonly = any(
            marker in turn_id
            for marker in ("_recall", "_summary", "_final")
        )
        steps.append(
            {
                "say": str(definition["user"]),
                "label": "new" if requires_work else ("readonly_ref" if readonly else "chat"),
                "wait": "provider_terminal" if requires_work else "chat_complete",
                "timeout_s": timeout_s,
                "source_turn_id": turn_id,
            }
        )
    return {
        "schema": 1,
        "id": f"amadeus_{scenario['scenario_id']}",
        "category": "mixed",
        "notes": "Shipping Amadeus arm of the shared Codex role-contract experiment.",
        "raw_utterances": True,
        "bind_scratch_project": True,
        "role_trace": True,
        "fixture_files": dict(scenario["fixture_files"]),
        "steps": steps,
    }


def _provider_run_created_count(
    events: list[dict[str, Any]],
    *,
    origin_turn_id: str = "",
) -> int:
    count = 0
    for event in events:
        if str(event.get("method") or "") != "provider.event":
            continue
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        nested = params.get("event") if isinstance(params.get("event"), dict) else params
        if str(nested.get("type") or "").strip().lower() == "run.created":
            metadata = (
                nested.get("metadata")
                if isinstance(nested.get("metadata"), dict)
                else {}
            )
            if origin_turn_id and str(metadata.get("turn_id") or "") != origin_turn_id:
                continue
            count += 1
    return count


def _visible_work_notes(events: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    for event in events:
        if str(event.get("method") or "") != "chat.work_note":
            continue
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        title = str(params.get("title") or "").strip()
        summary = str(params.get("summary") or "").strip()
        text = " — ".join(part for part in (title, summary) if part)
        if text and text not in notes:
            notes.append(text)
    return notes


async def _run_amadeus_live(
    scenario: dict[str, Any],
    *,
    report_dir: Path,
    max_turns: int,
    timeout_s: float,
    chat_provider: str,
) -> tuple[list[dict[str, Any]], str]:
    """Run the same probes through the current shipping Amadeus chat path."""

    from tools.e2e_routing_matrix import _real_recording

    routed = _amadeus_routing_scenario(
        scenario,
        max_turns=max_turns,
        timeout_s=timeout_s,
    )
    recording = await _real_recording(
        routed,
        report_dir / "amadeus_transport",
        chat_provider=chat_provider,
        execution_provider="codex",
    )
    observed_steps = [
        step
        for step in recording.get("steps", [])
        if isinstance(step, dict) and step.get("say")
    ]
    origin_run_counts: dict[str, int] = {}
    for observed in observed_steps:
        for event in observed.get("events", []):
            if not isinstance(event, dict) or str(event.get("method") or "") != "provider.event":
                continue
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            nested = params.get("event") if isinstance(params.get("event"), dict) else params
            if str(nested.get("type") or "").strip().lower() != "run.created":
                continue
            metadata = nested.get("metadata") if isinstance(nested.get("metadata"), dict) else {}
            origin = str(metadata.get("turn_id") or "").strip()
            if origin:
                origin_run_counts[origin] = origin_run_counts.get(origin, 0) + 1
    rows: list[dict[str, Any]] = []
    for index, (definition, observed) in enumerate(
        zip(scenario["turns"][:max_turns], observed_steps, strict=False),
        1,
    ):
        required = tuple(str(value) for value in definition.get("required_presets") or [])
        raw_text = str(observed.get("raw_role_text") or "")
        final_eval = evaluate_role_output(
            raw_text,
            required_presets=required,
            allowed_action_types=("EMO", "DELEGATE", "CONTROL", "AUIP"),
        )
        source_minimum = int(definition.get("expected_min_spawns") or 0)
        expected_delegation = source_minimum > 0
        turn_events = [
            event for event in observed.get("events", []) if isinstance(event, dict)
        ]
        transport_turn_id = str(observed.get("turn_id") or "").strip()
        spawn_count = (
            origin_run_counts.get(transport_turn_id, 0)
            if transport_turn_id
            else _provider_run_created_count(turn_events)
        )
        # Retain the shared scenario's requested native fan-out. The separate
        # semantic metric compares only delegate/no-delegate, while this exact
        # count exposes duplicated or collapsed independent subtasks.
        minimum = source_minimum
        maximum = int(definition.get("expected_max_spawns") or 0)
        required_emotion_pass = not required or "required_emotion_missing" not in {
            violation.code for violation in final_eval.violations
        }
        rows.append(
            {
                "turn": index,
                "turn_id": str(definition["id"]),
                "transport_turn_id": transport_turn_id,
                "stage": str(definition["id"]).split("_", 1)[0],
                "user": str(definition["user"]),
                "provider_status": "timeout" if observed.get("timed_out") else "completed",
                "error": "; ".join(str(value) for value in observed.get("delegate_errors") or []),
                "final_text": raw_text,
                "clean_text": final_eval.clean_text or str(observed.get("reply_text") or ""),
                "visible_followups": _visible_work_notes(turn_events),
                "commentary": [],
                "source_expected_min_spawns": source_minimum,
                "source_expected_max_spawns": int(definition.get("expected_max_spawns") or 0),
                "expected_min_spawns": minimum,
                "expected_max_spawns": maximum,
                "expected_delegation": expected_delegation,
                "delegated": spawn_count > 0,
                "spawn_count": spawn_count,
                "spawn_names": ["provider.run.created"] * spawn_count,
                "direct_tool_names": [],
                "delegation_exact": minimum <= spawn_count <= maximum,
                "required_presets": list(required),
                "required_emotion_pass": required_emotion_pass,
                "final_contract_conformant": final_eval.conformant,
                "all_visible_contract_conformant": final_eval.conformant,
                "final_evaluation": final_eval.to_dict(),
                "commentary_evaluations": [],
                "first_visible_ms": observed.get("first_visible_ms"),
                "first_sentence_ms": observed.get("first_sentence_ms"),
                "delegate_start_ms": observed.get("delegate_start_ms"),
                "turn_total_ms": observed.get("turn_total_ms"),
                "event_types": [
                    str(event.get("method") or "")
                    for event in turn_events
                ],
                "transport_timed_out": bool(observed.get("timed_out")),
            }
        )
    workspace = str(recording.get("paths", {}).get("scratch") or "")
    return rows, workspace


def _rescore_recorded_roles(rows: list[dict[str, Any]], *, arm: str) -> None:
    """Apply the current non-authoritative emotion-probe policy offline."""

    allowed = (
        ("EMO", "DELEGATE", "CONTROL", "AUIP")
        if arm == "amadeus"
        else ("EMO",)
    )
    for row in rows:
        required = tuple(str(value) for value in row.get("required_presets") or [])
        final_eval = evaluate_role_output(
            str(row.get("final_text") or ""),
            required_presets=required,
            allowed_action_types=allowed,
        )
        commentary = [str(value) for value in row.get("commentary") or []]
        commentary_evals = [
            evaluate_role_output(text, allowed_action_types=allowed)
            for text in commentary
        ]
        row["clean_text"] = final_eval.clean_text
        row["required_emotion_pass"] = not required or bool(
            set(required).intersection(final_eval.emotion_presets)
        )
        row["final_contract_conformant"] = final_eval.conformant
        row["all_visible_contract_conformant"] = all(
            item.conformant for item in (*commentary_evals, final_eval)
        )
        row["final_evaluation"] = final_eval.to_dict()
        row["commentary_evaluations"] = [item.to_dict() for item in commentary_evals]


def _write_report(
    report_dir: Path,
    *,
    scenario: dict[str, Any],
    contract: str,
    rows: list[dict[str, Any]],
    live: bool,
    workspace: str,
    run_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = _reclassify_recorded_tools(rows)
    report_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scenario_id": scenario["scenario_id"],
        "live": live,
        "turns_requested": len(scenario["turns"]),
        "turns_completed": len(rows),
        "source_prompt_sha256": prompt_fingerprint(current_role_prompt()),
        "contract_sha256": prompt_fingerprint(contract),
        "workspace": workspace,
        "run_config": dict(run_config or {}),
        "metrics": _aggregate(rows) if rows else {},
    }
    (report_dir / "contract.txt").write_text(contract, encoding="utf-8")
    (report_dir / "scenario.json").write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (report_dir / "turns.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    (report_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (report_dir / "human_review.md").write_text(_human_review(rows), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    parser.add_argument("--report-dir", default="")
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--arm",
        choices=("codex-root", "amadeus"),
        default="codex-root",
        help="conversation architecture to drive in live mode",
    )
    parser.add_argument(
        "--amadeus-chat-provider",
        default="deepseek",
        help="shipping Amadeus chat-model provider used by the Amadeus arm",
    )
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--model", default="")
    parser.add_argument("--reasoning-effort", default="")
    parser.add_argument("--service-tier", default="")
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument(
        "--rescore-turns",
        default="",
        help="recompute a prior turns.jsonl with the current scorer without a model call",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    scenario_path = Path(args.scenario).resolve()
    scenario = _validate_scenario(_read_json(scenario_path), source=str(scenario_path))
    role_prompt = current_role_prompt()
    contract = build_codex_root_role_contract(source_prompt=role_prompt)
    max_turns = min(len(scenario["turns"]), max(1, int(args.max_turns)))
    report_dir = (
        Path(args.report_dir).resolve()
        if str(args.report_dir or "").strip()
        else (DEFAULT_REPORT_ROOT / f"{scenario['scenario_id']}_{_utc_stamp()}").resolve()
    )

    rows: list[dict[str, Any]] = []
    workspace = ""
    rescore_path = str(args.rescore_turns or "").strip()
    if args.live and rescore_path:
        raise ValueError("--live and --rescore-turns are mutually exclusive")
    if rescore_path:
        rows = [
            json.loads(line)
            for line in Path(rescore_path).resolve().read_text(encoding="utf-8").splitlines()
            if line.strip()
        ][:max_turns]
        _rescore_recorded_roles(rows, arm=str(args.arm))
        workspace = str(rows[0].get("workspace") or "") if rows else ""
    elif args.live:
        if args.arm == "amadeus":
            rows, workspace = asyncio.run(
                _run_amadeus_live(
                    scenario,
                    report_dir=report_dir,
                    max_turns=max_turns,
                    timeout_s=max(1.0, float(args.timeout)),
                    chat_provider=str(args.amadeus_chat_provider or "").strip() or "deepseek",
                )
            )
        else:
            rows, workspace = asyncio.run(
                _run_live(
                    scenario,
                    contract=contract,
                    max_turns=max_turns,
                    timeout_s=max(1.0, float(args.timeout)),
                    model=str(args.model or "").strip() or None,
                    reasoning_effort=str(args.reasoning_effort or "").strip() or None,
                    service_tier=str(args.service_tier or "").strip() or None,
                    keep_workspace=bool(args.keep_workspace),
                )
            )
    summary = _write_report(
        report_dir,
        scenario=scenario,
        contract=contract,
        rows=rows,
        live=bool(args.live or rescore_path),
        workspace=workspace,
        run_config={
            "arm": str(args.arm),
            "model": str(args.model or "").strip(),
            "reasoning_effort": str(args.reasoning_effort or "").strip(),
            "service_tier": str(args.service_tier or "").strip(),
            "amadeus_chat_provider": str(args.amadeus_chat_provider or "").strip(),
            "rescored_from": str(Path(rescore_path).resolve()) if rescore_path else "",
        },
    )
    print(json.dumps({**summary, "report_dir": str(report_dir)}, ensure_ascii=False, indent=2))
    if not args.live and not rescore_path:
        return 0
    metrics = summary.get("metrics", {}).get("overall", {})
    hard_failure = (
        int(summary.get("turns_completed") or 0) != max_turns
        or int(metrics.get("delegation_exact") or 0) != max_turns
        or int(metrics.get("all_visible_contract_passes") or 0) != max_turns
    )
    return 1 if hard_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
