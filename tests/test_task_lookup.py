"""Retrieval replacing injection: finding the task the user meant.

Two facts set this up. The roster carries at most five rows sharing one budget
with the persona, and with candidate rows off by default it carries no task
identifiers at all -- so the main chat has never had a way to name past work.
And `intent="report"` only ever refused to start work: the log line claiming it
answered from the ledger was aspiration, since nothing read the ledger.

The tempting fix -- keep a few recent tasks injected and let the model say when
the one being asked about is missing -- was measured on 2026-08-02 and does not
hold: 3 times in 9 the model answered about the closest task in the list
instead, once inventing one outright. So absence is never the model's judgement
to make. The host sees the user's words first, and resolves before the model
speaks; by the time the model answers, the right task is simply present.

Runs standalone through tools/run_tests.py and is also pytest-compatible.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_runtime import runtime as provider_runtime
from agent_host.provider_types import ProviderRunRequest
from agent_host.work_ledger_store import WorkLedgerStore
from core.chat_runtime import ChatRuntime, _TurnState
from server import task_lookup
from server.work_context import _status_phrase, render_conversation_work_context
from server.work_ledger_coordinator import WorkLedgerCoordinator

SESSION = "s-lookup"


def test_cancel_pending_status_is_not_rendered_as_stopped_or_plain_running() -> None:
    status = _status_phrase(
        {
            "execution": "running",
            "activity_phase": "cancelling",
            "activity_liveness": {"state": "cancel_pending"},
            "attention": "none",
            "completion": "unknown",
        }
    )
    assert status.startswith("cancel_pending")
    assert "not yet confirmed" in status

# The oldest task is the target, and its title is a harness preamble rather
# than anything containing the filename -- the shape a repaired first delegate
# actually produced on 2026-08-01. Only what the task *produced* can find it.
CONVERSATION = [
    ("这是路由协议测试；不要在主对话中直接执行任务…", "theme.txt"),
    ("给 notes.md 记录会议纪要", "notes.md"),
    ("调研 Rust 的 async 运行时选型", "async.md"),
    ("调研 SQLite WAL 模式的取舍", "wal.md"),
    ("重构 chat_runtime 的标签解析", "parser.py"),
    ("给 amend.txt 追加一行 two", "amend.txt"),
    ("修复壁纸键盘音的并发释放", "audio.py"),
    ("导出结果到 out/result.txt", "result.txt"),
]


def _seed(store: WorkLedgerStore, root: Path, session_id: str = SESSION) -> list[str]:
    """One conversation's worth of finished Codex tasks, oldest first."""

    coordinator = WorkLedgerCoordinator(store)
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for title, filename in CONVERSATION:
        prepared = coordinator.prepare_request(
            ProviderRunRequest(
                provider="codex",
                task=title,
                cwd=str(workspace),
                mode="agent",
                metadata={"source": "lookup-test", "session_id": session_id},
            )
        )
        work = prepared.metadata["work"]
        work_item_id = str(work["work_item_id"])
        store.update_attempt(str(work["attempt_id"]), execution_status="succeeded")
        store.register_artifact(
            work_item_id,
            attempt_id=str(work["attempt_id"]),
            kind="business.file",
            path=workspace / filename,
        )
        ids.append(work_item_id)
    return ids


def _window(store: WorkLedgerStore, keep: int):
    """Shrink the recency scan to its newest ``keep`` items.

    A real window overflow needs thousands of rows; crippling the scan
    reproduces the same condition -- the target exists but the recency path
    cannot see it -- without seeding a ledger that size.
    """

    original = store.list_work_items

    def limited(**kwargs):
        kwargs.pop("limit", None)
        return original(limit=2000, **kwargs)[:keep]

    return patch.object(store, "list_work_items", side_effect=limited)


