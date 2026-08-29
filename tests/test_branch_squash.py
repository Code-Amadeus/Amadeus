"""分支区间 squash-merge（对白保留语义）测试。

覆盖：入口 checkpoint 加深与区间起点、操作轮打标、关闭时坍缩
（保留正常对白与区间前历史）、scope guard、开关关闭。

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_branch_squash.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import session_manager as sm
from server.interaction_branch import (
    InteractionBranchCoordinator,
    InteractionBranchState,
)


class _SessionSandbox:
    """临时接管 session_manager 单例状态：不落盘、测试后完全还原。"""

    def __init__(self, session_id: str):
        self.session_id = session_id

    def __enter__(self):
        self._saved_dialog = list(sm.conversation_history.dialog)
        self._saved_sid = sm.get_current_session_id()
        self._saved_save = sm.save_session
        sm.conversation_history.dialog.clear()
        sm.set_current_session_id(self.session_id)
        sm.save_session = lambda *a, **k: None  # 不写文件
        return sm.conversation_history.dialog

    def __exit__(self, *exc):
        sm.save_session = self._saved_save
        sm.conversation_history.dialog[:] = self._saved_dialog
        sm.set_current_session_id(self._saved_sid)
        return False


def _make_coordinator():
    async def fake_provider_run(params):
        return {"run": {}}

    return InteractionBranchCoordinator(
        provider_run=fake_provider_run,
        root=tempfile.mkdtemp(prefix="ib_squash_"),
    )


def _make_branch(session_id, *, region_start=0) -> InteractionBranchState:
    now = time.time()
    return InteractionBranchState(
        branch_id="br_sq_1",
        parent_session_id=session_id,
        provider="browser",
        status="active",
        goal="B站でAmadeus動画を探す",
        browser_session_id="bs_1",
        title="bilibili",
        url="https://www.bilibili.com/x",
        region_start_index=region_start,
        created_at=now,
        updated_at=now,
        expires_at=now + 900,
    )


def test_squash_preserves_dialogue_and_pre_region():
    c = _make_coordinator()
    with _SessionSandbox("sq_s1") as dialog:
        # 区间前的闲聊（region_start=2）
        dialog.append({"role": "user", "content": "今日は疲れたな"})
        dialog.append({"role": "assistant", "content": "お疲れさま。少し休んだら？"})
        # ── 分支开启（开分支对白不打标 → 保留）──
        dialog.append({"role": "user", "content": "B站でAmadeusの動画探して"})
        dialog.append({"role": "assistant", "content": "いいわよ、開いてみるわね。"})
        # 操作轮 1（打标 → 坍缩）
        dialog.append({"role": "user", "content": "検索して", "branch_id": "br_sq_1"})
        dialog.append({"role": "assistant", "content": "検索するわ。", "branch_id": "br_sq_1"})
        # 分支期间的正常对白（无标签 → 保留）
        dialog.append({"role": "user", "content": "そういえばこのUP主どう思う？"})
        dialog.append({"role": "assistant", "content": "解説は丁寧だけど、少し冗長ね。"})
        # 操作轮 2（打标 → 坍缩）
        dialog.append({"role": "user", "content": "最初の結果を開いて", "branch_id": "br_sq_1"})
        dialog.append({"role": "assistant", "content": "開くわね。", "branch_id": "br_sq_1"})

        branch = _make_branch("sq_s1", region_start=2)
        branch.hidden_summary = "Amadeus動画を検索し最初の結果を再生"
        branch.actions = [{"a": 1}, {"a": 2}, {"a": 3}]
        c._active_by_session["sq_s1"] = branch
        c._close_branch(branch, status="closed", reason="test")

        contents = [str(e.get("content", ""))[:14] for e in dialog]
        # 区间前 2 条 + 开分支对白 2 条 + 胶囊 + 中途正常对白 2 条 = 7 条
        assert len(dialog) == 7, contents
        assert dialog[0]["content"] == "今日は疲れたな"
        assert dialog[2]["content"] == "B站でAmadeusの動画探して"      # 开分支对白保留
        assert dialog[3]["content"] == "いいわよ、開いてみるわね。"
        assert dialog[4]["content"].startswith("[BRANCH_SUMMARY]")     # 胶囊在首个被移除位置
        assert "Amadeus動画を検索し最初の結果を再生" in dialog[4]["content"]
        assert dialog[4].get("branch_capsule") == "br_sq_1"
        assert dialog[5]["content"] == "そういえばこのUP主どう思う？"   # 中途对白保留
        assert dialog[6]["content"] == "解説は丁寧だけど、少し冗長ね。"
        # 所有打标条目已坍缩
        assert not any(e.get("branch_id") == "br_sq_1" for e in dialog)


def test_squash_scope_guard_and_flag():
    c = _make_coordinator()
    # 会话已切换 → 不动历史
    with _SessionSandbox("sq_other") as dialog:
        dialog.append({"role": "user", "content": "x", "branch_id": "br_sq_1"})
        branch = _make_branch("sq_s2", region_start=0)  # parent != current
        c._close_branch(branch, status="closed", reason="test")
        assert len(dialog) == 1 and dialog[0]["content"] == "x"

    # region 未记录（-1）→ 不动历史
    with _SessionSandbox("sq_s3") as dialog:
        dialog.append({"role": "user", "content": "y", "branch_id": "br_sq_1"})
        branch = _make_branch("sq_s3", region_start=-1)
        c._close_branch(branch, status="closed", reason="test")
        assert len(dialog) == 1

    # 开关关闭 → 不动历史
    os.environ["BRANCH_SQUASH_MERGE"] = "0"
    try:
        import importlib
        import config.settings as settings_mod
        importlib.reload(settings_mod)
        with _SessionSandbox("sq_s4") as dialog:
            dialog.append({"role": "user", "content": "z", "branch_id": "br_sq_1"})
            branch = _make_branch("sq_s4", region_start=0)
            c._close_branch(branch, status="closed", reason="test")
            assert len(dialog) == 1
    finally:
        os.environ.pop("BRANCH_SQUASH_MERGE", None)
        importlib.reload(settings_mod)


def test_checkpoint_records_region_start_and_deep_history():
    with _SessionSandbox("sq_s5") as dialog:
        for i in range(20):
            dialog.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"msg{i}"})
        cp = InteractionBranchCoordinator._checkpoint_for_session(
            session_id="sq_s5", user_intent="open bilibili"
        )
        assert cp["region_start_index"] == 20
        # 默认窗口 16 条（BRANCH_CHECKPOINT_MESSAGES）
        assert len(cp["recent_messages"]) == 16
        assert cp["recent_messages"][0]["content"] == "msg4"
        assert cp["recent_messages"][-1]["content"] == "msg19"


def test_turnstate_marks_branch_continue():
    from core.chat_runtime import ChatRuntime, _TurnState

    rt = ChatRuntime()

    def fresh_st():
        # 解析器每轮只认第一个 DELEGATE（_delegate_seen 截断），
        # 每个场景必须用新的 _TurnState/parser
        return _TurnState(gui_callback=None, turn_id="t1")

    # This test owns only the parser's branch marker. Host dispatch is covered
    # by test_host_action_dispatcher and must not be silently wired here.
    with patch("core.chat_runtime.record_actions", return_value=None):
        # 普通 DELEGATE（无 branch 属性）不打标
        st = fresh_st()
        rt._consume_stream_chunk(st, '[DELEGATE provider="openclaw" task="調べて"]')
        assert st.branch_continue_seen is False
        # branch=new 不打标（开分支对白保留）
        st = fresh_st()
        rt._consume_stream_chunk(st, '[DELEGATE provider="browser" branch="new" task="開いて"]')
        assert st.branch_continue_seen is False
        # branch=continue 打标
        st = fresh_st()
        rt._consume_stream_chunk(st, '待ってて。[DELEGATE provider="browser" branch="continue" task="クリック"]')
        assert st.branch_continue_seen is True


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all branch squash tests passed")


if __name__ == "__main__":
    _main()
