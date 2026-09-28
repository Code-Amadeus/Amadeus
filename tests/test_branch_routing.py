"""单脑分支路由（branch=continue/new/close）测试。

覆盖：三条结构性快通道、旧关键词误吸场景不再误判、
continue_from_delegate / close_active_branch、_should_start_new_branch
的显式意图判定、work_context 分支状态块渲染。

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_branch_routing.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.interaction_branch as ib_mod
from server.interaction_branch import (
    InteractionBranchCoordinator,
    InteractionBranchContinuationReceipt,
    InteractionBranchRoutingLease,
    InteractionBranchRunStopUnconfirmed,
    InteractionBranchState,
)


def _make_coordinator(runs: list):
    async def fake_provider_run(params):
        runs.append(params)
        return {"run": {"run_id": f"run_{len(runs)}", "status": "running"}}

    async def fake_provider_cancel(run_id: str, **_kwargs):
        return {"cancelled": True, "run": {"run_id": run_id, "status": "cancelled"}}

    return InteractionBranchCoordinator(
        provider_run=fake_provider_run,
        provider_cancel=fake_provider_cancel,
        root=tempfile.mkdtemp(prefix="ib_test_"),
        ttl_seconds=900.0,
    )


def _make_branch(session_id="s1", *, status="active", url="https://www.bilibili.com/video/x",
                 goal="watch amadeus videos") -> InteractionBranchState:
    now = time.time()
    return InteractionBranchState(
        branch_id="br_test_1",
        parent_session_id=session_id,
        provider="browser",
        status=status,
        goal=goal,
        browser_session_id="bs_1",
        title="bilibili video page",
        url=url,
        created_at=now,
        updated_at=now,
        expires_at=now + 900,
    )


def test_structural_fast_paths_only():
    async def run():
        runs: list = []
        c = _make_coordinator(runs)
        branch = _make_branch()
        c._active_by_session["s1"] = branch

        # 旧关键词误吸场景：这些现在全部落回主对话（返回 None），
        # 既不误 continue 也不误杀分支
        for text in (
            "打开心扉聊聊吧",          # 旧: "打开"命中 continuation → 误吸
            "嗯嗯",                    # 旧: noise 表判定
            "点击第一个结果",           # 旧: 关键词 continue —— 现在由主 LLM 发标签
            "look up paxos papers",    # 旧: unanchored search retarget
            "换个话题吧",               # 旧: new_topic → 误杀分支
        ):
            result = await c.try_route_user_message(text=text, session_id="s1")
            assert result is None, f"should defer to main llm: {text!r}"
            assert c._active_by_session.get("s1") is branch, f"branch must survive: {text!r}"

        # 快通道 2：显式 URL 同站 → continue（provider_run 被调用）
        result = await c.try_route_user_message(
            text="https://www.bilibili.com/video/BV1 开这个", session_id="s1"
        )
        assert result is not None and result["handled"]
        assert runs[-1]["metadata"]["branch_intent"] == "continue"

        # 快通道 3：显式 URL 异站 → 分支 superseded，落回主对话
        branch2 = _make_branch(session_id="s2")
        c._active_by_session["s2"] = branch2
        result = await c.try_route_user_message(
            text="打开 https://zh.wikipedia.org/wiki/Amadeus", session_id="s2"
        )
        assert result is not None and result["handled"] is False
        assert result["routing_scope_transition"]["state"] == "absent"
        assert "s2" not in c._active_by_session  # superseded

    asyncio.run(run())


def test_waiting_value_fast_path():
    async def run():
        runs: list = []
        c = _make_coordinator(runs)
        branch = _make_branch(status="waiting_for_user", goal="search this site for a keyword")
        c._active_by_session["s1"] = branch
        result = await c.try_route_user_message(text="Amadeus", session_id="s1")
        assert result is not None and result["handled"]
        # 非等值状态下，同样的短语落回主对话
        runs.clear()
        branch2 = _make_branch(session_id="s3", status="active")
        c._active_by_session["s3"] = branch2
        assert await c.try_route_user_message(text="Amadeus", session_id="s3") is None

    asyncio.run(run())


def test_continue_and_close_from_delegate():
    async def run():
        runs: list = []
        c = _make_coordinator(runs)
        branch = _make_branch()
        c._active_by_session["s1"] = branch

        # continue：后台启动 run（不 await 完成），metadata 带分支身份与意图
        run_info = await c.continue_from_delegate(
            session_id="s1",
            task="Paxos のページをもう一度開く",
            source_user_text="リストの最初の動画を開いて",
            turn_id="t1",
        )
        assert run_info is not None and run_info.accepted
        md = runs[-1]["metadata"]
        requirements = runs[-1]["requirements"]
        assert requirements["task_kind"] == "browser"
        assert requirements["steering"] == "immediate"
        assert requirements["interaction"] == "bidirectional"
        assert md["interaction_branch_id"] == "br_test_1"
        assert md["branch_intent"] == "continue"
        assert md["branch_user_message"] == "リストの最初の動画を開いて"
        # 分支 transcript 记录了本轮指令
        assert branch.visible_messages[-1]["content"] == "リストの最初の動画を開いて"
        assert branch.visible_messages[-1]["source"] == "main_chat_intervention"

        # 无活跃分支的 continue → None（调用方按 new 处理）
        assert await c.continue_from_delegate(session_id="nope", task="x") is None

        # close：关闭并清空
        assert await c.close_active_branch("s1", reason="llm_close") is True
        assert "s1" not in c._active_by_session
        assert await c.close_active_branch("s1") is False  # 幂等

    asyncio.run(run())


def test_turn_start_routing_lease_continues_exact_browser_branch():
    async def run():
        runs: list = []
        c = _make_coordinator(runs)
        branch = _make_branch()
        branch.work_item_id = "work-browser-1"
        c._active_by_session["s1"] = branch
        lease = c.capture_routing_lease("s1")

        assert lease is not None
        assert lease.branch_id == "br_test_1"
        assert InteractionBranchRoutingLease.from_mapping(lease.as_dict()) == lease
        result = await c.continue_from_delegate(
            session_id="s1",
            task="model paraphrase",
            source_user_text="就在刚才页面点 Detail",
            turn_id="turn-detail",
            routing_lease=lease,
        )

        assert result is not None and result.accepted
        assert len(runs) == 1
        metadata = runs[0]["metadata"]
        assert metadata["interaction_branch_id"] == "br_test_1"
        assert metadata["browser_session_id"] == "bs_1"
        assert metadata["continuation"] == "amend"
        assert metadata["work"] == {"work_item_id": "work-browser-1"}
        assert metadata["branch_user_message"] == "就在刚才页面点 Detail"
        close_lease = c.capture_routing_lease("s1")
        assert close_lease is not None
        assert await c.close_from_routing_lease(
            close_lease,
            reason="explicit-close",
        ) is True
        assert c.active_branch_for_session("s1") is None

    asyncio.run(run())


def test_stale_routing_lease_never_binds_a_replacement_branch():
    async def run():
        runs: list = []
        c = _make_coordinator(runs)
        original = _make_branch()
        c._active_by_session["s1"] = original
        lease = c.capture_routing_lease("s1")
        assert lease is not None

        replacement = _make_branch()
        replacement.branch_id = "br_replacement"
        replacement.browser_session_id = "bs_replacement"
        c._active_by_session["s1"] = replacement
        result = await c.continue_from_delegate(
            session_id="s1",
            task="continue the old page",
            source_user_text="继续刚才那个",
            turn_id="stale-turn",
            routing_lease=lease,
        )

        assert result is None
        assert runs == []
        assert replacement.visible_messages == []
        assert await c.close_from_routing_lease(
            lease,
            reason="stale-close",
        ) is False
        assert c.active_branch_for_session("s1") is replacement

    asyncio.run(run())


def test_new_authority_source_is_rechecked_after_browser_session_lock_wait():
    async def run():
        runs: list = []
        c = _make_coordinator(runs)
        branch = _make_branch()
        c._active_by_session["s1"] = branch
        lease = c.capture_routing_lease("s1")
        assert lease is not None
        lock = c._branch_locks.setdefault("s1", asyncio.Lock())
        await lock.acquire()
        live = True

        def require_live():
            if not live:
                raise RuntimeError("source retired while waiting")

        pending = asyncio.create_task(c.continue_from_delegate(
            session_id="s1", task="click", source_user_text="点一下",
            turn_id="new-authority-turn", routing_lease=lease,
            admission_check=require_live))
        await asyncio.sleep(0)
        live = False
        lock.release()
        try:
            await pending
        except RuntimeError as exc:
            assert str(exc) == "source retired while waiting"
        else:
            raise AssertionError("retired source must not mutate the Browser branch")
        assert runs == []
        assert branch.instruction_revision == 0
        assert branch.visible_messages == []

    asyncio.run(run())


def test_advanced_or_expired_routing_lease_fails_closed():
    async def run():
        runs: list = []
        c = _make_coordinator(runs)
        branch = _make_branch()
        c._active_by_session["s1"] = branch
        lease = c.capture_routing_lease("s1")
        assert lease is not None

        branch.instruction_revision += 1
        assert c.resolve_routing_lease(lease) is None
        assert await c.continue_from_delegate(
            session_id="s1",
            task="stale revision",
            routing_lease=lease,
        ) is None
        assert runs == []

        fresh = c.capture_routing_lease("s1")
        assert fresh is not None
        branch.expires_at = time.time() - 1
        assert c.resolve_routing_lease(fresh) is None
        assert runs == []

    asyncio.run(run())


def test_chat_turn_freezes_branch_lease_into_host_control_metadata():
    runs: list = []
    coordinator = _make_coordinator(runs)
    branch = _make_branch()
    coordinator._active_by_session["s1"] = branch
    ib_mod._current_coordinator = coordinator
    try:
        from core.chat_runtime import ChatRuntime, _TurnState

        state = _TurnState(
            gui_callback=None,
            turn_id="turn-lease",
            question="继续刚才的页面",
            session_id="s1",
        )
        assert state.interaction_branch_routing_lease["branch_id"] == "br_test_1"

        action = {"type": "DELEGATE", "attrs": {"provider": "browser"}}
        ChatRuntime._annotate_delegate_source(
            action,
            state.question,
            turn_id=state.turn_id,
            routing_scope_lease=state.interaction_branch_routing_lease,
        )
        assert action["attrs"]["_host_interaction_branch_routing_lease"] == (
            state.interaction_branch_routing_lease
        )
    finally:
        ib_mod._current_coordinator = None


def test_canonical_provider_handoff_retires_only_a_different_provider_branch():
    async def run() -> None:
        runs: list = []
        coordinator = _make_coordinator(runs)
        branch = _make_branch(session_id="handoff-session")
        coordinator._active_by_session["handoff-session"] = branch

        assert await coordinator.close_for_provider_handoff(
            "handoff-session",
            next_provider="browser",
        ) is False
        assert coordinator.active_branch_for_session("handoff-session") is branch

        assert await coordinator.close_for_provider_handoff(
            "handoff-session",
            next_provider="openclaw",
        ) is True
        assert coordinator.active_branch_for_session("handoff-session") is None
        assert branch.status == "closed"
        assert branch.metadata["closed_status"] == "superseded"
        assert branch.metadata["closed_reason"] == "provider_handoff:openclaw"

    asyncio.run(run())


def test_provider_handoff_invalidates_an_inflight_browser_start():
    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled: list[str] = []
        coordinator: InteractionBranchCoordinator

        async def provider_cancel(run_id: str, **_kwargs):
            cancelled.append(run_id)
            return {"cancelled": True}

        async def provider_run(params):
            started.set()
            await release.wait()
            run = {
                "run_id": "late-browser-run",
                "provider": "browser",
                "status": "queued",
                "metadata": dict(params["metadata"]),
            }
            await coordinator._on_provider_event(
                "provider.event",
                {
                    "provider": "browser",
                    "run_id": run["run_id"],
                    "type": "run.created",
                    "payload": {"task": params["task"]},
                    "metadata": dict(params["metadata"]),
                },
            )
            return {"run": run}

        coordinator = InteractionBranchCoordinator(
            provider_run=provider_run,
            provider_cancel=provider_cancel,
            root=tempfile.mkdtemp(prefix="ib_race_"),
        )
        branch = _make_branch(session_id="race-session")
        coordinator._active_by_session["race-session"] = branch
        lease = coordinator.capture_routing_lease("race-session")
        assert lease is not None

        continuation = asyncio.create_task(
            coordinator.continue_from_delegate(
                session_id="race-session",
                task="continue on the page",
                turn_id="race-turn",
                routing_lease=lease,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert await coordinator.close_for_provider_handoff(
            "race-session",
            next_provider="openclaw",
        ) is True
        release.set()
        receipt = await asyncio.wait_for(continuation, timeout=1.0)

        assert receipt is not None
        assert receipt.disposition == "superseded"
        assert cancelled == ["late-browser-run"]
        assert coordinator.active_branch_for_session("race-session") is None
        assert branch.status == "closed"
        assert branch.active_run_id == ""

    asyncio.run(run())


def test_same_routing_lease_can_reserve_only_one_continuation():
    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def provider_run(_params):
            started.set()
            await release.wait()
            return {"run": {"run_id": "reserved-once", "status": "running"}}

        coordinator = InteractionBranchCoordinator(
            provider_run=provider_run,
            root=tempfile.mkdtemp(prefix="ib_reservation_"),
        )
        branch = _make_branch(session_id="reserve-session")
        coordinator._active_by_session["reserve-session"] = branch
        lease = coordinator.capture_routing_lease("reserve-session")
        assert lease is not None

        first = asyncio.create_task(
            coordinator.continue_from_delegate(
                session_id="reserve-session",
                task="first",
                routing_lease=lease,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        second = await coordinator.continue_from_delegate(
            session_id="reserve-session",
            task="second",
            routing_lease=lease,
        )
        release.set()
        first_receipt = await asyncio.wait_for(first, timeout=1.0)

        assert second is None
        assert first_receipt is not None and first_receipt.accepted

    asyncio.run(run())


def test_newer_generation_adopts_same_run_without_stale_finalize_cancelling_it():
    async def run() -> None:
        run_created = asyncio.Event()
        release_first_start = asyncio.Event()
        cancellations: list[str] = []
        steers: list[dict] = []
        coordinator: InteractionBranchCoordinator

        async def provider_cancel(run_id: str, **_kwargs):
            cancellations.append(run_id)
            return {"cancelled": True}

        async def provider_steer(params):
            steers.append(dict(params))
            return {
                "accepted": True,
                "run": {
                    "run_id": params["run_id"],
                    "provider": "browser",
                    "status": "running",
                },
            }

        async def provider_run(params):
            run = {
                "run_id": "shared-run",
                "provider": "browser",
                "status": "queued",
                "metadata": dict(params["metadata"]),
            }
            await coordinator._on_provider_event(
                "provider.event",
                {
                    "provider": "browser",
                    "run_id": "shared-run",
                    "type": "run.created",
                    "payload": {"task": params["task"]},
                    "metadata": dict(params["metadata"]),
                },
            )
            run_created.set()
            await release_first_start.wait()
            return {"run": run}

        coordinator = InteractionBranchCoordinator(
            provider_run=provider_run,
            provider_steer=provider_steer,
            provider_cancel=provider_cancel,
            root=tempfile.mkdtemp(prefix="ib_adopt_"),
        )
        branch = _make_branch(session_id="adopt-session")
        coordinator._active_by_session["adopt-session"] = branch
        first_lease = coordinator.capture_routing_lease("adopt-session")
        assert first_lease is not None

        first = asyncio.create_task(
            coordinator.continue_from_delegate(
                session_id="adopt-session",
                task="start the first continuation",
                routing_lease=first_lease,
            )
        )
        await asyncio.wait_for(run_created.wait(), timeout=1.0)
        second_lease = coordinator.capture_routing_lease("adopt-session")
        assert second_lease is not None
        second = await coordinator.continue_from_delegate(
            session_id="adopt-session",
            task="replace with the newest instruction",
            routing_lease=second_lease,
        )
        release_first_start.set()
        first_receipt = await asyncio.wait_for(first, timeout=1.0)

        assert second is not None and second.accepted
        assert first_receipt is not None
        assert first_receipt.disposition == "superseded"
        assert [item["run_id"] for item in steers] == ["shared-run"]
        assert cancellations == []
        assert coordinator._run_was_semantically_closed("shared-run") is False
        current = coordinator.active_branch_for_session("adopt-session")
        assert current is branch
        assert current.active_run_id == "shared-run"

    asyncio.run(run())


def test_deferred_newer_steer_does_not_cancel_or_disown_original_run():
    async def run() -> None:
        run_created = asyncio.Event()
        release_first_start = asyncio.Event()
        cancellations: list[str] = []
        coordinator: InteractionBranchCoordinator

        async def provider_cancel(run_id: str, **_kwargs):
            cancellations.append(run_id)
            return {"cancelled": True}

        async def provider_steer(params):
            return {
                "accepted": False,
                "reason": "unsafe_boundary",
                "run": {
                    "run_id": params["run_id"],
                    "provider": "browser",
                    "status": "running",
                },
            }

        async def provider_run(params):
            run = {
                "run_id": "original-run",
                "provider": "browser",
                "status": "queued",
                "metadata": dict(params["metadata"]),
            }
            await coordinator._on_provider_event(
                "provider.event",
                {
                    "provider": "browser",
                    "run_id": "original-run",
                    "type": "run.created",
                    "payload": {"task": params["task"]},
                    "metadata": dict(params["metadata"]),
                },
            )
            run_created.set()
            await release_first_start.wait()
            return {"run": run}

        coordinator = InteractionBranchCoordinator(
            provider_run=provider_run,
            provider_steer=provider_steer,
            provider_cancel=provider_cancel,
            root=tempfile.mkdtemp(prefix="ib_deferred_adopt_"),
        )
        branch = _make_branch(session_id="deferred-adopt-session")
        coordinator._active_by_session["deferred-adopt-session"] = branch
        first_lease = coordinator.capture_routing_lease("deferred-adopt-session")
        assert first_lease is not None

        first = asyncio.create_task(
            coordinator.continue_from_delegate(
                session_id="deferred-adopt-session",
                task="start original",
                routing_lease=first_lease,
            )
        )
        await asyncio.wait_for(run_created.wait(), timeout=1.0)
        second_lease = coordinator.capture_routing_lease("deferred-adopt-session")
        assert second_lease is not None
        second = await coordinator.continue_from_delegate(
            session_id="deferred-adopt-session",
            task="newer instruction",
            routing_lease=second_lease,
        )
        release_first_start.set()
        first_receipt = await asyncio.wait_for(first, timeout=1.0)

        assert second is not None and second.disposition == "deferred"
        assert first_receipt is not None and first_receipt.accepted
        assert first_receipt.reason == (
            "provider_run_retained_after_newer_continuation_rejected"
        )
        assert cancellations == []
        assert coordinator._run_was_semantically_closed("original-run") is False
        assert branch.active_run_id == "original-run"

    asyncio.run(run())


def test_unconfirmed_running_cancel_never_reports_handoff_success():
    async def run() -> None:
        stop_steers: list[dict] = []

        async def provider_run(_params):
            raise AssertionError("handoff must not start Browser work")

        async def provider_cancel(_run_id: str, **_kwargs):
            return {
                "cancelled": False,
                "reason": "cancel_unconfirmed",
                "run": {"status": "running"},
            }

        async def provider_steer(params):
            stop_steers.append(dict(params))
            return {"accepted": False, "reason": "run_not_steerable"}

        coordinator = InteractionBranchCoordinator(
            provider_run=provider_run,
            provider_steer=provider_steer,
            provider_cancel=provider_cancel,
            root=tempfile.mkdtemp(prefix="ib_unconfirmed_"),
        )
        branch = _make_branch(session_id="uncertain-session")
        branch.active_run_id = "running-browser"
        coordinator._active_by_session["uncertain-session"] = branch

        try:
            await coordinator.close_for_provider_handoff(
                "uncertain-session",
                next_provider="openclaw",
            )
        except InteractionBranchRunStopUnconfirmed as exc:
            assert exc.branch_id == branch.branch_id
            assert exc.run_id == "running-browser"
            assert "cancel_unconfirmed" in exc.reason
        else:
            raise AssertionError("unconfirmed Browser stop was reported as success")

        assert len(stop_steers) == 1
        assert coordinator.active_branch_for_session("uncertain-session") is None
        assert coordinator.termination_pending_for_session("uncertain-session")
        ib_mod._current_coordinator = coordinator
        try:
            captured = ib_mod.capture_interaction_branch_routing_scope(
                "uncertain-session"
            )
            assert captured["state"] == "quarantined"

            await coordinator._on_provider_result(
                "provider.result",
                {
                    "provider": "browser",
                    "run_id": "running-browser",
                    "status": "done",
                    "metadata": {
                        "session_id": "uncertain-session",
                        "interaction_branch_id": branch.branch_id,
                    },
                },
            )
            assert not coordinator.termination_pending_for_session(
                "uncertain-session"
            )
            recovered = ib_mod.capture_interaction_branch_routing_scope(
                "uncertain-session"
            )
            assert recovered["state"] == "absent"
        finally:
            ib_mod._current_coordinator = None

    asyncio.run(run())


def test_retarget_is_visibly_blocked_when_old_browser_stop_is_unconfirmed():
    async def run() -> None:
        provider_runs: list[dict] = []

        async def provider_run(params):
            provider_runs.append(params)
            return {"run": {"run_id": "unexpected"}}

        async def provider_cancel(_run_id: str, **_kwargs):
            return {
                "cancelled": False,
                "reason": "cancel_pending",
                "run": {"status": "running"},
            }

        async def provider_steer(_params):
            return {"accepted": False, "reason": "run_not_steerable"}

        coordinator = InteractionBranchCoordinator(
            provider_run=provider_run,
            provider_steer=provider_steer,
            provider_cancel=provider_cancel,
            root=tempfile.mkdtemp(prefix="ib_retarget_uncertain_"),
        )
        branch = _make_branch(session_id="retarget-session")
        branch.active_run_id = "old-running-browser"
        coordinator._active_by_session["retarget-session"] = branch

        result = await coordinator.try_route_user_message(
            text="Open https://different.example/new",
            session_id="retarget-session",
            turn_id="retarget-turn",
        )

        assert result is not None
        assert result["handled"] is True
        assert result["route_kind"] == "browser_retarget_blocked"
        assert "run_stop_unconfirmed" in result["continuation_reason"]
        assert provider_runs == []

    asyncio.run(run())


def test_late_tombstoned_event_cannot_mutate_idle_replacement_branch():
    async def run() -> None:
        coordinator = _make_coordinator([])
        replacement = _make_branch(session_id="late-event-session")
        replacement.branch_id = "branch-b"
        replacement.browser_session_id = "browser-b"
        replacement.active_run_id = ""
        replacement.last_run_id = "run-b"
        coordinator._active_by_session["late-event-session"] = replacement
        coordinator._mark_branch_semantically_closed("branch-a")
        coordinator._mark_run_semantically_closed("run-a")

        await coordinator._on_provider_event(
            "provider.event",
            {
                "provider": "browser",
                "run_id": "run-a",
                "type": "run.status",
                "payload": {
                    "stage": "steer_applied",
                    "revision": 99,
                    "browser_session_id": "browser-a",
                },
                "metadata": {
                    "session_id": "late-event-session",
                    "interaction_branch_id": "branch-a",
                },
            },
        )

        assert replacement.browser_session_id == "browser-b"
        assert replacement.applied_instruction_revision == 0
        assert coordinator.active_branch_for_session("late-event-session") is replacement

    asyncio.run(run())


def test_new_browser_run_is_cancelled_when_prior_run_stop_is_unconfirmed():
    async def run() -> None:
        cancellations: list[str] = []

        async def provider_run(_params):
            raise AssertionError("event test does not start through coordinator")

        async def provider_cancel(run_id: str, **_kwargs):
            cancellations.append(run_id)
            if run_id == "old-run":
                return {
                    "cancelled": False,
                    "reason": "cancel_unconfirmed",
                    "run": {"status": "running"},
                }
            return {"cancelled": True, "run": {"status": "cancelled"}}

        async def provider_steer(_params):
            return {"accepted": False, "reason": "run_not_steerable"}

        coordinator = InteractionBranchCoordinator(
            provider_run=provider_run,
            provider_steer=provider_steer,
            provider_cancel=provider_cancel,
            root=tempfile.mkdtemp(prefix="ib_replace_uncertain_"),
        )
        old = _make_branch(session_id="replace-session")
        old.branch_id = "old-branch"
        old.active_run_id = "old-run"
        coordinator._active_by_session["replace-session"] = old

        await coordinator._on_provider_event(
            "provider.event",
            {
                "provider": "browser",
                "run_id": "new-run",
                "type": "run.created",
                "payload": {"task": "new page"},
                "metadata": {
                    "source": "llm_delegate",
                    "session_id": "replace-session",
                    "interaction_branch_id": "new-branch",
                    "branch_intent": "new",
                },
            },
        )

        assert cancellations == ["old-run", "new-run"]
        assert coordinator.active_branch_for_session("replace-session") is None
        assert coordinator._run_was_semantically_closed("new-run") is True

        await coordinator._on_provider_event(
            "provider.event",
            {
                "provider": "browser",
                "run_id": "later-run",
                "type": "run.created",
                "payload": {"task": "must stay quarantined"},
                "metadata": {
                    "source": "llm_delegate",
                    "session_id": "replace-session",
                    "interaction_branch_id": "later-branch",
                    "branch_intent": "new",
                },
            },
        )
        assert cancellations == ["old-run", "new-run", "later-run"]
        assert coordinator.active_branch_for_session("replace-session") is None

        await coordinator._on_provider_event(
            "provider.event",
            {
                "provider": "browser",
                "run_id": "old-run",
                "type": "run.finished",
                "payload": {"status": "done"},
                "metadata": {"session_id": "replace-session"},
            },
        )
        assert not coordinator.termination_pending_for_session("replace-session")

        await coordinator._on_provider_event(
            "provider.event",
            {
                "provider": "browser",
                "run_id": "recovered-run",
                "type": "run.created",
                "payload": {"task": "allowed after terminal"},
                "metadata": {
                    "source": "llm_delegate",
                    "session_id": "replace-session",
                    "interaction_branch_id": "recovered-branch",
                    "branch_intent": "new",
                },
            },
        )
        recovered = coordinator.active_branch_for_session("replace-session")
        assert recovered is not None
        assert recovered.branch_id == "recovered-branch"
        assert recovered.active_run_id == "recovered-run"

    asyncio.run(run())


def test_direct_router_never_adopts_branch_after_admitted_absence():
    async def run() -> None:
        runs: list = []
        coordinator = _make_coordinator(runs)
        absent_scope = {
            "state": "absent",
            "parent_session_id": "scope-session",
            "captured_at": time.time(),
        }
        appeared = _make_branch(
            session_id="scope-session",
            url="https://example.test/current",
        )
        appeared.branch_id = "appeared-after-admission"
        coordinator._active_by_session["scope-session"] = appeared

        result = await coordinator.try_route_user_message(
            text="Open https://example.test/next",
            session_id="scope-session",
            turn_id="old-absent-turn",
            routing_scope=absent_scope,
        )

        assert result is None
        assert runs == []
        assert coordinator.active_branch_for_session("scope-session") is appeared

    asyncio.run(run())


def test_routing_scope_capture_failure_is_explicit_and_fail_closed():
    async def run() -> None:
        from unittest.mock import patch

        from core.chat_runtime import (
            _capture_interaction_branch_routing_lease as capture_runtime_scope,
        )
        from server import app as server_app
        from server.handlers.chat_handler import ChatHandler

        with patch(
            "server.interaction_branch.capture_interaction_branch_routing_scope",
            side_effect=RuntimeError("capture failed"),
        ):
            runtime_scope = capture_runtime_scope("capture-session")
            handler_scope = ChatHandler._capture_interaction_branch_routing_lease(
                "capture-session"
            )

        for scope in (runtime_scope, handler_scope):
            assert scope["state"] == "invalid"
            assert scope["parent_session_id"] == "capture-session"
            assert scope["reason"] == "routing_scope_capture_failed"

        async def ignore_block(**_kwargs) -> None:
            return None

        previous = ib_mod._current_coordinator
        ib_mod._current_coordinator = None
        try:
            unavailable_scope = ib_mod.capture_interaction_branch_routing_scope(
                "capture-session"
            )
            assert unavailable_scope["state"] == "invalid"
            attrs = {
                "provider": "browser",
                "branch": "continue",
                "_host_interaction_branch_routing_lease": unavailable_scope,
            }
            with (
                patch(
                    "core.session_manager.get_current_session_id",
                    return_value="capture-session",
                ),
                patch.object(
                    server_app,
                    "_announce_interaction_branch_lease_block",
                    new=ignore_block,
                ),
            ):
                consumed = await server_app._consume_captured_interaction_branch_intent(
                    "continue the browser action",
                    attrs,
                    preflight_only=True,
                )
            assert consumed is True
            assert attrs["_host_interaction_branch_scope_disposition"] == "blocked"
        finally:
            ib_mod._current_coordinator = previous

    asyncio.run(run())


def test_confirmed_retarget_evolves_same_turn_scope_to_absent():
    async def run() -> None:
        from unittest.mock import patch

        from server.app import _consume_captured_interaction_branch_intent
        from server.handlers.chat_handler import ChatHandler

        coordinator = _make_coordinator([])
        branch = _make_branch(
            session_id="retarget-scope-session",
            url="https://example.test/old",
        )
        coordinator._active_by_session["retarget-scope-session"] = branch
        lease = coordinator.capture_routing_lease("retarget-scope-session")
        assert lease is not None
        scope = {"state": "bound", **lease.as_dict()}
        handler = ChatHandler()
        handler.configure(
            stream_llm_query=lambda *_args, **_kwargs: None,
            pending_sentence_items=None,
            interaction_branch_router=coordinator.try_route_user_message,
        )

        direct = await handler._try_interaction_branch_route(
            text="Open https://different.test/new",
            turn_id="retarget-scope-turn",
            session_id="retarget-scope-session",
            routing_scope=scope,
        )
        assert direct is None
        assert scope["state"] == "absent"
        assert scope["transitioned_from_branch_id"] == branch.branch_id
        assert coordinator.active_branch_for_session("retarget-scope-session") is None

        ib_mod._current_coordinator = coordinator
        try:
            with patch(
                "core.session_manager.get_current_session_id",
                return_value="retarget-scope-session",
            ):
                attrs = {
                    "provider": "browser",
                    "branch": "new",
                    "_host_interaction_branch_routing_lease": scope,
                }
                assert await _consume_captured_interaction_branch_intent(
                    "open the new page",
                    attrs,
                    preflight_only=True,
                ) is False
        finally:
            ib_mod._current_coordinator = None

    asyncio.run(run())


def test_direct_fast_path_does_not_claim_unknown_execution_never_started():
    async def run() -> None:
        coordinator = _make_coordinator([])
        branch = _make_branch(session_id="truth-session")
        coordinator._active_by_session["truth-session"] = branch
        lease = coordinator.capture_routing_lease("truth-session")
        assert lease is not None

        async def blocked(*_args, **_kwargs):
            return InteractionBranchContinuationReceipt(
                disposition="superseded",
                reason="branch_generation_superseded_during_start",
                branch_id=branch.branch_id,
                instruction_revision=1,
                run={"run_id": "maybe-started"},
                execution_started=None,
            )

        coordinator._continue_branch = blocked  # type: ignore[method-assign]
        result = await coordinator.try_route_user_message(
            text="Open https://www.bilibili.com/video/new",
            session_id="truth-session",
            turn_id="truth-turn",
            routing_scope={"state": "bound", **lease.as_dict()},
        )

        assert result is not None
        assert result["execution_uncertain"] is True
        assert "Nothing new was started" not in result["display_text"]

    asyncio.run(run())


def test_accepted_revision_survives_newer_deferred_proposal_and_late_apply():
    async def run() -> None:
        coordinator = _make_coordinator([])
        branch = _make_branch(session_id="revision-session")
        branch.active_run_id = "revision-run"
        branch.instruction_revision = 3
        branch.accepted_instruction_revision = 2
        coordinator._active_by_session["revision-session"] = branch

        await coordinator._on_provider_event(
            "provider.event",
            {
                "provider": "browser",
                "run_id": "revision-run",
                "type": "run.status",
                "payload": {"stage": "steer_applied", "revision": 2},
                "metadata": {
                    "session_id": "revision-session",
                    "interaction_branch_id": branch.branch_id,
                },
            },
        )

        assert branch.instruction_revision == 3
        assert branch.accepted_instruction_revision == 2
        assert branch.applied_instruction_revision == 2
        assert branch.metadata["steering"]["state"] == "applied"

    asyncio.run(run())


def test_expired_active_branch_never_yields_a_usable_lease():
    async def run() -> None:
        runs: list = []
        coordinator = _make_coordinator(runs)
        branch = _make_branch(session_id="expired-active-session")
        branch.active_run_id = "expired-active-run"
        branch.expires_at = time.time() - 1
        coordinator._active_by_session["expired-active-session"] = branch

        captured = coordinator.capture_routing_lease("expired-active-session")
        assert captured is not None
        assert coordinator.resolve_routing_lease(captured) is None
        assert await coordinator.continue_from_delegate(
            session_id="expired-active-session",
            task="must not steer",
            routing_lease=captured,
        ) is None
        assert runs == []
        assert InteractionBranchRoutingLease.from_mapping(
            {
                "branch_id": "b",
                "parent_session_id": "s",
                "provider": "browser",
                "instruction_revision": 1,
                "expires_at": 0,
            }
        ) is None
        assert InteractionBranchRoutingLease.from_mapping(
            {
                "branch_id": "b",
                "parent_session_id": "s",
                "provider": "browser",
                "instruction_revision": 1,
                "expires_at": float("nan"),
            }
        ) is None

    asyncio.run(run())


def test_unrelated_terminal_result_cannot_mutate_replacement_branch():
    async def run() -> None:
        coordinator = _make_coordinator([])
        replacement = _make_branch(session_id="result-session")
        replacement.branch_id = "branch-b"
        replacement.browser_session_id = "browser-b"
        replacement.active_run_id = "run-b"
        coordinator._active_by_session["result-session"] = replacement

        await coordinator._on_provider_result(
            "provider.result",
            {
                "provider": "browser",
                "run_id": "run-a",
                "status": "cancelled",
                "metadata": {
                    "session_id": "result-session",
                    "interaction_branch_id": "branch-a",
                    "browser": {
                        "browser_session_id": "browser-a",
                        "closed": True,
                    },
                },
            },
        )

        assert coordinator.active_branch_for_session("result-session") is replacement
        assert replacement.status == "active"
        assert replacement.active_run_id == "run-b"
        assert replacement.browser_session_id == "browser-b"

    asyncio.run(run())


def test_provider_admission_token_is_bound_to_one_provider():
    async def run() -> None:
        coordinator = _make_coordinator([])
        scope = {
            "state": "absent",
            "parent_session_id": "admission-session",
        }
        accepted, _reason = await coordinator.provider_start_admission(
            scope,
            session_id="admission-session",
            provider="browser",
            reservation_id="same-token",
            phase="reserve",
        )
        assert accepted is True
        reused, reason = await coordinator.provider_start_admission(
            scope,
            session_id="admission-session",
            provider="openclaw",
            reservation_id="same-token",
            phase="reserve",
        )
        assert reused is False
        assert reason == "provider_admission_provider_mismatch"
        released, _reason = await coordinator.provider_start_admission(
            scope,
            session_id="admission-session",
            provider="browser",
            reservation_id="same-token",
            phase="release",
        )
        assert released is True

    asyncio.run(run())


def test_branch_persistence_cannot_escape_its_storage_root():
    with tempfile.TemporaryDirectory(prefix="branch_storage_root_") as temp_root:
        root = os.path.join(temp_root, "branches")
        coordinator = InteractionBranchCoordinator(
            provider_run=lambda _params: None,  # type: ignore[arg-type]
            root=root,
        )
        branch = _make_branch(session_id="storage-session")
        branch.branch_id = "../escaped"

        coordinator._persist(branch)

        assert not os.path.exists(os.path.join(temp_root, "escaped.json"))
        stored = list(Path(root).glob("branch_*.json"))
        assert len(stored) == 1
        assert stored[0].resolve().parent == Path(root).resolve()


def test_should_start_new_branch_intent():
    runs: list = []
    c = _make_coordinator(runs)
    branch = _make_branch()

    def check(metadata, expected):
        got = c._should_start_new_branch(
            branch, metadata=metadata, provider_branch={}, run_id="r9",
            title="", url="",
        )
        assert got is expected, (metadata, got)

    # 显式 continue 意图：绝不 supersede
    check({"source": "llm_delegate", "branch_intent": "continue"}, False)
    # 显式 new 意图：supersede
    check({"source": "llm_delegate", "branch_intent": "new"}, True)
    # 缺省意图 + llm_delegate：保持旧行为（开新）
    check({"source": "llm_delegate"}, True)
    # 同分支 id：从不 supersede（原有规则保留）
    check({"interaction_branch_id": "br_test_1", "branch_intent": "new"}, False)


def test_branch_routing_context_block():
    runs: list = []
    c = _make_coordinator(runs)
    c.configure()  # 注册模块单例
    try:
        branch = _make_branch()
        c._active_by_session["s1"] = branch
        from server.work_context import render_branch_routing_context

        block = render_branch_routing_context("s1")
        assert "[Active browser branch]" in block
        assert "bilibili" in block
        assert 'branch="continue"' in block
        assert "not the presumed subject" in block
        assert "An unrelated goal does not inherit this branch" in block
        # 无分支会话 → 空块（不污染 prompt）
        assert render_branch_routing_context("other") == ""
        assert render_branch_routing_context(None) == ""
    finally:
        ib_mod._current_coordinator = None


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all branch routing tests passed")


if __name__ == "__main__":
    _main()