def test_a_task_outside_the_recency_window_is_still_found() -> None:
    with tempfile.TemporaryDirectory(prefix="task_lookup_window_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            ids = _seed(store, root)
            target = ids[0]
            coordinator = WorkLedgerCoordinator(store)
            with _window(store, 5):
                window = coordinator.conversation_work_items_for_resolution(SESSION)
                seen = {str(row.get("work_item_id")) for row in window["items"]}
                assert target not in seen, "the window must not already carry the target"

                found = coordinator.conversation_work_items_by_file(SESSION, "theme.txt")
            assert [row["work_item_id"] for row in found] == [target]
            # Zero rows means the task does not exist, not that it scrolled out
            # of reach -- which is the whole difference from the window.
            assert coordinator.conversation_work_items_by_file(SESSION, "nope.txt") == []


def test_the_index_does_not_cross_conversations() -> None:
    with tempfile.TemporaryDirectory(prefix="task_lookup_session_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            mine = _seed(store, root)
            _seed(store, root / "other", session_id="s-other")
            coordinator = WorkLedgerCoordinator(store)
            found = coordinator.conversation_work_items_by_file(SESSION, "theme.txt")
            assert [row["work_item_id"] for row in found] == [mine[0]]


def test_approved_desktop_export_is_a_produced_file_for_amend_lookup() -> None:
    """Two-phase exports are deliverables, even without a business.file row."""

    with tempfile.TemporaryDirectory(prefix="task_lookup_export_") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        workspace.mkdir()
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            coordinator = WorkLedgerCoordinator(store)
            prepared = coordinator.prepare_request(
                ProviderRunRequest(
                    provider="codex",
                    task="Build an endless game",
                    cwd=str(workspace),
                    mode="agent",
                    metadata={"source": "lookup-test", "session_id": SESSION},
                )
            )
            work = prepared.metadata["work"]
            work_item_id = str(work["work_item_id"])
            store.update_attempt(str(work["attempt_id"]), execution_status="succeeded")
            store.register_artifact(
                work_item_id,
                attempt_id=str(work["attempt_id"]),
                kind="business.export",
                title="Export endless_game.html to Desktop",
                path=root / "Desktop" / "endless_game.html",
                status="approved",
                sha256="a" * 64,
            )

            found = coordinator.conversation_work_items_by_file(
                SESSION,
                "endless_game.html",
            )
            assert [row["work_item_id"] for row in found] == [work_item_id]
            assert found[0]["files"] == ["endless_game.html"]


def test_amend_binds_to_a_target_the_window_lost() -> None:
    """The 9/9 amend result expires the moment its target leaves the window.

    Resolution searched the same recency roster, so a follow-up naming a task
    that had scrolled past matched nothing -- and nothing matching means new
    work, silently, in a fresh worktree.
    """

    with tempfile.TemporaryDirectory(prefix="task_lookup_amend_") as temp:
        root = Path(temp)
        with WorkLedgerStore(root / "ledger.sqlite3") as store:
            ids = _seed(store, root)
            coordinator = WorkLedgerCoordinator(store)
            coordinator.configure()
            try:
                utterance = "把 theme.txt 里的 color 改成 green"

                def ground() -> dict:
                    action = {
                        "type": "DELEGATE",
                        "attrs": {
                            "provider": "codex",
                            "intent": "amend",
                            "task": utterance,
                        },
                    }
                    with (
                        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
                        patch.object(settings, "DELEGATE_AMEND_INTENT", True),
                        patch.object(
                            provider_runtime,
                            "provider_manifests",
                            return_value=(CODEX_APP_SERVER_MANIFEST,),
                        ),
                        _window(store, 5),
                    ):
                        ChatRuntime._ground_present_provider_delegate(
                            action, utterance, session_id=SESSION
                        )
                    return action["attrs"]

                with patch.object(settings, "TASK_LOOKUP_ENABLED", False):
                    without = ground()
                assert "workspace_ref" not in without, (
                    "today's behaviour: the window cannot see it, so this forks a task"
                )

                with patch.object(settings, "TASK_LOOKUP_ENABLED", True):
                    with_lookup = ground()
                assert with_lookup["workspace_ref"] == ids[0]
                assert with_lookup["task"].startswith("目标文件是 theme.txt。")
            finally:
                coordinator.close()


def test_the_switch_off_leaves_the_resolution_path_untouched() -> None:
    """Off must mean the recency roster, unchanged, including its fail-closed rule."""

    calls: list[str] = []

    def roster(session_id: str):
        calls.append(session_id)
        return None, [{"work_item_id": "w1", "title": "create theme.txt", "files": []}], True

    action = {
        "type": "DELEGATE",
        "attrs": {"provider": "codex", "intent": "amend", "task": "改 theme.txt"},
    }
    with (
        patch.object(settings, "TASK_LOOKUP_ENABLED", False),
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
        patch.object(settings, "DELEGATE_AMEND_INTENT", True),
        patch.object(
            provider_runtime,
            "provider_manifests",
            return_value=(CODEX_APP_SERVER_MANIFEST,),
        ),
        patch("core.chat_runtime._load_conversation_resolution_roster", side_effect=roster),
    ):
        bound = ChatRuntime._ground_present_provider_delegate(
            action, "改 theme.txt", session_id=SESSION
        )
    assert bound is True and calls == [SESSION]
    assert action["attrs"]["workspace_ref"] == "w1"

    # A saturated window still fails closed rather than guessing.
    with (
        patch.object(settings, "TASK_LOOKUP_ENABLED", False),
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
        patch.object(settings, "DELEGATE_AMEND_INTENT", True),
        patch.object(
            provider_runtime,
            "provider_manifests",
            return_value=(CODEX_APP_SERVER_MANIFEST,),
        ),
        patch(
            "core.chat_runtime._load_conversation_resolution_roster",
            return_value=(None, [], False),
        ),
    ):
        blocked = {
            "type": "DELEGATE",
            "attrs": {"provider": "codex", "intent": "amend", "task": "改 theme.txt"},
        }
        assert (
            ChatRuntime._ground_present_provider_delegate(
                blocked, "改 theme.txt", session_id=SESSION
            )
            is False
        )

    # And the pre-turn pass is inert, leaving nothing for the roster to read.
    async def run() -> None:
        with patch.object(settings, "TASK_LOOKUP_ENABLED", False):
            assert await task_lookup.pre_turn_resolve(SESSION, "改 theme.txt") is None
        assert task_lookup.peek_turn_resolution(SESSION) is None

    asyncio.run(run())


def test_the_first_sentence_is_not_taxed_for_an_injection_nobody_reads() -> None:
    """The pre-turn pass only runs where its result can actually be used.

    Real machine, 2026-08-02: a turn whose gate opened reached its first
    sentence in 1.91s against 0.75-1.14s for every turn whose gate stayed
    shut, because the pick runs before the model is asked to speak. With
    candidate rows off the roster injects nothing anyway, so that second was
    buying only a warmed cache for the report path -- which has its own pause
    to resolve in, and did, correctly, on the very same run.
    """

    called = {"resolved": False}

    async def must_not_resolve(*_args, **_kwargs) -> dict:
        called["resolved"] = True
        return {"row": None, "level": 0, "reason": "no_reference", "candidates": []}

    async def run(candidates: bool):
        with (
            patch.object(settings, "TASK_LOOKUP_ENABLED", True),
            patch.object(settings, "WORK_ROSTER_CANDIDATES", candidates),
            patch.object(task_lookup, "resolve", must_not_resolve),
        ):
            return await task_lookup.pre_turn_resolve(SESSION, "那个颜色的任务怎么样了")

    assert asyncio.run(run(False)) is None
    assert called["resolved"] is False, "the first sentence paid for an unread injection"

    # With rows on, the injection has somewhere to land and the pass runs.
    assert asyncio.run(run(True)) is not None
    assert called["resolved"] is True


def _roster_rows(count: int = 5) -> list[dict]:
    return [
        {
            "work_item_id": f"work_{index}",
            "title": f"任务 {index} 的标题，长度接近截断上限的普通标题",
            "execution": "succeeded",
            "completion": "complete",
            "attention": "none",
            "updated_at": f"2026-08-02T0{index}:00:00Z",
            "files": [f"file_{index}.txt"],
        }
        for index in range(count)
    ]


def _render(rows: list[dict], resolution: dict | None, *, candidates: bool) -> str:
    class Fake:
        @staticmethod
        def conversation_work_items(_session_id: str, *, limit: int = 4) -> list[dict]:
            return rows

    task_lookup.set_turn_resolution(resolution)
    try:
        with (
            patch.object(settings, "WORK_ROSTER_CANDIDATES", candidates),
            patch.object(settings, "TASK_LOOKUP_ENABLED", resolution is not None),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=Fake(),
            ),
        ):
            return render_conversation_work_context(SESSION)
    finally:
        task_lookup.set_turn_resolution(None)


def test_a_resolved_task_is_paid_for_out_of_the_existing_roster_budget() -> None:
    """Rule R3: the roster may change what it carries, never how much.

    Every row here is paid on every turn from here on, in the same budget as
    the character. So a resolved fact displaces recency rows rather than
    joining them.
    """

    rows = _roster_rows()
    resolution = {"session_id": SESSION, "utterance": "问一个旧任务", "row": rows[4]}

    baseline = _render(rows, None, candidates=True)
    injected = _render(rows, resolution, candidates=True)
    assert "refers to this task" in injected
    assert rows[4]["work_item_id"] in injected
    assert len(injected) <= len(baseline), "the roster must not grow"

    # With candidate rows off there is nothing to displace, so the block is
    # left exactly as it is today and the report path carries the answer.
    off_baseline = _render(rows, None, candidates=False)
    off_injected = _render(rows, resolution, candidates=False)
    assert off_injected == off_baseline


def test_nothing_asks_the_model_to_notice_a_task_is_missing() -> None:
    """Rule R3b: suppressing a habit with prose is what keeps failing.

    Asking the model to defer when the task is not listed failed 3 of 9, and
    the failures were confident wrong answers -- worse than a second of
    latency. The judgement is the host's, before the model speaks.
    """

    from llm.prompts import get_system_prompt

    rows = _roster_rows()
    resolution = {"session_id": SESSION, "utterance": "问一个旧任务", "row": rows[4]}
    surfaces = [
        _render(rows, None, candidates=True),
        _render(rows, resolution, candidates=True),
        _render(rows, None, candidates=False),
        get_system_prompt("with_delegate"),
        get_system_prompt("base"),
    ]
    for text in surfaces:
        lowered = text.lower()
        for banned in ("lookup", "not in the list", "not listed", "不在上面", "不在列表"):
            assert banned not in lowered, banned


def test_the_report_path_answers_from_the_ledger_without_delegate_vocabulary() -> None:
    """Rule R1: Host resolves identity; Work Narrator expresses its facts."""

    from server import app as server_app

    row = {
        "work_item_id": "w_target",
        "title": "创建 out/result.txt",
        "files": ["result.txt"],
        "execution": "succeeded",
        "completion": "partial",
        "attention": "conflict",
        "completion_rationale": "三个工具调用均失败，git 无改动",
        "activity_milestones": {
            "diagnostic": {
                "summary": "The AUIP board boot failure is caused by snapshot timing.",
                "observedAt": 2,
            }
        },
    }
    facts = task_lookup.render_task_facts(row)
    assert "partial" in facts and "conflict" in facts and "git 无改动" in facts

    model_calls: list[str] = []
    history: list[str] = []

    async def fake_stream(text: str, **kwargs) -> str:
        model_calls.append(text)
        raise AssertionError("a ledger status answer must not invoke the main model")

    class FakeHistory:
        @staticmethod
        def add_assistant(text: str, **_kwargs) -> None:
            history.append(text)

    narrator = _StatusNarrator()

    async def run() -> str:
        with (
            patch.object(settings, "TASK_LOOKUP_ENABLED", True),
            patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
            patch.object(server_app, "_stream_llm_query_adapter", fake_stream),
            patch.object(
                server_app,
                "_observer_display_language",
                return_value="simplified_chinese",
            ),
            patch.object(server_app, "output_idle_probe", None),
            patch.object(server_app, "work_status_narrator", narrator),
            patch("core.session_manager.get_current_session_id", return_value=SESSION),
            patch("core.session_manager.conversation_history", FakeHistory),
            patch.object(
                task_lookup,
                "resolve",
                new=_stub_resolution({"row": row, "level": 1, "reason": "hit"}),
            ),
        ):
            return await server_app._handle_delegate(
                "确认一下那个导出任务",
                {
                    "intent": "report",
                    "lookup_question": "刚才那个导出任务成了吗？",
                    "lookup_session_id": SESSION,
                },
            )

    result = asyncio.run(run())
    assert result == "[report] answered from the ledger"
    assert model_calls == []
    assert len(narrator.notes) == 1
    assert narrator.notes[0]["metadata"]["status_query"] is True
    assert "snapshot timing" in narrator.notes[0]["summary"]
    assert history and history[0].startswith("[TASK_STATUS]")
    assert narrator.text in history[0]
    assert "snapshot timing" not in history[0]


def test_free_form_status_intent_has_no_host_regex_owner() -> None:
    """Natural words reach DELEGATE/ControlDecision before Ledger execution.

    The production failure used ``game ... state`` to seize an explicit amend
    request and emit an old WorkItem status template. Keep the entire
    recognition API retired rather than growing a mutation exception list.
    """

    from server import context_status

    for module, retired in (
        (
            task_lookup,
            (
                "is_task_status_query",
                "is_current_task_status_query",
                "try_current_task_status_query",
                "try_resolved_task_status_query",
            ),
        ),
        (
            context_status,
            (
                "is_project_status_query",
                "is_work_item_status_query",
                "try_bound_context_status_query",
            ),
        ),
    ):
        assert not any(hasattr(module, name) for name in retired)

    app_source = (Path(__file__).resolve().parents[1] / "server" / "app.py").read_text(
        encoding="utf-8"
    )
    assert "try_bound_context_status_query" not in app_source
    assert "try_current_task_status_query" not in app_source
    assert "try_resolved_task_status_query" not in app_source


def test_canonical_report_identity_bypasses_natural_language_reresolution() -> None:
    from server import app as server_app

    row = {
        "work_item_id": "work_config",
        "title": "config-draft.ini task",
        "files": ["config-draft.ini"],
        "state": "review_ready",
        "execution": "succeeded",
        "completion": "complete",
        "attention": "none",
    }
    bound_calls = []

    class Coordinator:
        def bound_work_item_status_row(self, session_id: str, work_item_id: str):
            bound_calls.append((session_id, work_item_id))
            return dict(row)

        async def enrich_report_row(self, value: dict):
            return dict(value)

    async def voice_sink(_payload: dict) -> dict:
        return {"status": "queued"}

    async def forbidden_resolve(*_args, **_kwargs):
        raise AssertionError("canonical identity was sent back through NLP lookup")

    class FakeHistory:
        @staticmethod
        def add_assistant(_text: str, **_kwargs) -> None:
            return None

    narrator = _StatusNarrator("还在处理这个任务，目前没有发现阻碍。")

    async def run() -> str:
        with (
            patch.object(settings, "TASK_LOOKUP_ENABLED", True),
            patch.object(server_app, "host_readonly_voice_sink", voice_sink),
            patch.object(server_app, "output_idle_probe", None),
            patch.object(
                server_app,
                "_observer_display_language",
                return_value="simplified_chinese",
            ),
            patch.object(server_app, "work_status_narrator", narrator),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=Coordinator(),
            ),
            patch.object(task_lookup, "resolve", new=forbidden_resolve),
            patch("core.session_manager.conversation_history", FakeHistory),
        ):
            return await server_app._handle_delegate(
                "然后告诉我刚才那个任务的状态",
                {
                    "intent": "report",
                    "subject": "work_item",
                    "workspace_ref": "work_config",
                    "lookup_question": "整句里还提到了 route-note.txt",
                    "lookup_session_id": SESSION,
                },
            )

    assert asyncio.run(run()) == "[report] answered from canonical ledger identity"
    assert bound_calls == [(SESSION, "work_config")]


