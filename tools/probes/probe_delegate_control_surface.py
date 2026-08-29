r"""Real-model shadow probe for the complete DELEGATE control vocabulary.

This probe never dispatches an action. It builds the shipping dynamic prompt
from a temporary Work Ledger and an optional active Browser branch, then asks
an independent temperature-zero decision to classify the current user turn.
The current role reply is never part of that decision.

Usage::

    .venv_cu124\Scripts\python.exe -X utf8 tools/probes/probe_delegate_control_surface.py [repeats]

Exit codes: 0 all completed decisions matched; 1 semantic mismatch; 2 no
usable evidence because infrastructure failed.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.probes.control_adjudication_shadow import (
    adjudication_messages,
    delegate_attrs,
    filter_known_fact_controls,
    merge_proposal_controls,
    normalize_control_actions,
    project_control_actions,
)
from server.project_reference import (
    ProjectCandidate,
    guard_project_bound_actions,
    resolve_project_reference,
)
from server.reference_catalog import candidate_catalog_from_coordinator
from server.control_decision import (
    CONTROL_REFERENCE_CANDIDATES_ATTR,
    reconcile_control_decision,
    resolve_control_decision,
)


SESSION_ID = "probe-delegate-control-surface"


def _ask(messages: list[dict[str, str]], *, model: str, temperature: float) -> str:
    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    response = client.llm_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=900,
        stream=False,
        timeout=45,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return str(response.choices[0].message.content or "")


@dataclass(frozen=True)
class ExpectedAction:
    exact: dict[str, str] = field(default_factory=dict)
    absent: tuple[str, ...] = ()
    truthy: tuple[str, ...] = ()
    nonempty: tuple[str, ...] = ()


@dataclass(frozen=True)
class Case:
    name: str
    utterance: str
    expected: tuple[ExpectedAction, ...]
    history: str = "work"
    active_browser: bool = False
    expected_disposition: str = ""


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _errors(case: Case, attrs: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if len(attrs) != len(case.expected):
        errors.append(f"expected {len(case.expected)} DELEGATE tag(s), got {len(attrs)}")
    for index, expected in enumerate(case.expected):
        if index >= len(attrs):
            break
        actual = attrs[index]
        for key, value in expected.exact.items():
            if str(actual.get(key) or "").strip() != value:
                errors.append(
                    f"action {index + 1} {key}: expected {value!r}, "
                    f"got {str(actual.get(key) or '').strip()!r}"
                )
        for key in expected.absent:
            if str(actual.get(key) or "").strip():
                errors.append(f"action {index + 1} {key}: expected absent")
        for key in expected.truthy:
            if not _is_truthy(actual.get(key)):
                errors.append(f"action {index + 1} {key}: expected true")
        for key in expected.nonempty:
            if not str(actual.get(key) or "").strip():
                errors.append(f"action {index + 1} {key}: expected non-empty")
    return errors


def _disposition_errors(
    case: Case,
    dispositions: tuple[str, ...],
) -> list[str]:
    expected = str(case.expected_disposition or "").strip()
    if not expected:
        return []
    if expected == "blocked":
        matched = any(value.startswith("blocked_") for value in dispositions)
    else:
        matched = expected in dispositions
    return [] if matched else [
        f"expected handler disposition {expected!r}, got {list(dispositions)!r}"
    ]


def _dispatchable_structured_actions(
    actions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Project the handler boundary, not merely accepted canonical controls.

    A frozen empty reference set is visibly blocked and a multi-candidate set
    is deferred to Attention.  Neither reaches a Provider or mutates focus, so
    the control-surface probe must not count it as a dispatched action merely
    because reconciliation correctly preserved it for the handler.
    """

    from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR

    dispatchable: list[dict[str, Any]] = []
    dispositions: list[str] = []
    for action in actions:
        if CONTROL_REFERENCE_CANDIDATES_ATTR in action:
            candidates = action.get(CONTROL_REFERENCE_CANDIDATES_ATTR)
            focus_modifier = str(action.get("focus") or "").strip().lower()
            needs_existing_entity = (
                (
                    str(action.get("intent") or "").strip().lower() == "focus"
                    and focus_modifier != "clear"
                )
                or focus_modifier == "set"
            )
            if candidates is None and needs_existing_entity:
                dispositions.append("blocked_missing")
                continue
            if isinstance(candidates, tuple):
                if not candidates:
                    dispositions.append("blocked_empty")
                    continue
                if len(candidates) > 1:
                    dispositions.append("attention")
                    continue
        dispatchable.append(action)
    return dispatchable, tuple(dispositions)


