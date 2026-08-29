r"""Real-model probe for the Ledger-report versus Provider-work boundary.

The surface verb is deliberately held ambiguous: "summarize" may mean report
durable WorkItem facts or inspect the current repository.  The contract must
classify by the required source of truth, not by the word itself.

Usage::

    .venv_cu124\Scripts\python.exe -X utf8 tools/probes/probe_work_intent_boundary.py [repeats]

Exit codes: 0 every completed model turn matched; 1 semantic mismatch; 2 no
usable model evidence because infrastructure failed.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SESSION_ID = "probe-work-intent-boundary"

from tools.probes.control_adjudication_shadow import (
    adjudication_messages,
    delegate_attrs,
    merge_proposal_controls,
    normalize_control_actions,
    project_control_actions,
)
from server.control_decision import reconcile_control_decision, resolve_control_decision
from server.reference_catalog import candidate_catalog_from_coordinator


@dataclass(frozen=True)
class Case:
    name: str
    utterance: str
    expected_intent: str | None
    expected_subject: str = ""
    project_id: str = ""
    project_id_rule: str = "any"  # any / exact / absent
    expected_work_placement: str = ""
    expected_reference_mode: str = ""
    expected_target: str = ""


def _ask(
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float,
) -> str:
    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    response = client.llm_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=700,
        stream=False,
        timeout=45,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return str(response.choices[0].message.content or "")


def _case_errors(case: Case, attrs: list[dict[str, Any]]) -> list[str]:
    actual = str(attrs[0].get("intent") or "") if len(attrs) == 1 else (
        "none" if not attrs else f"{len(attrs)}-tags"
    )
    expected = case.expected_intent or "none"
    errors: list[str] = []
    if actual != expected:
        errors.append(f"intent expected {expected}, got {actual}")
    if case.expected_intent is not None and len(attrs) != 1:
        errors.append(f"expected exactly one DELEGATE, got {len(attrs)}")
    if len(attrs) == 1 and case.expected_subject:
        subject = str(attrs[0].get("subject") or "work_item")
        if subject != case.expected_subject:
            errors.append(f"subject expected {case.expected_subject}, got {subject}")
    if len(attrs) == 1 and case.project_id_rule == "exact":
        if str(attrs[0].get("project_id") or "") != case.project_id:
            errors.append("specific project report omitted or changed project_id")
    if len(attrs) == 1 and case.project_id_rule == "absent":
        if str(attrs[0].get("project_id") or ""):
            errors.append("project list incorrectly selected one project_id")
    return errors


def _decision_axis_errors(case: Case, decision: Any) -> list[str]:
    expected = (
        case.expected_work_placement,
        case.expected_reference_mode,
        case.expected_target,
    )
    if not any(expected):
        return []
    entries = tuple(getattr(decision, "entries", ()) or ())
    if str(getattr(decision, "status", "")) != "ok" or len(entries) != 1:
        return ["structured decision did not return exactly one valid entry"]
    entry = entries[0]
    errors: list[str] = []
    if (
        case.expected_work_placement
        and entry.work_placement != case.expected_work_placement
    ):
        errors.append(
            "work_placement expected "
            f"{case.expected_work_placement}, got {entry.work_placement}"
        )
    if case.expected_reference_mode:
        actual_reference_mode = (
            "none" if entry.reference_candidates is None else "candidates"
        )
        if actual_reference_mode != case.expected_reference_mode:
            errors.append(
                "reference_mode expected "
                f"{case.expected_reference_mode}, got {actual_reference_mode}"
            )
    if case.expected_target:
        actual_target = str(entry.control.get("target") or "")
        if actual_target != case.expected_target:
            errors.append(
                f"target expected {case.expected_target}, got {actual_target or 'absent'}"
            )
    return errors


def _adjudication_messages(
    control_system: str,
    *,
    utterance: str,
    project_id: str,
) -> list[dict[str, str]]:
    """Build an independent decision with full prior conversation context."""

    messages = _history(
        control_system,
        project_id,
        current_game="状态声明已经过时" in utterance,
    )
    messages.append({"role": "user", "content": utterance})
    return adjudication_messages(messages)


def _cases(main_project_id: str) -> list[Case]:
    return [
        Case(
            "recent-project-list",
            "我最近有哪些可以继续的本地项目？只告诉我账本里实际存在的。",
            "report",
            "project",
            project_id_rule="absent",
        ),
        Case(
            "specific-project-status",
            "Amadeus 项目目前有多少工作项、最近进展是什么？只查现有记录。",
            "report",
            "project",
            project_id=main_project_id,
            project_id_rule="exact",
        ),
        Case(
            "existing-task-status",
            "刚才创建 route-note.txt 的任务完成了吗？只汇报已有状态。",
            "report",
            "work_item",
        ),
        Case(
            "current-code-summary",
            "请实际读取当前 Amadeus 项目的代码，总结模块结构和关键入口；只读，不要修改文件。",
            "execute",
        ),
        Case(
            "current-code-audit",
            "检查当前仓库的依赖和测试布局，指出风险并给一份报告，不要改代码。",
            "execute",
        ),
        Case(
            "existing-repo-file-edit",
            "修改当前 Amadeus 项目已有的 README.md，把安装说明写清楚；这不是继续刚才的 route-note.txt 任务。",
            "execute",
        ),
        Case(
            "auip-state-schema-amend",
            "你需要根据 AUIP 重新改写当前游戏，目前这个版本的状态声明已经过时了。",
            "amend",
        ),
        Case(
            "new-desktop-deliverable",
            "你可以在桌面帮我写写一个五子棋的游戏吗？",
            "execute",
            expected_work_placement="draft",
            expected_reference_mode="none",
            expected_target="desktop",
        ),
        Case(
            "existing-artifact-inspection",
            "实际打开并总结刚才 route-note.txt 的内容，不要修改它。",
            "amend",
        ),
        Case(
            "general-code-knowledge",
            "一般来说 Python 项目为什么会使用 pyproject.toml？",
            None,
        ),
        Case(
            "hypothetical-analysis",
            "如果以后要审计一个陌生仓库，你通常会先看哪些部分？",
            None,
        ),
    ]


def _history(
    system: str,
    project_id: str,
    *,
    current_game: bool = False,
) -> list[dict[str, str]]:
    history = [
        {"role": "system", "content": system},
        {"role": "user", "content": "在 Amadeus 项目里创建 route-note.txt，写入 routing baseline。"},
        {
            "role": "assistant",
            "content": (
                "わかった、プロジェクト内に作るわ。"
                f'[DELEGATE provider="codex" intent="execute" project_id="{project_id}" '
                'task="route-note.txt を作成して routing baseline と書く"]'
            ),
        },
    ]
    if current_game:
        history.extend(
            (
                {
                    "role": "user",
                    "content": "做一个五子棋游戏，并接入 AUIP 让我可以和你一起玩。",
                },
                {
                    "role": "assistant",
                    "content": (
                        "作って AUIP に接続したわ。"
                        f'[DELEGATE provider="codex" intent="execute" project_id="{project_id}" '
                        'task="五子棋ゲームを作成して AUIP に接続する"]'
                    ),
                },
            )
        )
    return history


async def _run(args: argparse.Namespace) -> int:
    import config.settings as settings
    from agent_host.work_ledger_store import WorkLedgerStore
    from llm.prompts import (
        get_delegate_control_prompt,
        get_structured_control_prompt,
        get_system_prompt,
    )
    from server.work_context import augment_system_prompt_with_active_provider_context
    from server.work_ledger_coordinator import WorkLedgerCoordinator

    if not settings.DELEGATE_INTENT_ATTRIBUTE:
        print("DELEGATE_INTENT_ATTRIBUTE is off; contract unavailable")
        return 2
    if args.structured_decision:
        args.shadow_adjudication = True
        args.proposal_gated = True

    raw_semantic_failures = 0
    canonical_semantic_failures = 0
    effective_semantic_failures = 0
    proposal_corrections = 0
    proposal_regressions = 0
    proposal_unchanged_failures = 0
    omission_recoveries = 0
    infrastructure_failures = 0
    completed = 0
    latencies: list[float] = []
    adjudication_latencies: list[float] = []
    with tempfile.TemporaryDirectory(prefix="work_intent_boundary_") as temp:
        root = Path(temp)
        main_root = root / "amadeus"
        second_root = root / "game-lab"
        main_root.mkdir()
        second_root.mkdir()
        store = WorkLedgerStore(root / "ledger.sqlite3")
        main = store.create_or_get_project(main_root, name="Amadeus")
        store.create_or_get_project(second_root, name="Game Lab")
        item = store.create_work_item(
            main.project_id,
            title="Create route-note.txt",
            goal="Record routing baseline.",
        )
        attempt = store.create_attempt(
            item.work_item_id,
            provider="codex",
            task="create route-note.txt",
            metadata={"session_id": SESSION_ID},
        )
        store.register_artifact(
            item.work_item_id,
            kind="business.file",
            title="route-note.txt",
            attempt_id=attempt.attempt_id,
            path=main_root / "route-note.txt",
        )
        store.update_attempt(
            attempt.attempt_id,
            execution_status="succeeded",
            result="route-note.txt created",
        )
        game = store.create_work_item(
            main.project_id,
            title="Current AUIP Gomoku game",
            goal="Keep the current game compatible with the AUIP contract.",
        )
        game_attempt = store.create_attempt(
            game.work_item_id,
            provider="codex",
            task="create the current AUIP game",
            metadata={"session_id": SESSION_ID},
        )
        store.register_artifact(
            game.work_item_id,
            kind="business.file",
            title="gomoku.html",
            attempt_id=game_attempt.attempt_id,
            path=main_root / "gomoku.html",
        )
        store.update_attempt(
            game_attempt.attempt_id,
            execution_status="succeeded",
            result="AUIP Gomoku game created",
        )
        coordinator = WorkLedgerCoordinator(store)
        coordinator.configure()
        try:
            cases = _cases(main.project_id)
            if args.case:
                requested = set(args.case)
                available = {case.name for case in cases}
                unknown = sorted(requested - available)
                if unknown:
                    raise ValueError(f"unknown case name(s): {', '.join(unknown)}")
                cases = [case for case in cases if case.name in requested]
            print(
                f"work intent boundary: {max(1, args.repeats)} repeat(s) x "
                f"{len(cases)} cases model={args.model} temperature={args.temperature}\n"
            )
            for case in cases:
                with patch(
                    "server.work_ledger_coordinator.cwd_in_project_registry",
                    return_value=True,
                ):
                    if case.name == "auip-state-schema-amend":
                        coordinator.bind_session_context(
                            SESSION_ID,
                            main.project_id,
                            work_item_id=game.work_item_id,
                            source="probe-current-game",
                        )
                    else:
                        coordinator.set_session_project(SESSION_ID, main.project_id)
                    system = augment_system_prompt_with_active_provider_context(
                        get_system_prompt("with_delegate"),
                        session_id=SESSION_ID,
                    )
                    control_system = augment_system_prompt_with_active_provider_context(
                        (
                            get_structured_control_prompt()
                            if args.structured_decision
                            else get_delegate_control_prompt()
                        ),
                        session_id=SESSION_ID,
                    )
                    typed_candidates, typed_catalog_complete, _typed_reason = (
                        candidate_catalog_from_coordinator(
                            coordinator,
                            SESSION_ID,
                            project_limit=200,
                            work_item_limit=200,
                        )
                    )
                assert "source of truth" in system or "事実源" in system
                outcomes: list[str] = []
                for _ in range(max(1, args.repeats)):
                    messages = _history(
                        system,
                        main.project_id,
                        current_game=case.name == "auip-state-schema-amend",
                    )
                    messages.append({"role": "user", "content": case.utterance})
                    started = time.monotonic()
                    try:
                        reply = await asyncio.to_thread(
                            _ask,
                            messages,
                            model=args.model,
                            temperature=args.temperature,
                        )
                    except Exception as exc:
                        infrastructure_failures += 1
                        outcomes.append("INFRA")
                        print(f"  INFRA {case.name}: {type(exc).__name__}: {exc}")
                        continue
                    latencies.append(time.monotonic() - started)
                    completed += 1
                    attrs = delegate_attrs(reply)
                    raw_errors = _case_errors(case, attrs)
                    if raw_errors:
                        raw_semantic_failures += 1
                    if args.shadow_adjudication:
                        review_started = time.monotonic()
                        merge_notes: list[str] = []
                        if args.structured_decision:
                            decision = None
                            proposal_slots = normalize_control_actions(attrs)
                            if not proposal_slots:
                                from core.chat_runtime import ChatRuntime

                                try:
                                    recovered = await ChatRuntime._request_delegate_resend(
                                        case.utterance,
                                        reply,
                                        session_id=SESSION_ID,
                                    )
                                except Exception as exc:
                                    infrastructure_failures += 1
                                    recovered = []
                                    merge_notes = [
                                        f"omission resend unavailable: {type(exc).__name__}"
                                    ]
                                recovered_attrs = [
                                    dict(action.get("attrs") or {})
                                    for action in recovered
                                    if action.get("type") == "DELEGATE"
                                ]
                                effective_attrs = normalize_control_actions(
                                    recovered_attrs
                                )
                                canonical_attrs = project_control_actions(
                                    effective_attrs
                                )
                                canonical_reply = (
                                    "existing omission resend recovered action"
                                    if effective_attrs
                                    else ""
                                )
                                if effective_attrs:
                                    omission_recoveries += 1
                                    merge_notes.append(canonical_reply)
                            else:
                                decision = await resolve_control_decision(
                                    _adjudication_messages(
                                        control_system,
                                        utterance=case.utterance,
                                        project_id=main.project_id,
                                    ),
                                    proposal_slots,
                                    typed_candidates,
                                    complete=typed_catalog_complete,
                                    query=lambda review_messages: asyncio.to_thread(
                                        _ask,
                                        review_messages,
                                        model=args.model,
                                        temperature=0.0,
                                    ),
                                    proposal_controls=attrs,
                                )
                                if decision.status == "unavailable":
                                    infrastructure_failures += 1
                                canonical_reply = decision.raw_reply or (
                                    f"{decision.status}: {decision.reason}"
                                )
                                effective_attrs, merge_notes = (
                                    reconcile_control_decision(
                                        proposal_slots,
                                        decision,
                                        provider_ids={
                                            "codex",
                                            "codex",
                                            "browser",
                                            "openclaw",
                                        },
                                        proposal_controls=attrs,
                                        source_user_text=case.utterance,
                                    )
                                )
                                canonical_attrs = project_control_actions(
                                    effective_attrs
                                )
                        else:
                            try:
                                canonical_reply = await asyncio.to_thread(
                                    _ask,
                                    _adjudication_messages(
                                        control_system,
                                        utterance=case.utterance,
                                        project_id=main.project_id,
                                    ),
                                    model=args.model,
                                    temperature=0.0,
                                )
                            except Exception as exc:
                                infrastructure_failures += 1
                                outcomes.append("AUDIT_INFRA")
                                print(
                                    f"  AUDIT_INFRA {case.name}: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                                continue
                            canonical_attrs = project_control_actions(
                                delegate_attrs(canonical_reply)
                            )
                        adjudication_latencies.append(time.monotonic() - review_started)
                        canonical_errors = _case_errors(case, canonical_attrs)
                        if args.structured_decision:
                            canonical_errors.extend(
                                _decision_axis_errors(case, decision)
                            )
                        if canonical_errors:
                            canonical_semantic_failures += 1
                        effective_errors: list[str] = []
                        if args.proposal_gated:
                            if not args.structured_decision:
                                effective_attrs, merge_notes = merge_proposal_controls(
                                    attrs,
                                    canonical_attrs,
                                )
                            effective_errors = _case_errors(case, effective_attrs)
                            if effective_errors:
                                effective_semantic_failures += 1
                            if raw_errors and not effective_errors:
                                proposal_corrections += 1
                            elif not raw_errors and effective_errors:
                                proposal_regressions += 1
                            elif raw_errors and effective_errors:
                                proposal_unchanged_failures += 1
                        outcome = (
                            ("RAW_FAIL" if raw_errors else "RAW_PASS")
                            + "->"
                            + ("CANON_FAIL" if canonical_errors else "CANON_PASS")
                            + (
                                "->"
                                + ("EFFECTIVE_FAIL" if effective_errors else "EFFECTIVE_PASS")
                                if args.proposal_gated
                                else ""
                            )
                        )
                        outcomes.append(outcome)
                        if raw_errors or canonical_errors or effective_errors:
                            print(
                                f"  {outcome} {case.name}: "
                                f"raw={'; '.join(raw_errors) or 'ok'}; "
                                f"canonical={'; '.join(canonical_errors) or 'ok'}"
                                + (
                                    f"; effective={'; '.join(effective_errors) or 'ok'}"
                                    if args.proposal_gated
                                    else ""
                                )
                            )
                            if merge_notes:
                                print(f"       merge: {'; '.join(merge_notes)}")
                            if canonical_errors:
                                print(
                                    "       canonical: "
                                    f"{' '.join(canonical_reply.split())[:240]}"
                                )
                    elif raw_errors:
                        outcomes.append("FAIL")
                        print(f"  FAIL {case.name}: {'; '.join(raw_errors)}")
                        print(f"       reply: {' '.join(reply.split())[:240]}")
                    else:
                        outcomes.append("PASS")
                print(f"  {case.name:30s} {'/'.join(outcomes)}")
        finally:
            coordinator.close()

    print("\nsummary")
    print(f"  completed turns       : {completed}")
    print(f"  raw semantic failures : {raw_semantic_failures}")
    if args.shadow_adjudication:
        print(f"  canonical failures    : {canonical_semantic_failures}")
    if args.proposal_gated:
        print(f"  effective failures    : {effective_semantic_failures}")
        print(f"  proposal corrections  : {proposal_corrections}")
        print(f"  proposal regressions  : {proposal_regressions}")
        print(f"  unchanged failures    : {proposal_unchanged_failures}")
        if args.structured_decision:
            print(f"  omission recoveries   : {omission_recoveries}")
    print(f"  infrastructure failures: {infrastructure_failures}")
    if latencies:
        print(f"  latency median        : {statistics.median(latencies):.2f}s")
    if adjudication_latencies:
        print(
            "  adjudication median   : "
            f"{statistics.median(adjudication_latencies):.2f}s"
        )
    if completed == 0:
        return 2
    semantic_failures = (
        effective_semantic_failures
        if args.proposal_gated
        else canonical_semantic_failures
        if args.shadow_adjudication
        else raw_semantic_failures
    )
    return 1 if semantic_failures or infrastructure_failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repeats", nargs="?", type=int, default=2)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument(
        "--shadow-adjudication",
        action="store_true",
        help="review each sampled proposal at temperature zero without changing product behavior",
    )
    parser.add_argument(
        "--proposal-gated",
        action="store_true",
        help="shadow-merge canonical controls only when the role emitted a proposal",
    )
    parser.add_argument(
        "--structured-decision",
        action="store_true",
        help="use one proposal-indexed JSON decision for control and Project sets",
    )
    parser.add_argument(
        "--case",
        action="append",
        help="run only the named case; repeat the option to select several",
    )
    return parser


if __name__ == "__main__":
    parsed = _parser().parse_args()
    if parsed.proposal_gated:
        parsed.shadow_adjudication = True
    raise SystemExit(asyncio.run(_run(parsed)))