def _stub_resolution(payload: dict):
    async def resolve(*_args, **_kwargs) -> dict:
        return {"row": None, "level": 0, "reason": "no_reference", "candidates": [], **payload}

    return resolve


class _StatusNarrator:
    def __init__(self, text: str = "AUIP 接入正在验证，棋盘启动问题已经定位。") -> None:
        self.text = text
        self.notes: list[dict] = []
        self.deliveries: list[dict] = []
        self.superseded: list[str] = []

    def supersede_for_status_query(self, work_item_id: str) -> int:
        self.superseded.append(work_item_id)
        return 0

    async def compose_status_query_reply(self, note: dict) -> dict:
        self.notes.append(note)
        return {
            "display_text": self.text,
            "main_chat_entry": self.text,
            "display_language": "simplified_chinese",
            "action": "speak",
            "speak": True,
            "append_to_main_chat": True,
        }

    def record_status_query_delivery(
        self,
        _note: dict,
        _decision: dict,
        delivery: dict,
    ) -> None:
        self.deliveries.append(delivery)


def test_terminal_status_prefers_the_bounded_semantic_fact_over_full_provider_prose() -> None:
    row = {
        "work_item_id": "w_research",
        "execution": "succeeded",
        "completion": "partial",
        "attention": "review",
        "activity_phase": "terminal",
        "terminal_summary": (
            "Opening prose. [PROGRESS:VALIDATION] internal marker must not leak. "
            + "very long provider report " * 200
        ),
        "activity_milestones": {
            "validation": {
                "summary": "RFC 2606 and IANA were checked and agree.",
                "observedAt": 20.0,
            }
        },
    }
    display, voice = task_lookup.render_current_status_answer(row)
    # Foreign Provider prose remains evidence on the work surface. The spoken
    # status preserves its semantic milestone without embedding raw English in
    # a Chinese or Japanese sentence.
    assert "终态结果" in display
    assert "RFC 2606 and IANA were checked and agree" not in display
    assert "RFC 2606 and IANA were checked and agree" not in voice
    assert "[PROGRESS:" not in display + voice
    assert "very long provider report" not in display + voice