def _histories(main_id: str, chess_id: str) -> dict[str, list[dict[str, str]]]:
    work = [
        {"role": "user", "content": "在 Amadeus 项目里创建 route-note.txt，写入 routing baseline。"},
        {
            "role": "assistant",
            "content": (
                "わかった、作っておくわ。"
                f'[DELEGATE provider="codex" intent="execute" project_id="{main_id}" '
                'task="route-note.txt を作成して routing baseline と書く"]'
            ),
        },
        {
            "role": "assistant",
            "content": "[WORK_OBSERVER]\nroute-note.txt was created and verified.",
        },
    ]
    browser = [
        {"role": "user", "content": "打开哔哩哔哩。"},
        {
            "role": "assistant",
            "content": (
                "開いてみるわ。"
                '[DELEGATE provider="browser" intent="execute" branch="new" '
                'action="open" url="https://www.bilibili.com" task="哔哩哔哩を開く"]'
            ),
        },
        {
            "role": "assistant",
            "content": "[WORK_OBSERVER]\nBrowser is on the Bilibili home page.",
        },
    ]
    game = [
        {"role": "user", "content": "把 endless game 改成三次获胜。"},
        {
            "role": "assistant",
            "content": (
                "変更を始めるわ。"
                f'[DELEGATE provider="codex" intent="amend" project_id="{main_id}" '
                'task="endless game を三回先勝に変更する"]'
            ),
        },
        {
            "role": "assistant",
            "content": "[WORK_OBSERVER]\nThe endless game amendment is still running.",
        },
    ]
    cross_type = [
        {
            "role": "user",
            "content": "在国际象棋游戏项目里做一个双人模式。",
        },
        {
            "role": "assistant",
            "content": (
                "先放进这个项目。"
                f'[DELEGATE provider="codex" intent="execute" project_id="{chess_id}" '
                'task="实现国际象棋游戏双人模式"]'
            ),
        },
        {
            "role": "assistant",
            "content": "[WORK_OBSERVER]\n国际象棋游戏双人模式已经实现并验证。",
        },
        {
            "role": "user",
            "content": "另外一次性做一个也叫国际象棋游戏的原型，不属于任何项目。",
        },
        {
            "role": "assistant",
            "content": (
                "这个会留在本会话草稿。"
                '[DELEGATE provider="codex" intent="execute" one_off="true" '
                'task="制作一次性的国际象棋游戏原型"]'
            ),
        },
        {
            "role": "assistant",
            "content": "[WORK_OBSERVER]\n一次性的国际象棋游戏原型已经完成。",
        },
    ]
    return {
        "work": work,
        "browser": browser,
        "game": game,
        "cross_type": cross_type,
        "empty": [],
    }


