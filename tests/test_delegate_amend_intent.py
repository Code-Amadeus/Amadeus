"""Letting the model say which turns change existing work.

Which task a follow-up extends is a question about what the user meant. The
host had been inferring it from phrasing, and had the gate backwards: it
grounded pronouns ("that file") and skipped the easier case where the filename
is written out. Measured 2026-08-01, that cost every explicitly named
follow-up: B3 and E1 routed to a new task 10 times out of 10, while the
anaphoric A5 bound 4 times in 5. Turning the roster's candidate rows back on
changed nothing (0/10 either way), which is what ruled the roster out.

Naming the target instead of guessing it is not enough on its own: the model
will not quote a work_item_id (0 of 18, then 0 of 10 more), because that asks
it to transcribe an identifier rather than make a judgement. Declaring `amend`
asks for the judgement it is already making, which is the same reason the
report declaration worked.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config.settings as settings
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_runtime import runtime as provider_runtime
from core.chat_runtime import ChatRuntime, _delegate_declared_amend
from llm.prompts import get_system_prompt
from agent_host.provider_contract import ProviderRequirements
from server.app import _handle_delegate

ROSTER = [
    {"work_item_id": "w_old", "title": "create old.txt and write old"},
    {"work_item_id": "w_cur", "title": "create current.txt and write current"},
]

# What the ledger registered, rather than how the task happened to be worded.
ROSTER_BY_ARTIFACT = [
    {"work_item_id": "w_old", "title": "这是路由协议测试；不要在主对话中直接执行任务…", "files": ["old.txt"]},
    {"work_item_id": "w_cur", "title": "", "files": ["current.txt"]},
]


def _index_matches(roster):
    """Serve the same rows through the index the shipping path queries.

    Both resolution sources are live -- the recency roster when task lookup is
    off, the artifact/title index when it is on -- and they are supposed to
    agree on what a filename means. Feeding one roster to both is what makes
    every case below a statement about the declaration rather than about
    whichever source happened to be wired up.
    """

    from core.chat_runtime import _explicit_file_references

    def matches(_session_id: str, reference: str):
        wanted = {str(reference).lower()}
        return [
            item
            for item in roster
            if wanted
            & (
                {str(name).lower() for name in (item.get("files") or [])}
                | _explicit_file_references(item.get("title") or "")
            )
        ]

    return matches


def _ground(
    task: str,
    question: str,
    roster=ROSTER,
    *,
    amend=True,
    flags=True,
    lookup=None,
    extra_attrs=None,
):
    action = {
        "type": "DELEGATE",
        "attrs": {
            "provider": "codex",
            "task": task,
            "intent": "amend" if amend else "execute",
            **dict(extra_attrs or {}),
        },
    }
    with (
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", flags),
        patch.object(settings, "DELEGATE_AMEND_INTENT", flags),
        patch.object(settings, "TASK_LOOKUP_ENABLED", bool(lookup)),
        patch(
            "core.chat_runtime._load_conversation_resolution_roster",
            return_value=(None, roster, True),
        ),
        patch(
            "server.task_lookup._exact_matches_for_reference",
            side_effect=_index_matches(roster),
        ),
        patch.object(
            provider_runtime,
            "provider_manifests",
            return_value=(CODEX_APP_SERVER_MANIFEST,),
        ),
    ):
        bound = ChatRuntime._ground_present_provider_delegate(
            action, question, session_id="s1"
        )
    return bound, action["attrs"]


def _both_paths(task: str, question: str, roster=ROSTER, **kwargs):
    """Run a case against both resolution sources and require the same answer."""

    off = _ground(task, question, roster, lookup=False, **kwargs)
    on = _ground(task, question, roster, lookup=True, **kwargs)
    assert off[0] == on[0], "the two resolution sources disagreed on whether to bind"
    assert off[1].get("workspace_ref") == on[1].get("workspace_ref")
    return on


def test_an_explicitly_named_follow_up_binds_where_it_used_to_start_a_new_task() -> None:
    # The exact utterance that routed to `new` 5 times out of 5 in B3.
    bound, attrs = _both_paths(
        "在当前工作目录的 old.txt 中添加一行 reviewed",
        "给旧任务加一行 reviewed；这里的旧任务指 old.txt。",
    )
    assert bound is True
    assert attrs["workspace_ref"] == "w_old"

    # The pronoun case the old gate was built for still works.
    bound, attrs = _both_paths(
        "old.txt の色を green に変更する", "把刚才那个文件里的颜色改成 green"
    )
    assert bound is True
    assert attrs["workspace_ref"] == "w_old"


def test_resolution_uses_what_the_task_produced_not_how_it_was_titled() -> None:
    """A title is whatever text created the task; artifacts are a fact.

    2026-08-01, real run: the first delegate was synthesised by the repair net
    from the raw utterance, so the work item was titled with the harness
    preamble. "Append a line to amend.txt" matched nothing and forked a second
    task into its own worktree, while amend.txt was registered as that item's
    business.file the entire time.
    """

    bound, attrs = _both_paths(
        "在当前工作目录的 old.txt 中添加一行 reviewed",
        "给旧任务加一行 reviewed；这里的旧任务指 old.txt。",
        ROSTER_BY_ARTIFACT,
    )
    assert bound is True
    assert attrs["workspace_ref"] == "w_old"

    # Two tasks that produced the same file are still a question, not a guess.
    both = [
        {"work_item_id": "w1", "title": "", "files": ["notes.txt"]},
        {"work_item_id": "w2", "title": "", "files": ["notes.txt", "other.txt"]},
    ]
    bound, attrs = _both_paths("给 notes.txt 加一行", "给 notes.txt 加一行", both)
    assert bound is False
    # Untitled candidates still have to be nameable, or the question renders as
    # ", " and asks nothing.
    assert "notes.txt" in attrs["amend_ambiguous"]
    assert attrs["amend_ambiguous"].strip(", ")
    assert [item["work_item_id"] for item in attrs["_host_amend_candidates"]] == [
        "w1",
        "w2",
    ]


def test_current_project_source_outranks_historical_same_file_deliveries() -> None:
    from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
    from server.reference_catalog import TypedReferenceCandidate
    from server import work_ledger_coordinator

    class ProjectCoordinator:
        @staticmethod
        def resolve_project_source_references(project_id: str, references):
            assert project_id == "project_loop"
            assert set(references) == {"two_player_maze.html"}
            return {
                "status": "resolved",
                "projectId": project_id,
                "workspacePath": "C:/loop",
                "files": ["two_player_maze.html"],
            }

    project = TypedReferenceCandidate(
        "project",
        "project_loop",
        "ETERNAL_LOOP",
        "persistent",
        aliases=("two_player_maze.html",),
    )
    action = {
        "type": "DELEGATE",
        "attrs": {
            "provider": "codex",
            "intent": "execute",
            "project_id": "project_loop",
            "task": "change two_player_maze.html to one point wins",
            CONTROL_REFERENCE_CANDIDATES_ATTR: (project,),
        },
    }
    with (
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
        patch.object(settings, "DELEGATE_AMEND_INTENT", True),
        patch.object(
            work_ledger_coordinator,
            "get_work_ledger_coordinator",
            return_value=ProjectCoordinator(),
        ),
            patch(
                "server.task_lookup._exact_matches_for_reference",
                return_value=[
                {"work_item_id": "work_created", "files": ["two_player_maze.html"]},
                {"work_item_id": "work_changed", "files": ["two_player_maze.html"]},
                ],
            ),
            patch.object(
                provider_runtime,
                "provider_manifests",
                return_value=(CODEX_APP_SERVER_MANIFEST,),
            ),
        ):
        bound = ChatRuntime._ground_present_provider_delegate(
            action,
            "把 two_player_maze.html 改成 1 分获胜",
            session_id="session-new",
        )
    assert bound is True
    assert action["attrs"]["project_id"] == "project_loop"
    assert action["attrs"]["_host_project_source_amend"] is True
    assert action["attrs"]["intent"] == "amend"
    assert action["attrs"]["amend_inferred"] is True
    assert "workspace_ref" not in action["attrs"]
    assert "amend_ambiguous" not in action["attrs"]


def test_complete_multi_file_set_binds_the_one_task_that_owns_all_files() -> None:
    roster = [
        {
            "work_item_id": "real-life",
            "title": "Real Life game",
            "files": ["index.html", "style.css", "script.js"],
        },
        {
            "work_item_id": "other-page",
            "title": "Other page",
            "files": ["index.html"],
        },
    ]
    bound, attrs = _both_paths(
        "Copy existing index.html, style.css and script.js to Desktop.",
        "把已有的三个网页文件复制到桌面",
        roster,
        extra_attrs={"target": "desktop"},
    )
    assert bound is True
    assert attrs["workspace_ref"] == "real-life"
    assert attrs["intent"] == "amend"
    assert "index.html, script.js, style.css" in attrs["task"]


def test_existing_output_copy_is_not_reclassified_from_prose_when_declared_execute() -> None:
    roster = [
        {
            "work_item_id": "real-life",
            "title": "",
            "files": ["index.html", "style.css", "script.js"],
        }
    ]
    model_task = "作成済みの index.html、style.css、script.js をデスクトップ用に配置する"
    question = "不是叫你重做，只是把它放到桌面"
    bound, attrs = _both_paths(
        model_task,
        question,
        roster,
        amend=False,
        extra_attrs={"target": "desktop"},
    )
    assert bound is False
    assert "workspace_ref" not in attrs
    assert attrs["intent"] == "execute"
    assert "amend_inferred" not in attrs


def test_declared_amend_with_no_target_is_blocked_instead_of_becoming_new_work() -> None:
    """A declared edit may not silently create a replacement from scratch."""

    bound, attrs = _both_paths(
        "在当前工作目录创建 brand-new.txt，写入 hello", "创建 brand-new.txt"
    )
    assert bound is False
    assert "workspace_ref" not in attrs
    assert attrs["amend_missing"] == "brand-new.txt"
    assert "amend_ambiguous" not in attrs

    # An execute declaration still creates genuinely new work.  Fail-closed
    # applies to the model's explicit judgement that something already exists.
    bound, attrs = _both_paths(
        "Create brand-new.txt and write hello",
        "Create brand-new.txt and write hello",
        amend=False,
    )
    assert bound is False
    assert attrs["intent"] == "execute"
    assert "amend_missing" not in attrs


def test_several_matches_ask_instead_of_picking_one() -> None:
    """Picking wrong writes into a worktree without the file; asking costs a turn."""

    roster = [
        {"work_item_id": "w1", "title": "create notes.txt"},
        {"work_item_id": "w2", "title": "revise notes.txt again"},
    ]
    bound, attrs = _both_paths("给 notes.txt 加一行", "给 notes.txt 加一行", roster)
    assert bound is False
    assert "workspace_ref" not in attrs
    assert "notes.txt" in attrs["amend_ambiguous"]
    assert len(attrs["_host_amend_candidates"]) == 2


def test_missing_amend_attribute_never_reaches_provider_routing() -> None:
    async def run() -> None:
        announcement = AsyncMock()
        with (
            patch("server.app._announce_amend_missing", announcement),
            patch("server.app._delegate_provider_for_task") as router,
        ):
            result = await _handle_delegate(
                "Add another player",
                {
                    "provider": "codex",
                    "intent": "amend",
                    "amend_missing": "endless_game.html",
                },
            )
        router.assert_not_called()
        announcement.assert_awaited_once_with("endless_game.html")
        assert result == "[amend blocked] tracked target was not found"

    asyncio.run(run())


def test_active_amendment_assembly_starts_the_prepared_replacement_attempt() -> None:
    async def run() -> None:
        replacement = {
            "work_item_id": "w-active",
            "project_id": "project-active",
            "workspace_path": str(Path.cwd()),
            "workspace_mode": "worktree",
            "provider": "codex",
            "mode": "agent",
            "predecessor_attempt_id": "attempt-old",
            "instruction": "replacement instruction with workspace inspection",
            "lineage": {
                "amended_from": "attempt-old",
                "amendments": [
                    {
                        "number": 1,
                        "created_at": "2026-08-06T00:00:00+00:00",
                        "text": "change it to two-player",
                        "amended_from": "attempt-old",
                    }
                ],
            },
            "control": {
                "state": "cancel_pending",
                "revision": 1,
                "predecessor_attempt_id": "attempt-old",
            },
        }
        route_active = AsyncMock(
            return_value={"handled": False, "replacement": replacement}
        )
        start = AsyncMock(
            return_value=SimpleNamespace(
                task_handle=None,
                result="",
                error="",
                metadata={},
            )
        )
        with (
            patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
            patch.object(settings, "DELEGATE_AMEND_INTENT", True),
            patch(
                "server.app._delegate_workspace_route",
                return_value={
                    "status": "resolved",
                    "cwd": str(Path.cwd()),
                    "projectId": "project-active",
                    "workItemId": "w-active",
                    "workspaceMode": "worktree",
                    "source": "intent_workspace_ref",
                },
            ),
            patch(
                "server.app._delegate_provider_selection",
                return_value=(
                    ProviderRequirements(
                        task_kind="workspace_mutation",
                        workspace_access="write",
                        preferred_provider="codex",
                    ),
                    SimpleNamespace(
                        provider_id="codex",
                        to_dict=lambda: {"provider_id": "codex"},
                    ),
                ),
            ),
            patch(
                "agent_host.provider_runtime.runtime.get_manifest",
                return_value=CODEX_APP_SERVER_MANIFEST,
            ),
            patch(
                "server.app._sanitize_delegate_task_for_provider",
                return_value=("change it to two-player", {}),
            ),
            patch("server.app._route_active_amendment", new=route_active),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=object(),
            ),
            patch("agent_host.provider_runtime.runtime.start", new=start),
        ):
            await _handle_delegate(
                "change it to two-player",
                {
                    "provider": "codex",
                    "intent": "amend",
                    "workspace_ref": "w-active",
                    "_host_source_user_text": "你怎么还没改？",
                    "_host_source_user_context": (
                        'User: "把现有小游戏改成双人模式。" | '
                        'Main Chat: "我现在开始修改。"'
                    ),
                },
            )
        route_active.assert_awaited_once()
        assert route_active.await_args.kwargs["source_user_text"] == "你怎么还没改？"
        assert "把现有小游戏改成双人模式" in (
            route_active.await_args.kwargs["source_user_context"]
        )
        request = start.await_args.args[0]
        assert request.provider == "codex" and request.mode == "agent"
        assert request.task == replacement["instruction"]
        assert request.cwd == replacement["workspace_path"]
        assert request.metadata["continuation"] == "steer_replacement"
        assert request.metadata["replaces_attempt_id"] == "attempt-old"
        assert request.metadata["work"]["work_item_id"] == "w-active"
        assert request.metadata["steer_replacement"]["revision"] == 1

    asyncio.run(run())


def test_a_uniquely_matched_existing_file_repairs_execute_to_amend() -> None:
    """The durable ledger records the fact even when the model misses the verb."""

    # Real-provider matrix, 2026-08-04: the model named the right project and
    # Codex changed the right file, but labelled it execute.  Without promoting
    # this unique artifact match, the ledger lost the lineage and the UI showed
    # a semantically unrelated task.
    bound, attrs = _both_paths(
        "在当前工作目录的 old.txt 中添加一行 reviewed",
        "给旧任务加一行 reviewed；这里的旧任务指 old.txt。",
        amend=False,
    )
    assert bound is True
    assert attrs["workspace_ref"] == "w_old"
    assert attrs["intent"] == "amend"
    assert attrs["amend_inferred"] is True

    # Zero matches is still new execute work; correction requires a fact.
    bound, attrs = _both_paths(
        "创建 brand-new.txt",
        "创建 brand-new.txt",
        amend=False,
    )
    assert bound is False
    assert attrs["intent"] == "execute"
    assert "amend_inferred" not in attrs

    # With the vocabulary switched off, the explicit-file correction path is
    # inert and a declaration means nothing.
    bound, attrs = _both_paths(
        "在当前工作目录的 old.txt 中添加一行 reviewed",
        "给旧任务加一行 reviewed；这里的旧任务指 old.txt。",
        amend=False,
        flags=False,
    )
    assert bound is False
    assert attrs["intent"] == "execute"
    with patch.object(settings, "DELEGATE_AMEND_INTENT", False):
        with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True):
            assert _delegate_declared_amend({"intent": "amend"}) is False


def test_a_read_only_existing_artifact_preserves_continuity_without_overriding_focus() -> None:
    bound, attrs = _both_paths(
        "读取 old.txt 并总结内容，不要修改",
        "实际读取并总结 old.txt 的内容，不要修改它。",
        amend=False,
    )
    assert bound is True
    assert attrs["workspace_ref"] == "w_old"
    assert attrs["intent"] == "amend"
    assert attrs["amend_inferred"] is True

    # Focus is the user's persistent-context decision. A ledger match may
    # correct execute continuity, but it cannot reinterpret an explicit focus
    # declaration; only the conversational classifier has that authority.
    bound, attrs = _both_paths(
        "切换后读取 old.txt",
        "切到旧项目后读取 old.txt。",
        amend=False,
        extra_attrs={"intent": "focus", "project_id": "project-old"},
    )
    assert bound is False
    assert attrs["intent"] == "focus"
    assert "workspace_ref" not in attrs

    bound, attrs = _both_paths(
        "切换后给 old.txt 追加一行",
        "切到旧项目后，给刚才那个 old.txt 加一行。",
        amend=False,
        extra_attrs={"intent": "focus", "project_id": "project-old"},
    )
    assert bound is False
    assert attrs["intent"] == "focus"
    assert "workspace_ref" not in attrs

    # A filename match in another explicitly selected Project is not evidence
    # that the selected destination was wrong.
    roster = [
        {
            "work_item_id": "w_old",
            "project_id": "project-old",
            "title": "",
            "files": ["old.txt"],
        }
    ]
    bound, attrs = _both_paths(
        "读取 old.txt",
        "读取 new-project 里的 old.txt。",
        roster,
        amend=False,
        extra_attrs={"project_id": "project-new"},
    )
    assert bound is False
    assert attrs["intent"] == "execute"
    assert "workspace_ref" not in attrs


def test_amend_requires_identifiable_continuity_and_does_not_contradict_report() -> None:
    with (
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
        patch.object(settings, "DELEGATE_AMEND_INTENT", True),
    ):
        prompt = get_system_prompt("with_delegate")
    assert 'intent="amend"' in prompt
    # Two different axes, so both tie-breaks have to survive side by side.
    assert "既存台帳だけで足りるなら report" in prompt or "ledger facts are report" in prompt
    assert (
        "特定の既存 WorkItem" in prompt
        or "identifies a specific prior WorkItem" in prompt
    )
    assert "その連続性を指せない新しい依頼は execute" in prompt or (
        "If no such continuity is identifiable, use execute" in prompt
    )
    assert "迷ったら amend" not in prompt
    assert "continues an existing one, choose amend" not in prompt
    assert "要約・分析・監査・検証" in prompt or "summarizing, analyzing, auditing" in prompt
    assert "プロジェクトやリポジトリ自体" in prompt or "existing project or repository alone" in prompt

    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True):
        with patch.object(settings, "DELEGATE_AMEND_INTENT", False):
            without = get_system_prompt("with_delegate")
    assert 'intent="amend"' not in without
    assert 'intent="report"' in without


def test_implicit_amend_binds_only_the_unique_active_work_item() -> None:
    from server import work_ledger_coordinator

    class FakeCoordinator:
        rows = [
            {
                "work_item_id": "work_active",
                "project_id": "project_game",
                "execution": "running",
            },
            {
                "work_item_id": "work_old",
                "project_id": "project_game",
                "execution": "succeeded",
            },
        ]

        @classmethod
        def conversation_work_items_for_resolution(cls, _session_id: str, *, limit: int):
            assert limit == 200
            return {"items": list(cls.rows), "complete": True}

        @staticmethod
        def workspace_routing_context(*, limit: int):
            assert limit == 200
            return {
                "candidates": [
                    {
                        "projectId": "project_game",
                        "projectName": "ETERNAL_LOOP",
                    },
                    {
                        "projectId": "project_other",
                        "projectName": "other",
                    },
                ]
            }

    action = {
        "type": "DELEGATE",
        "attrs": {"provider": "codex", "intent": "amend", "task": "四点先胜"},
    }
    with patch.object(
        work_ledger_coordinator,
        "get_work_ledger_coordinator",
        return_value=FakeCoordinator(),
    ):
        assert ChatRuntime._ground_unique_active_amendment(
            action,
            "改成4分吧",
            session_id="session-game",
        ) is True
    assert action["attrs"]["workspace_ref"] == "work_active"
    assert action["attrs"]["project_id"] == "project_game"

    FakeCoordinator.rows = [
        *FakeCoordinator.rows,
        {
            "work_item_id": "work_other",
            "project_id": "project_other",
            "execution": "queued",
        },
    ]
    ambiguous = {
        "type": "DELEGATE",
        "attrs": {"provider": "codex", "intent": "amend", "task": "再改一下"},
    }
    with patch.object(
        work_ledger_coordinator,
        "get_work_ledger_coordinator",
        return_value=FakeCoordinator(),
    ):
        assert ChatRuntime._ground_unique_active_amendment(
            ambiguous,
            "再改一下",
            session_id="session-game",
        ) is False
    assert "workspace_ref" not in ambiguous["attrs"]


def test_incremental_amendment_survives_a_contextual_project_guess() -> None:
    from server import work_ledger_coordinator
    from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
    from server.reference_catalog import TypedReferenceCandidate

    project = TypedReferenceCandidate(
        kind="project",
        entity_id="project_game",
        label="ETERNAL_LOOP",
        scope="persistent",
        aliases=("endless game",),
    )

    class FakeCoordinator:
        @staticmethod
        def conversation_work_items_for_resolution(_session_id: str, *, limit: int):
            assert limit == 200
            return {
                "items": [
                    {
                        "work_item_id": "work_active",
                        "project_id": "project_game",
                        "title": "change the maze win condition",
                        "files": ["two_player_maze.html"],
                        "execution": "running",
                        "relation": "running",
                    }
                ],
                "complete": True,
            }

        @staticmethod
        def workspace_routing_context(*, limit: int):
            assert limit == 200
            return {
                "candidates": [
                    {
                        "projectId": "project_game",
                        "projectName": "ETERNAL_LOOP",
                    }
                ]
            }

    action = {
        "type": "DELEGATE",
        "attrs": {
            "provider": "codex",
            "intent": "amend",
            "subject": "project",
            "project_id": "project_game",
            "task": "Change two_player_maze.html to first-to-four wins.",
            CONTROL_REFERENCE_CANDIDATES_ATTR: (project,),
        },
    }
    with patch.object(
        work_ledger_coordinator,
        "get_work_ledger_coordinator",
        return_value=FakeCoordinator(),
    ):
        assert ChatRuntime._ground_unique_active_amendment(
            action,
            "算了改成4次吧",
            session_id="session-game",
        ) is True

    attrs = action["attrs"]
    assert attrs["workspace_ref"] == "work_active"
    assert attrs["subject"] == "work_item"
    assert attrs[CONTROL_REFERENCE_CANDIDATES_ATTR][0].entity_id == "work_active"

    explicit_project = {
        "type": "DELEGATE",
        "attrs": {
            "provider": "codex",
            "intent": "amend",
            "subject": "project",
            "project_id": "project_game",
            CONTROL_REFERENCE_CANDIDATES_ATTR: (project,),
        },
    }
    with patch.object(
        work_ledger_coordinator,
        "get_work_ledger_coordinator",
        return_value=FakeCoordinator(),
    ):
        assert ChatRuntime._ground_unique_active_amendment(
            explicit_project,
            "把 ETERNAL_LOOP 项目改成4次",
            session_id="session-game",
        ) is False
    assert "workspace_ref" not in explicit_project["attrs"]


if __name__ == "__main__":
    test_an_explicitly_named_follow_up_binds_where_it_used_to_start_a_new_task()
    print("ok: an explicitly named follow-up binds where it used to start a new task")
    test_resolution_uses_what_the_task_produced_not_how_it_was_titled()
    print("ok: resolution uses what the task produced, not how it was titled")
    test_current_project_source_outranks_historical_same_file_deliveries()
    print("ok: current Project source outranks historical same-file deliveries")
    test_complete_multi_file_set_binds_the_one_task_that_owns_all_files()
    print("ok: a complete multi-file set binds its one owning task")
    test_existing_output_copy_is_not_reclassified_from_prose_when_declared_execute()
    print("ok: prose does not override a declared execute intent")
    test_declared_amend_with_no_target_is_blocked_instead_of_becoming_new_work()
    print("ok: declared amend with no target is blocked")
    test_several_matches_ask_instead_of_picking_one()
    print("ok: several matches ask instead of picking one")
    test_missing_amend_attribute_never_reaches_provider_routing()
    print("ok: missing amend target never reaches provider routing")
    test_active_amendment_assembly_starts_the_prepared_replacement_attempt()
    print("ok: active amendment assembly starts the prepared replacement attempt")
    test_a_uniquely_matched_existing_file_repairs_execute_to_amend()
    print("ok: a uniquely matched existing file repairs execute to amend")
    test_a_read_only_existing_artifact_preserves_continuity_without_overriding_focus()
    print("ok: read-only artifact continuity never overrides explicit focus")
    test_amend_requires_identifiable_continuity_and_does_not_contradict_report()
    print("ok: amend requires identifiable continuity and does not contradict report")
    test_implicit_amend_binds_only_the_unique_active_work_item()
    print("ok: implicit amend binds only the unique active WorkItem")
    test_incremental_amendment_survives_a_contextual_project_guess()
    print("ok: incremental amend survives a contextual Project guess")
    print("all delegate amend tests passed")