def test_running_status_next_step_tracks_the_latest_semantic_phase() -> None:
    base = {
        "work_item_id": "w-phased",
        "execution": "running",
        "activity_phase": "working",
        "completion": "unknown",
        "attention": "none",
    }
    no_milestone = task_lookup.current_status_facts(base)
    designed = task_lookup.current_status_facts(
        {
            **base,
            "activity_milestones": {
                "design": {"summary": "Use one AUIP receipt loop.", "observedAt": 1}
            },
        }
    )
    capable = task_lookup.current_status_facts(
        {
            **base,
            "activity_milestones": {
                "capability": {
                    "summary": "Rejected moves can now recover once.",
                    "observedAt": 2,
                }
            },
        }
    )
    assert "实现方案" in no_milestone["next_zh"]
    assert "已确认的方案" in designed["next_zh"]
    assert "验证已经实现的能力" in capable["next_zh"]


def test_running_status_without_a_milestone_names_the_current_task() -> None:
    facts = task_lookup.current_status_facts(
        {
            "work_item_id": "w-intake",
            "title": "把现有应用接入 AUIP",
            "execution": "running",
            "activity_phase": "working",
            "completion": "unknown",
            "attention": "none",
        }
    )
    display, voice = task_lookup.render_current_status_facts(facts)

    assert "把现有应用接入 AUIP" in display
    assert "把现有应用接入 AUIP" in voice
    assert "没有新的可确认成果" in display
    assert "確認できる新しい成果" in voice