def _cases(
    main_id: str,
    game_id: str,
    chess_id: str,
) -> list[Case]:
    codex = {"provider": "codex"}
    return [
        Case(
            "focus-project",
            "切换到 Amadeus 项目，后续工作都在这里做。",
            (ExpectedAction(exact={**codex, "intent": "focus", "project_id": main_id}, absent=("task",)),),
        ),
        Case(
            "focus-drafts",
            "回到草稿，接下来的工作不要放在项目里。",
            (ExpectedAction(exact={**codex, "intent": "focus"}, absent=("project_id", "task")),),
        ),
        Case(
            "focus-and-execute",
            "切到 Amadeus，并新建 control-note.txt 写入 ready。",
            (ExpectedAction(exact={**codex, "intent": "execute", "project_id": main_id, "focus": "set"}),),
        ),
        Case(
            "drafts-and-execute",
            "回到草稿，然后临时新建 control-note.txt 写入 ready。",
            (ExpectedAction(exact={**codex, "intent": "execute", "focus": "clear"}, absent=("project_id",)),),
        ),
        Case(
            "one-off-while-focused",
            "另外做一个一次性的番茄钟小工具，不属于任何项目。",
            (ExpectedAction(exact={**codex, "intent": "execute"}, absent=("project_id", "focus"), truthy=("one_off",)),),
        ),
        Case(
            "project-report",
            "Amadeus 项目整体进展怎样？只查询账本。",
            (ExpectedAction(exact={**codex, "intent": "report", "subject": "project", "project_id": main_id}),),
        ),
        Case(
            "work-item-report",
            "刚才创建 route-note.txt 的任务完成了吗？只汇报已有状态。",
            (ExpectedAction(exact={**codex, "intent": "report", "subject": "work_item"}),),
        ),
        Case(
            "fresh-code-observation",
            "实际读取当前 Amadeus 仓库并总结测试结构，不要修改文件。",
            (ExpectedAction(exact={**codex, "intent": "execute", "project_id": main_id}),),
        ),
        Case(
            "artifact-continuation",
            "实际读取并总结刚才 route-note.txt 的内容，不要修改它。",
            (ExpectedAction(exact={**codex, "intent": "amend"}),),
        ),
        Case(
            "cross-project-operation",
            "在 Game Lab 项目里新建 scores.txt，写入 42；不要切换后续工作。",
            (ExpectedAction(exact={**codex, "intent": "execute", "project_id": game_id}, absent=("focus",)),),
        ),
        Case(
            "retract-running",
            "刚才那个还在运行的游戏任务不用做了，停下来。",
            (),
            expected_disposition="attention",
        ),
        Case(
            "ordinary-chat",
            "一般来说，为什么软件项目需要区分状态事实和代码观察？",
            (),
        ),
        Case(
            "ordinary-research-comment",
            "我感觉它 quite hard to find.",
            (),
            history="work",
        ),
        Case(
            "ordinary-task-correction",
            "这不是新的任务吧，你理解错了吗",
            (),
            history="work",
        ),
        Case(
            "ordinary-acknowledgement",
            "那就是。",
            (),
            history="work",
        ),
        Case(
            "incremental-amend",
            "算了，改成4次吧。",
            (ExpectedAction(exact={**codex, "intent": "amend"}),),
            history="game",
        ),
        Case(
            "open-web-research",
            "帮我查找 Paxos Made Simple 的原文并总结可靠来源。",
            (ExpectedAction(exact={"provider": "openclaw", "intent": "execute"}),),
        ),
        Case(
            "unknown-focus",
            "切换到候选列表里不存在的 Orion 项目。",
            (),
        ),
        Case(
            "known-risk-ambiguous-focus",
            "切到那个名字里有 Game 的项目。",
            (),
            expected_disposition="attention",
        ),
        Case(
            "known-risk-ambiguous-alias",
            "切换到 Game 项目。",
            (),
            expected_disposition="attention",
        ),
        Case(
            "cross-type-project-draft-ambiguity",
            "切回那个国际象棋游戏。",
            (),
            history="cross_type",
            expected_disposition="attention",
        ),
        Case(
            "partial-name-project-focus",
            "切回象棋项目。",
            (
                ExpectedAction(
                    exact={**codex, "intent": "focus", "project_id": chess_id},
                    absent=("task",),
                ),
            ),
            history="cross_type",
        ),
        Case(
            "browser-continue",
            "点击页面上的第一个视频。",
            (ExpectedAction(exact={"provider": "browser", "intent": "execute", "branch": "continue"}),),
            history="browser",
            active_browser=True,
        ),
        Case(
            "browser-page-search",
            "在当前网站里搜索 Amadeus。",
            (ExpectedAction(exact={"provider": "browser", "intent": "execute", "branch": "continue", "action": "search"}),),
            history="browser",
            active_browser=True,
        ),
        Case(
            "browser-new-url",
            "改为打开 https://en.wikipedia.org/wiki/Amadeus。",
            (ExpectedAction(exact={"provider": "browser", "intent": "execute", "branch": "new", "action": "open"}),),
            history="browser",
            active_browser=True,
        ),
        Case(
            "browser-close",
            "关闭当前浏览器操作，回到普通聊天。",
            (ExpectedAction(exact={"provider": "browser", "branch": "close"}),),
            history="browser",
            active_browser=True,
        ),
        Case(
            "two-independent-actions",
            "先只查 Amadeus 项目的账本进展，然后打开 https://en.wikipedia.org/wiki/Amadeus。",
            (
                ExpectedAction(exact={**codex, "intent": "report", "subject": "project", "project_id": main_id}),
                ExpectedAction(exact={"provider": "browser", "intent": "execute", "action": "open"}),
            ),
        ),
        Case(
            "two-project-actions",
            "先在 Amadeus 项目新建 alpha.txt 写入 A，然后在 Game Lab 项目新建 beta.txt 写入 B；都不要切换后续工作。",
            (
                ExpectedAction(
                    exact={**codex, "intent": "execute", "project_id": main_id},
                    absent=("focus",),
                ),
                ExpectedAction(
                    exact={**codex, "intent": "execute", "project_id": game_id},
                    absent=("focus",),
                ),
            ),
        ),
    ]


