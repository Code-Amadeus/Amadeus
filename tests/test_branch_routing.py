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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.interaction_branch as ib_mod
from server.interaction_branch import (
    InteractionBranchCoordinator,
    InteractionBranchState,
)


def _make_coordinator(runs: list):
    async def fake_provider_run(params):
        runs.append(params)
        return {"run": {"run_id": f"run_{len(runs)}", "status": "running"}}

    return InteractionBranchCoordinator(
        provider_run=fake_provider_run,
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
        assert result is None
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
        assert run_info is not None
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
        assert c.close_active_branch("s1", reason="llm_close") is True
        assert "s1" not in c._active_by_session
        assert c.close_active_branch("s1") is False  # 幂等

    asyncio.run(run())


def test_canonical_provider_handoff_retires_only_a_different_provider_branch():
    runs: list = []
    coordinator = _make_coordinator(runs)
    branch = _make_branch(session_id="handoff-session")
    coordinator._active_by_session["handoff-session"] = branch

    assert (
        coordinator.close_for_provider_handoff(
            "handoff-session",
            next_provider="browser",
        )
        is False
    )
    assert coordinator.active_branch_for_session("handoff-session") is branch

    assert (
        coordinator.close_for_provider_handoff(
            "handoff-session",
            next_provider="openclaw",
        )
        is True
    )
    assert coordinator.active_branch_for_session("handoff-session") is None
    assert branch.status == "closed"
    assert branch.metadata["closed_status"] == "superseded"
    assert branch.metadata["closed_reason"] == "provider_handoff:openclaw"


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