def test_running_status_reports_direction_without_promoting_it_to_a_result() -> None:
    row = {
        "work_item_id": "w-direction",
        "execution": "running",
        "activity_phase": "working",
        "completion": "unknown",
        "attention": "none",
        "activity_direction_summary": (
            "Integrating the existing board as progressive enhancement, then "
            "checking connected and standalone play."
        ),
    }
    facts = task_lookup.current_status_facts(row)
    display, voice = task_lookup.render_current_status_facts(facts)

    assert facts["fact_kind"] == "direction"
    assert facts["fact_source"] == "provider_direction"
    assert "当前执行方向" in display
    assert "这还不是完成结果" in display
    assert "完了報告ではない" in voice
    assert "Integrating the existing board" not in display + voice
    assert "没有新的可确认成果" not in display

    terminal = task_lookup.current_status_facts(
        {
            **row,
            "execution": "cancelled",
            "activity_phase": "terminal",
        }
    )
    assert terminal["fact_kind"] == ""
    assert terminal["fact_source"] == "none"


def test_the_answer_waits_for_the_floor_and_keeps_a_text_fallback() -> None:
    """Both halves of the wait, neither of which had ever run.

    The report test stubs the idle probe out entirely, so the polling loop was
    dead code in the suite while being live code on the answering path. It has
    two jobs: hold the answer until the turn that promised it has finished
    playing -- starting a turn clears the sentence queue, so answering early
    cuts the promise off mid-sentence -- and, when the floor never comes, close
    that promise out loud instead of dropping it.
    """

    from server import app as server_app

    row = {
        "work_item_id": "w_target",
        "title": "创建 out/result.txt",
        "files": ["result.txt"],
        "execution": "running",
        "completion": "unknown",
        "attention": "none",
    }

    # Busy for the first few polls, then the floor opens.
    busy_polls = {"left": 3}

    def clears() -> bool:
        if busy_polls["left"] > 0:
            busy_polls["left"] -= 1
            return False
        return True

    spoken: list[dict] = []
    announced: list[dict] = []

    async def voice_sink(payload: dict) -> dict:
        spoken.append(payload)
        return {"status": "queued"}

    async def capture(_title: str, summary: str, *, reason: str, count: int) -> None:
        announced.append({"reason": reason, "summary": summary})

    class FakeHistory:
        @staticmethod
        def add_assistant(_text: str, **_kwargs) -> None:
            return None

    narrator = _StatusNarrator("还在处理这个任务，目前没有发现阻碍。")

    async def run(probe, timeout_s: float) -> str:
        with (
            patch.object(settings, "TASK_LOOKUP_ENABLED", True),
            patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
            patch.object(server_app, "host_readonly_voice_sink", voice_sink),
            patch.object(server_app, "output_idle_probe", probe),
            patch.object(server_app, "work_status_narrator", narrator),
            patch.object(server_app, "_ANSWER_IDLE_TIMEOUT_S", timeout_s),
            patch.object(server_app, "_announce_report_unanswered", capture),
            patch("core.session_manager.conversation_history", FakeHistory),
            patch.object(
                task_lookup,
                "resolve",
                new=_stub_resolution({"row": row, "level": 1, "reason": "hit"}),
            ),
        ):
            return await server_app._handle_delegate(
                "看看那个任务",
                {
                    "intent": "report",
                    "lookup_question": "那个导出好了吗？",
                    "lookup_session_id": SESSION,
                },
            )

    # It waits out the busy polls rather than talking over the promise.
    assert asyncio.run(run(clears, 5.0)) == "[report] answered from the ledger"
    assert busy_polls["left"] == 0, "the answer did not wait for the floor"
    assert spoken and not announced
    assert spoken[0]["complete_turn"] is True
    assert spoken[0]["turn_id"].startswith("host-answer:work_status_narrator:")
    assert narrator.deliveries[-1]["speech_status"] == "queued"

    # When the floor never comes, the verified text is still published; only
    # voice is withheld so it cannot interrupt the active turn.
    spoken.clear()
    result = asyncio.run(run(lambda: False, 1.0))
    assert result == "[report] answered from the ledger"
    assert spoken == [], "an answer was forced into a busy channel"
    assert announced == []