async def _run(args: argparse.Namespace) -> int:
    import config.settings as settings
    import server.interaction_branch as interaction_branch_module
    from agent_host.work_ledger_store import WorkLedgerStore
    from llm.prompts import (
        finalize_system_prompt_language,
        get_delegate_control_prompt,
        get_structured_control_prompt,
        get_system_prompt,
    )
    from server.interaction_branch import InteractionBranchCoordinator, InteractionBranchState
    from server.work_context import (
        augment_system_prompt_for_control_decision,
        augment_system_prompt_with_active_provider_context,
    )
    from server.work_ledger_coordinator import WorkLedgerCoordinator

    if args.structured_decision:
        args.proposal_gated = True

    semantic_failures = 0
    raw_semantic_failures = 0
    infrastructure_failures = 0
    completed = 0
    latencies: list[float] = []
    raw_latencies: list[float] = []
    structural_normalizations = 0
    fact_rejections = 0
    proposal_corrections = 0
    proposal_regressions = 0
    project_reference_rejections = 0
    project_reference_corrections = 0
    project_reference_latencies: list[float] = []
    structured_invalid = 0
    structured_protocol_retries = 0
    structured_reference_dispositions = {
        "attention": 0,
        "blocked_empty": 0,
        "blocked_missing": 0,
    }
    with tempfile.TemporaryDirectory(prefix="delegate_control_surface_") as temp:
        root = Path(temp)
        main_root = root / "amadeus"
        game_root = root / "game-lab"
        game_archive_root = root / "game-archive"
        chess_root = root / "chess"
        scratch_root = root / "scratch"
        draft_root = scratch_root / "chess-timer"
        main_root.mkdir()
        game_root.mkdir()
        game_archive_root.mkdir()
        chess_root.mkdir()
        draft_root.mkdir(parents=True)
        store = WorkLedgerStore(root / "ledger.sqlite3")
        main = store.create_or_get_project(main_root, name="Amadeus")
        game = store.create_or_get_project(game_root, name="Game Lab")
        game_archive = store.create_or_get_project(game_archive_root, name="Game Archive")
        chess = store.create_or_get_project(chess_root, name="国际象棋游戏")
        scratch = store.create_or_get_project(
            scratch_root,
            name="scratch",
            metadata={"scratch": True},
        )

        completed_item = store.create_work_item(
            main.project_id,
            title="Create route-note.txt",
            goal="Record routing baseline.",
        )
        completed_attempt = store.create_attempt(
            completed_item.work_item_id,
            provider="codex",
            task="create route-note.txt",
            metadata={"session_id": SESSION_ID},
        )
        store.register_artifact(
            completed_item.work_item_id,
            kind="business.file",
            title="route-note.txt",
            attempt_id=completed_attempt.attempt_id,
            path=main_root / "route-note.txt",
        )
        store.update_attempt(
            completed_attempt.attempt_id,
            execution_status="succeeded",
            result="route-note.txt created",
        )
        running_item = store.create_work_item(
            main.project_id,
            title="Build endless game",
            goal="Create and verify an endless game.",
        )
        store.create_attempt(
            running_item.work_item_id,
            provider="codex",
            task="build endless game",
            metadata={"session_id": SESSION_ID},
        )
        draft_item = store.create_work_item(
            scratch.project_id,
            title="国际象棋游戏",
            goal="Create a temporary chess prototype.",
            workspace_path=draft_root,
        )
        store.create_attempt(
            draft_item.work_item_id,
            provider="codex",
            task="制作一次性的国际象棋游戏原型",
            metadata={
                "session_id": SESSION_ID,
                "source_user_text": "另外一次性做一个也叫国际象棋游戏的原型，不属于任何项目。",
            },
        )

        coordinator = WorkLedgerCoordinator(store)
        branch_coordinator = InteractionBranchCoordinator(
            provider_run=lambda _params: asyncio.sleep(0, result={}),
            root=root / "branches",
        )
        coordinator.configure()
        branch_coordinator.configure()
        now = time.time()
        active_branch = InteractionBranchState(
            branch_id="branch-bilibili",
            parent_session_id=SESSION_ID,
            provider="browser",
            status="active",
            goal="Browse Bilibili",
            browser_session_id="browser-session-1",
            title="哔哩哔哩首页",
            url="https://www.bilibili.com",
            created_at=now,
            updated_at=now,
            expires_at=now + 900,
        )

        try:
            cases = _cases(
                main.project_id,
                game.project_id,
                chess.project_id,
            )
            if args.case:
                requested = set(args.case)
                available = {case.name for case in cases}
                unknown = sorted(requested - available)
                if unknown:
                    raise ValueError(f"unknown case name(s): {', '.join(unknown)}")
                cases = [case for case in cases if case.name in requested]
            histories = _histories(main.project_id, chess.project_id)
            print(
                f"delegate control surface: {max(1, args.repeats)} repeat(s) x "
                f"{len(cases)} cases model={args.model} temperature=0.0 "
                f"mode={'structured' if args.structured_decision else 'tag'}\n"
            )
            with (
                patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
                patch.object(settings, "DELEGATE_FOCUS_INTENT", True),
                patch.object(settings, "DELEGATE_AMEND_INTENT", True),
                patch.object(settings, "DELEGATE_RETRACT_INTENT", True),
                patch.object(settings, "WORK_SCRATCH_ROOT", str(scratch_root)),
                patch("server.work_ledger_coordinator.cwd_in_project_registry", return_value=True),
            ):
                coordinator.set_session_project(SESSION_ID, main.project_id)
                typed_candidates, typed_catalog_complete, _typed_reason = (
                    candidate_catalog_from_coordinator(
                        coordinator,
                        SESSION_ID,
                        project_limit=200,
                        work_item_limit=200,
                    )
                )
                routing_catalog = coordinator.workspace_routing_context(limit=200)
                project_candidates = tuple(
                    ProjectCandidate(
                        str(candidate.get("projectId") or ""),
                        str(candidate.get("projectName") or ""),
                    )
                    for candidate in routing_catalog.get("candidates", [])
                    if isinstance(candidate, dict)
                )
                project_catalog_complete = bool(
                    routing_catalog.get("candidatesComplete")
                    and int(
                        routing_catalog.get("candidateCount")
                        or len(project_candidates)
                    )
                    == len(project_candidates)
                )
                for case in cases:
                    if case.active_browser:
                        branch_coordinator._active_by_session[SESSION_ID] = active_branch
                    else:
                        branch_coordinator._active_by_session.pop(SESSION_ID, None)
                    if args.structured_decision:
                        control_system = augment_system_prompt_for_control_decision(
                            get_structured_control_prompt(),
                            session_id=SESSION_ID,
                        )
                    else:
                        control_system = augment_system_prompt_with_active_provider_context(
                            get_delegate_control_prompt(),
                            session_id=SESSION_ID,
                        )
                    role_system = finalize_system_prompt_language(
                        augment_system_prompt_with_active_provider_context(
                            get_system_prompt("with_delegate"),
                            session_id=SESSION_ID,
                        )
                    )
                    outcomes: list[str] = []
                    for _ in range(max(1, args.repeats)):
                        history = [dict(item) for item in histories[case.history]]
                        control_messages = [
                            {"role": "system", "content": control_system},
                            *history,
                            {"role": "user", "content": case.utterance},
                        ]
                        proposals: list[dict[str, Any]] = []
                        raw_errors: list[str] = []
                        if args.proposal_gated:
                            raw_messages = [
                                {"role": "system", "content": role_system},
                                *history,
                                {"role": "user", "content": case.utterance},
                            ]
                            raw_started = time.monotonic()
                            try:
                                raw_reply = await asyncio.to_thread(
                                    _ask,
                                    raw_messages,
                                    model=args.model,
                                    temperature=args.raw_temperature,
                                )
                            except Exception as exc:
                                infrastructure_failures += 1
                                outcomes.append("RAW_INFRA")
                                print(
                                    f"  RAW_INFRA {case.name}: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                                continue
                            raw_latencies.append(time.monotonic() - raw_started)
                            proposals = delegate_attrs(raw_reply)
                            raw_controls = project_control_actions(
                                normalize_control_actions(proposals)
                            )
                            raw_controls, _raw_rejected = filter_known_fact_controls(
                                raw_controls,
                                provider_ids={"codex", "codex", "browser", "openclaw"},
                                project_ids={
                                    main.project_id,
                                    game.project_id,
                                    game_archive.project_id,
                                    chess.project_id,
                                },
                            )
                            raw_errors = _errors(case, raw_controls)
                            if raw_errors:
                                raw_semantic_failures += 1
                        reply = ""
                        raw_attrs: list[dict[str, Any]] = []
                        normalized_attrs: list[dict[str, Any]] = []
                        merge_notes: list[str] = []
                        reference_dispositions: tuple[str, ...] = ()
                        decision_contract_error = ""
                        candidate_failure_reply = ""
                        if args.structured_decision:
                            from llm.client import remote_llm_messages_query

                            proposal_slots = normalize_control_actions(proposals)
                            if proposal_slots != proposals:
                                structural_normalizations += 1
                            if not proposals:
                                effective_attrs = []
                            else:
                                started = time.monotonic()
                                decision = await resolve_control_decision(
                                    adjudication_messages(control_messages),
                                    proposal_slots,
                                    typed_candidates,
                                    complete=typed_catalog_complete,
                                    query=lambda messages: asyncio.to_thread(
                                        remote_llm_messages_query,
                                        messages,
                                        model=args.model,
                                        temperature=0.0,
                                    ),
                                )
                                latencies.append(time.monotonic() - started)
                                reply = decision.raw_reply or (
                                    f"{decision.status}: {decision.reason}"
                                )
                                if decision.status == "unavailable":
                                    infrastructure_failures += 1
                                if decision.status == "invalid":
                                    structured_invalid += 1
                                structured_protocol_retries += (
                                    decision.candidate_protocol_retries
                                )
                                if decision.status != "ok":
                                    decision_contract_error = (
                                        "control decision status="
                                        f"{decision.status}: {decision.reason}"
                                    )
                                    candidate_failure_reply = (
                                        decision.candidate_failure_reply
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
                                    )
                                )
                                project_reference_rejections += sum(
                                    "Project candidate count=" in note
                                    for note in merge_notes
                                )
                                fact_rejections += sum(
                                    "provider is not registered" in note
                                    for note in merge_notes
                                )
                            completed += 1
                            dispatchable_attrs, reference_dispositions = (
                                _dispatchable_structured_actions(effective_attrs)
                            )
                            for disposition in reference_dispositions:
                                structured_reference_dispositions[disposition] += 1
                            attrs = project_control_actions(dispatchable_attrs)
                            canonical_errors = _errors(case, attrs)
                            errors = _errors(case, dispatchable_attrs)
                            disposition_errors = _disposition_errors(
                                case, reference_dispositions
                            )
                            canonical_errors.extend(disposition_errors)
                            errors.extend(disposition_errors)
                            if decision_contract_error:
                                canonical_errors.append(decision_contract_error)
                                errors.append(decision_contract_error)
                        else:
                            started = time.monotonic()
                            try:
                                reply = await asyncio.to_thread(
                                    _ask,
                                    adjudication_messages(control_messages),
                                    model=args.model,
                                    temperature=0.0,
                                )
                            except Exception as exc:
                                infrastructure_failures += 1
                                outcomes.append("INFRA")
                                print(
                                    f"  INFRA {case.name}: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                                continue
                            latencies.append(time.monotonic() - started)
                            completed += 1
                            raw_attrs = delegate_attrs(reply)
                            normalized_attrs = normalize_control_actions(raw_attrs)
                            if normalized_attrs != raw_attrs:
                                structural_normalizations += 1
                            projected_attrs = project_control_actions(normalized_attrs)
                            attrs, rejected = filter_known_fact_controls(
                                projected_attrs,
                                provider_ids={
                                    "codex",
                                    "codex",
                                    "browser",
                                    "openclaw",
                                },
                                project_ids={
                                    main.project_id,
                                    game.project_id,
                                    game_archive.project_id,
                                },
                            )
                            fact_rejections += len(rejected)
                            canonical_errors = _errors(case, attrs)
                            if args.proposal_gated:
                                effective_attrs, merge_notes = (
                                    merge_proposal_controls(proposals, attrs)
                                )
                                errors = _errors(case, effective_attrs)
                            else:
                                errors = canonical_errors
                            if args.proposal_gated and any(
                                str(action.get("project_id") or "").strip()
                                for action in effective_attrs
                            ):
                                reference_started = time.monotonic()
                                resolution = await resolve_project_reference(
                                    case.utterance,
                                    project_candidates,
                                    complete=project_catalog_complete,
                                    history=history,
                                    query=lambda messages: asyncio.to_thread(
                                        _ask,
                                        messages,
                                        model=args.model,
                                        temperature=0.0,
                                    ),
                                )
                                project_reference_latencies.append(
                                    time.monotonic() - reference_started
                                )
                                if resolution.status == "unavailable":
                                    infrastructure_failures += 1
                                effective_attrs, reference_notes = (
                                    guard_project_bound_actions(
                                        effective_attrs,
                                        resolution,
                                    )
                                )
                                project_reference_rejections += sum(
                                    note.startswith("suppressed ")
                                    for note in reference_notes
                                )
                                project_reference_corrections += sum(
                                    note.startswith("corrected ")
                                    for note in reference_notes
                                )
                                merge_notes.extend(reference_notes)
                                errors = _errors(case, effective_attrs)
                        if args.proposal_gated:
                            if raw_errors and not errors:
                                proposal_corrections += 1
                            elif not raw_errors and errors:
                                proposal_regressions += 1
                        if errors:
                            semantic_failures += 1
                            outcomes.append("FAIL")
                            print(
                                f"  FAIL {case.name}: "
                                + (
                                    f"raw={'; '.join(raw_errors) or 'ok'}; "
                                    if args.proposal_gated
                                    else ""
                                )
                                + f"canonical={'; '.join(canonical_errors) or 'ok'}; "
                                + f"effective={'; '.join(errors)}"
                            )
                            if merge_notes:
                                print(f"       merge: {'; '.join(merge_notes)}")
                            if args.structured_decision:
                                print(
                                    "       references: "
                                    + str(
                                        [
                                            [
                                                candidate.token
                                                for candidate in action.get(
                                                    CONTROL_REFERENCE_CANDIDATES_ATTR,
                                                    (),
                                                )
                                            ]
                                            if isinstance(
                                                action.get(
                                                    CONTROL_REFERENCE_CANDIDATES_ATTR
                                                ),
                                                tuple,
                                            )
                                            else None
                                            for action in effective_attrs
                                        ]
                                    )
                                )
                            print(f"       canonical: {' '.join(reply.split())[:360]}")
                            if candidate_failure_reply:
                                print(
                                    "       candidate-failure-reply: "
                                    f"{' '.join(candidate_failure_reply.split())[:240]}"
                                )
                        else:
                            outcomes.append(
                                "ATTENTION"
                                if "attention" in reference_dispositions
                                else "BLOCKED"
                                if reference_dispositions
                                else "CORRECTED"
                                if args.proposal_gated and raw_errors
                                else "NORM_PASS"
                                if normalized_attrs != raw_attrs
                                else "PASS"
                            )
                    print(f"  {case.name:30s} {'/'.join(outcomes)}")
        finally:
            branch_coordinator._active_by_session.clear()
            interaction_branch_module._current_coordinator = None
            coordinator.close()

    print("\nsummary")
    print(f"  completed decisions     : {completed}")
    if args.proposal_gated:
        print(f"  raw semantic failures   : {raw_semantic_failures}")
    print(f"  semantic failures       : {semantic_failures}")
    print(f"  infrastructure failures : {infrastructure_failures}")
    print(f"  structural normalizations: {structural_normalizations}")
    print(f"  host fact rejections     : {fact_rejections}")
    if args.proposal_gated:
        print(f"  proposal corrections     : {proposal_corrections}")
        print(f"  proposal regressions     : {proposal_regressions}")
        print(f"  project ref rejections   : {project_reference_rejections}")
        print(f"  project ref corrections  : {project_reference_corrections}")
    if args.structured_decision:
        print(f"  invalid structured replies: {structured_invalid}")
        print(f"  candidate protocol retries: {structured_protocol_retries}")
        print(
            "  reference dispositions  : "
            f"{structured_reference_dispositions}"
        )
    if raw_latencies:
        print(f"  role latency median      : {statistics.median(raw_latencies):.2f}s")
    if latencies:
        print(f"  adjudication median     : {statistics.median(latencies):.2f}s")
    if project_reference_latencies:
        print(
            "  project ref median      : "
            f"{statistics.median(project_reference_latencies):.2f}s"
        )
    if completed == 0:
        return 2
    return 1 if semantic_failures or infrastructure_failures else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repeats", nargs="?", type=int, default=2)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--proposal-gated",
        action="store_true",
        help="sample a role proposal and shadow-merge only its canonical controls",
    )
    parser.add_argument("--raw-temperature", type=float, default=0.7)
    parser.add_argument(
        "--structured-decision",
        action="store_true",
        help=(
            "replace the tag adjudicator plus Project resolver with one "
            "proposal-indexed JSON decision (still shadow-only)"
        ),
    )
    parser.add_argument(
        "--case",
        action="append",
        help="run only the named case; repeat the option to select several",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))