def test_an_unresolved_question_asks_instead_of_answering_about_the_wrong_task() -> None:
    from server import app as server_app

    notes: list[dict] = []

    async def capture(_title: str, summary: str, *, reason: str, count: int) -> None:
        notes.append({"summary": summary, "reason": reason, "count": count})

    candidates = [
        {"work_item_id": "w1", "title": "调研 Rust 的 async 运行时选型", "files": []},
        {"work_item_id": "w2", "title": "调研 SQLite WAL 模式的取舍", "files": []},
    ]

    async def run(payload: dict) -> str:
        with (
            patch.object(settings, "TASK_LOOKUP_ENABLED", True),
            patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
            patch.object(server_app, "_announce_report_unanswered", capture),
            patch.object(task_lookup, "resolve", new=_stub_resolution(payload)),
        ):
            return await server_app._handle_delegate(
                "看看那个调研",
                {
                    "intent": "report",
                    "lookup_question": "那个调研怎么样了？",
                    "lookup_session_id": SESSION,
                },
            )

    ambiguous = asyncio.run(
        run({"reason": "ambiguous", "candidates": candidates, "row": None})
    )
    assert ambiguous == "[report] asked which task"
    assert notes[-1]["count"] == 2
    # The question names tasks the user can recognise, never a work_item_id.
    assert "调研 SQLite WAL 模式的取舍" in notes[-1]["summary"]
    assert "w1" not in notes[-1]["summary"] and "w2" not in notes[-1]["summary"]

    empty = asyncio.run(run({"reason": "empty", "candidates": [], "row": None}))
    assert empty == "[report] no such task"
    assert notes[-1]["reason"] == "lookup_empty"


def test_the_switch_off_keeps_report_refusing_without_answering() -> None:
    from server import app as server_app

    async def run() -> str | None:
        with (
            patch.object(settings, "TASK_LOOKUP_ENABLED", False),
            patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
        ):
            return await server_app._handle_delegate(
                "看看那个任务", {"intent": "report"}
            )

    assert asyncio.run(run()) is None


def test_a_host_answering_turn_can_never_start_work() -> None:
    """The answering pass quotes filenames and verbs; none of it may execute."""

    runtime = ChatRuntime()
    st = _TurnState(gui_callback=None, prompt_variant="base")
    dispatched: list = []

    with patch("core.chat_runtime.record_actions", side_effect=dispatched.append):
        runtime._consume_stream_chunk(
            st,
            'まだよ。[DELEGATE provider="codex" task="theme.txt に一行足す"]',
        )
    assert dispatched == [], "a host answering turn dispatched a delegate"
    assert st.delegate_seen is False

    # Both omission nets stay out of it too: their triggers are exactly the
    # vocabulary a ledger fact is made of.
    async def run() -> bool:
        st_answer = _TurnState(
            gui_callback=None, prompt_variant="base"
        )
        st_answer.full_response = "theme.txt を作成したわよ"
        return await ChatRuntime._repair_missing_delegate(
            st_answer, "theme.txt を作成して", session_id=SESSION
        )

    assert asyncio.run(run()) is False


def test_each_rung_of_the_ladder_is_countable() -> None:
    """Rule T5: how much level 1 solved, and how much level 2 rescued.

    Whether the side channel earns its second of latency is an empirical
    question, so the answer has to be greppable before it can be asked.
    """

    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = Capture()
    logger = logging.getLogger("server.task_lookup")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)

    hit = {"work_item_id": "w_hit", "title": "创建 theme.txt", "files": ["theme.txt"]}

    async def run() -> None:
        with patch.object(settings, "TASK_LOOKUP_ENABLED", True):
            with patch.object(
                task_lookup, "_exact_matches_for_reference", return_value=[hit]
            ):
                first = await task_lookup.resolve(
                    SESSION, "改一下 theme.txt", consumer="test"
                )
            assert first["level"] == 1 and first["reason"] == "hit"

            with patch.object(
                task_lookup, "_exact_matches_for_reference", return_value=[]
            ):
                missing = await task_lookup.resolve(
                    SESSION, "改一下 ghost.txt", consumer="test"
                )
            assert missing["reason"] == "empty"

            candidates = [hit, {"work_item_id": "w_other", "title": "别的", "files": []}]
            with (
                patch.object(task_lookup, "_prefilter_gate", return_value=True),
                patch.object(
                    task_lookup, "_fallback_candidates", return_value=(candidates, True)
                ),
                patch.object(
                    task_lookup, "_side_channel_pick", new=_stub_pick("w_other")
                ),
            ):
                picked = await task_lookup.resolve(
                    SESSION, "那个别的任务怎么样了", consumer="test"
                )
            assert picked["level"] == 2 and picked["row"]["work_item_id"] == "w_other"

            with (
                patch.object(task_lookup, "_prefilter_gate", return_value=True),
                patch.object(
                    task_lookup, "_fallback_candidates", return_value=(candidates, True)
                ),
                patch.object(task_lookup, "_side_channel_pick", new=_stub_pick("")),
            ):
                unsure = await task_lookup.resolve(
                    SESSION, "那个别的任务怎么样了", consumer="test"
                )
            assert unsure["reason"] == "ambiguous" and unsure["candidates"] == candidates

            # A roster that cannot prove it is whole never reaches the model:
            # the pick does not decline when its answer is missing (0/9).
            no_pick = {"called": False}

            async def must_not_run(*_args, **_kwargs) -> str:
                no_pick["called"] = True
                return "w_hit"

            with (
                patch.object(task_lookup, "_prefilter_gate", return_value=True),
                patch.object(
                    task_lookup, "_fallback_candidates", return_value=(candidates, False)
                ),
                patch.object(task_lookup, "_side_channel_pick", new=must_not_run),
            ):
                partial = await task_lookup.resolve(
                    SESSION, "那个别的任务怎么样了", consumer="test"
                )
            assert partial["reason"] == "ambiguous" and partial["row"] is None
            assert no_pick["called"] is False, "a partial roster was offered to the model"

    try:
        asyncio.run(run())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    tagged = [line for line in records if line.startswith("[TASK-LOOKUP]")]
    assert sum("level=1 outcome=hit" in line for line in tagged) == 1
    assert sum("level=1 outcome=empty" in line for line in tagged) == 1
    assert sum("level=2 outcome=pick" in line for line in tagged) == 1
    assert sum("level=2 outcome=unsure" in line for line in tagged) == 1
    assert sum("level=2 outcome=skipped" in line for line in tagged) == 1


def _stub_pick(result: str):
    async def pick(*_args, **_kwargs) -> str:
        return result

    return pick


def test_the_prefilter_favours_recall_over_precision() -> None:
    """The pick decides; the prefilter only has to not lose the right row.

    Carrying a few wrong candidates costs nothing measurable (12/12 with eight
    deliberately similar ones); dropping the right one costs the answer.
    """

    rows = [
        {"work_item_id": "w_theme", "title": "把 theme.txt 的 color 改成 green", "files": ["theme.txt"]},
        {"work_item_id": "w_wal", "title": "调研 SQLite WAL 模式的取舍", "files": ["wal.md"]},
        {"work_item_id": "w_async", "title": "调研 Rust 的 async 运行时选型", "files": ["async.md"]},
    ]
    picked = [row["work_item_id"] for _score, row in task_lookup._prefilter("调研 SQLite WAL 模式那个结论是什么？", rows)]
    assert picked[0] == "w_wal"
    # The other research task is a near miss, and near misses belong in the
    # candidate set rather than being ruled out before the pick.
    assert "w_async" in picked

    assert task_lookup._prefilter("今日の天気はどう？", rows) == []


def test_exact_artifact_lookup_collapses_only_one_continuation_lineage() -> None:
    original = {"work_item_id": "w_original", "related_work_item_id": ""}
    amended = {"work_item_id": "w_amended", "related_work_item_id": "w_original"}
    unrelated = {"work_item_id": "w_other", "related_work_item_id": ""}

    assert task_lookup._collapse_continuation_lineages([original, amended]) == [amended]
    assert task_lookup._collapse_continuation_lineages(
        [original, amended, unrelated]
    ) == [amended, unrelated]

    # Parallel descendants are genuinely ambiguous; neither may win by recency.
    sibling = {"work_item_id": "w_sibling", "related_work_item_id": "w_original"}
    assert task_lookup._collapse_continuation_lineages(
        [original, amended, sibling]
    ) == [amended, sibling]

    # Corrupt lineage data fails closed instead of inventing a leaf.
    cycle_a = {"work_item_id": "w_a", "related_work_item_id": "w_b"}
    cycle_b = {"work_item_id": "w_b", "related_work_item_id": "w_a"}
    assert task_lookup._collapse_continuation_lineages([cycle_a, cycle_b]) == [
        cycle_a,
        cycle_b,
    ]


def test_a_paraphrase_defeats_literal_overlap_so_the_gate_never_shortlists() -> None:
    """Why the prefilter gates rather than shortlists.

    "把颜色改绿那个任务后来怎么样了？" shares no literal token with a task
    titled "把 theme.txt 的 color 改成 green": 颜色/color is a translation gap,
    not a spelling one, and no n-gram tuning reaches it. A shortlist built from
    scores is therefore exactly the set most likely to be missing its own
    answer -- and the pick does not decline when its answer is missing, it
    names the nearest row (0 of 9 declined, 2026-08-02). So the gate only
    decides whether to spend a call, and the pick always sees the whole
    conversation.
    """

    rows = [
        {"work_item_id": "w_theme", "title": "把 theme.txt 的 color 改成 green", "files": []},
        {"work_item_id": "w_notes", "title": "创建 notes.md 记录会议纪要", "files": []},
    ]
    assert task_lookup._prefilter("把颜色改绿那个任务后来怎么样了？", rows) == []

    class Fake:
        @staticmethod
        def conversation_work_item_index(_session_id: str, **_kwargs) -> list[dict]:
            return rows

        @staticmethod
        def conversation_work_items_for_resolution(
            _session_id: str, *, limit: int = 60
        ) -> dict:
            return {"items": rows, "complete": True}

    async def run() -> tuple[dict, dict]:
        with (
            patch.object(settings, "TASK_LOOKUP_ENABLED", True),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=Fake(),
            ),
            patch.object(task_lookup, "_side_channel_pick", new=_stub_pick("w_theme")),
        ):
            pre_turn = await task_lookup.resolve(
                SESSION, "把颜色改绿那个任务后来怎么样了？", consumer="pre_turn"
            )
            report = await task_lookup.resolve(
                SESSION,
                "把颜色改绿那个任务后来怎么样了？",
                consumer="report",
                recency_fallback=True,
            )
        return pre_turn, report

    pre_turn, report = asyncio.run(run())
    # The gate stays shut, so an ordinary-looking turn spends nothing.
    assert pre_turn["reason"] == "no_reference" and pre_turn["row"] is None
    # The report path, already committed to "let me check", skips the gate and
    # reaches the row over a set proven to contain it.
    assert report["row"]["work_item_id"] == "w_theme"


if __name__ == "__main__":
    test_cancel_pending_status_is_not_rendered_as_stopped_or_plain_running()
    print("ok: cancel-pending status remains unconfirmed")
    test_a_task_outside_the_recency_window_is_still_found()
    print("ok: a task outside the recency window is still found")
    test_the_index_does_not_cross_conversations()
    print("ok: the index does not cross conversations")
    test_amend_binds_to_a_target_the_window_lost()
    print("ok: amend binds to a target the window lost")
    test_the_switch_off_leaves_the_resolution_path_untouched()
    print("ok: the switch off leaves the resolution path untouched")
    test_the_first_sentence_is_not_taxed_for_an_injection_nobody_reads()
    print("ok: the first sentence is not taxed for an injection nobody reads")
    test_a_resolved_task_is_paid_for_out_of_the_existing_roster_budget()
    print("ok: a resolved task is paid for out of the existing roster budget")
    test_nothing_asks_the_model_to_notice_a_task_is_missing()
    print("ok: nothing asks the model to notice a task is missing")
    test_the_report_path_answers_from_the_ledger_without_delegate_vocabulary()
    test_free_form_status_intent_has_no_host_regex_owner()
    test_running_status_next_step_tracks_the_latest_semantic_phase()
    print("ok: the report path answers from the ledger without delegate vocabulary")
    test_the_answer_waits_for_the_floor_and_keeps_a_text_fallback()
    print("ok: the answer waits for the floor and says so when it never gets one")
    test_an_unresolved_question_asks_instead_of_answering_about_the_wrong_task()
    print("ok: an unresolved question asks instead of answering about the wrong task")
    test_the_switch_off_keeps_report_refusing_without_answering()
    print("ok: the switch off keeps report refusing without answering")
    test_a_host_answering_turn_can_never_start_work()
    print("ok: a host answering turn can never start work")
    test_each_rung_of_the_ladder_is_countable()
    print("ok: each rung of the ladder is countable")
    test_the_prefilter_favours_recall_over_precision()
    print("ok: the prefilter favours recall over precision")
    test_exact_artifact_lookup_collapses_only_one_continuation_lineage()
    print("ok: exact artifact lookup collapses only one continuation lineage")
    test_a_paraphrase_defeats_literal_overlap_so_the_gate_never_shortlists()
    print("ok: a paraphrase defeats literal overlap so the gate never shortlists")
    print("all task lookup tests passed")
