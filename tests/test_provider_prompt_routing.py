from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.provider_catalog import CODEX_APP_SERVER_MANIFEST
from agent_host.provider_runtime import runtime as provider_runtime
from core.chat_runtime import (
    ChatRuntime,
    _looks_like_anaphoric_work_mutation,
    _looks_like_explicit_work_mutation,
    _requests_explicit_new_work_task,
)
from llm import prompts


def test_delegate_prompts_follow_live_provider_registration() -> None:
    with patch.object(provider_runtime, "list_providers", return_value=[]):
        assert prompts.registered_provider_ids() == (), (
            "prompt construction must not invent providers before registration"
        )

    with (
        patch("tts.pipeline.TTS_OUTPUT_LANGUAGE", "英文"),
        patch(
            "llm.prompts.registered_provider_ids",
            return_value=("browser", "codex", "openclaw"),
        ),
    ):
        codex_only = {
            "runtime_with_delegate": prompts.get_system_prompt("with_delegate"),
            "runtime_bedrock": prompts.get_system_prompt("bedrock"),
        }
    for name, prompt in codex_only.items():
        assert "Currently registered provider ids: browser, codex, openclaw" in prompt, name
        assert 'provider="codex"' in prompt, name
        assert 'provider="locus"' not in prompt, name
        assert "Codex App Server" in prompt, name

    representative = codex_only["runtime_with_delegate"]
    assert "default local code provider" in representative
    assert 'force_provider="user"' in representative
    assert "explicitly chooses one registered provider" in representative
    assert "Never infer force_provider" in representative
    assert "live page state must be retained or manipulated" in representative
    assert "web research, source discovery, comparison, and synthesis" in representative
    assert "Never invent a URL merely to select Browser" in representative
    assert "without that evidence is Agent research" in representative
    assert "continues the export-owning WorkItem" in representative
    assert "not the Session's current Project source" in representative

    variants = codex_only
    for name, prompt in variants.items():

        # The persona used to describe delegation as "you have OpenClaw
        # connected; use it only when an external tool is needed" — a world
        # model that predates Codex, stated first, at length, in the persona's
        # own language. It beat the English routing addon appended after it:
        # first-turn creation emitted the tag 6/6 while an anaphoric follow-up
        # edit emitted 1/6 (2026-07-31 A/B). Delegation must stay a default
        # with one enumerated read-only exception.
        for banned in (
            "外部ツールが必要な時だけ",
            "Only when an external tool is needed",
            "AIアシスタント「OpenClaw」が接続されており",
            "You have an AI assistant called 'OpenClaw' connected",
        ):
            assert banned not in prompt, (name, banned)

        # Examples are imitated more reliably than the rule that governs them,
        # so every worked example must carry a provider attribute.
        for example in re.findall(r"\[DELEGATE[^\]]*\]", prompt):
            assert "provider=" in example, (name, example)

    # The failing code/edit case must appear as a worked example, not only as a rule.
    assert "theme.txt" in codex_only["runtime_with_delegate"]
    assert "[Provider routing]" in settings._DEFAULT_SYSTEM_PROMPT
    assert 'provider="codex"' not in settings._DEFAULT_SYSTEM_PROMPT
    assert 'provider="locus"' not in settings._DEFAULT_SYSTEM_PROMPT
    # The local-LLM CLI prompt is a fifth copy of the same contract.
    assert "外部ツールが必要な時だけ" not in settings._DEFAULT_SYSTEM_PROMPT


def test_provider_routing_wording_tracks_the_output_language() -> None:
    providers = ("browser", "codex", "openclaw")
    with (
        patch("tts.pipeline.TTS_OUTPUT_LANGUAGE", "日文"),
        patch("llm.prompts.registered_provider_ids", return_value=providers),
    ):
        japanese = prompts.get_system_prompt("with_delegate")
    with (
        patch("tts.pipeline.TTS_OUTPUT_LANGUAGE", "英文"),
        patch("llm.prompts.registered_provider_ids", return_value=providers),
    ):
        english = prompts.get_system_prompt("with_delegate")

    assert "現在登録されている provider id" in japanese
    assert "Codex App Server" in japanese
    assert "legacy code provider" not in japanese
    assert "Currently registered provider ids" not in japanese
    assert "Currently registered provider ids" in english
    assert "Codex App Server" in english
    assert "default local code provider" in english
    assert "現在登録されている provider id" not in english
    for prompt in (japanese, english):
        assert 'provider="codex"' in prompt
        assert 'provider="locus"' not in prompt
        assert 'force_provider="user"' in prompt


@patch.object(settings, "WORK_DELEGATE_REPAIR", True)
@patch.object(settings, "DELEGATE_RESEND_ON_OMISSION", False)
@patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", False)
@patch.object(settings, "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE", "off")
@patch("llm.prompts.registered_provider_ids", return_value=("codex",))
@patch.object(
    provider_runtime,
    "provider_manifests",
    new=lambda: (CODEX_APP_SERVER_MANIFEST,),
)
def test_missing_delegate_repair_is_mutation_only_and_unambiguous(
    _registered_providers,
) -> None:
    """Resolution logic, exercised with repair execution explicitly enabled.

    Execution is off by default (see the observe-only test below); this suite
    keeps covering *what* the resolver decides when it is switched on.
    """

    assert _looks_like_explicit_work_mutation(
        "再写一个相邻的小工具 goodbye.py，只打印 goodbye。"
    )
    assert _looks_like_explicit_work_mutation(
        "把刚才那个文件里的颜色改成 green。"
    )
    assert not _looks_like_explicit_work_mutation("刚才那个文件任务进展如何？")
    assert not _looks_like_explicit_work_mutation("如何修改这个文件？")
    assert not _looks_like_explicit_work_mutation(
        "继续刚才创建 two.txt 的任务，只汇报它的状态。"
    )
    assert not _looks_like_anaphoric_work_mutation(
        "继续刚才创建 two.txt 的任务，只汇报它的状态。"
    )
    assert _looks_like_anaphoric_work_mutation("把它的 value 改成 4。")
    assert _looks_like_anaphoric_work_mutation("把刚才那个标题改为 Delta。")

    # Machine output run into the end of a question. Found in the real corpus
    # (sessions/*.json, 2026-08-03): a build log carries filenames and mutation
    # verbs, so both tables read it as a request -- and the net synthesises the
    # user's own sentence as the task, so it would have started work described
    # by a log. Guarded by length, because spoken instructions are short:
    # median 10 characters over 2207 real turns, p99.9 134, and this was the
    # only one past 200. A length is structural; adding markers is not allowed.
    pasted_log = (
        "介绍下你自己Detected CUDA files, patching ldflags Emitting ninja build "
        "file F:\\Baidu\\build\\build.ninja creating build\\lib.win-amd64 copying "
        "modify main.py running install_lib writing manifest file 'src.egg-info' "
        "removing build\\bdist.win-amd64 编译完成 running test 测试通过 "
        "creating file setup.py writing 修改 config.ini running develop"
    )
    assert len(pasted_log) > 200
    assert not _looks_like_explicit_work_mutation(pasted_log)
    assert not _looks_like_anaphoric_work_mutation(pasted_log + " 把它改一下")
    # The longest legitimate request in that corpus is 61 characters, so the
    # guard has to stay far away from real speech.
    assert _looks_like_explicit_work_mutation(
        "你能在桌面上帮我创建一个以伊朗和美国战争为名的 txt 文件，"
        "内容是关于战争情况需要查找的一些重要方面，我根据这些提纲去找信息"
    )
    assert not _looks_like_anaphoric_work_mutation("它的 value 是多少？")
    assert _requests_explicit_new_work_task(
        "新开独立任务创建 d1-b.txt，写入 B。"
    )
    assert _requests_explicit_new_work_task(
        "再新建独立任务，创建 two.txt，写入 two。"
    )
    assert not _requests_explicit_new_work_task(
        "不要新开任务，把 alpha.txt 再加一行。"
    )
    assert not _requests_explicit_new_work_task(
        "不要再新建任务，把 alpha.txt 再加一行。"
    )

    class Coordinator:
        @staticmethod
        def conversation_work_items(_session_id: str, *, limit: int) -> list[dict]:
            assert limit == 8
            return [{"work_item_id": "work_previous"}]

    state = SimpleNamespace(delegate_seen=False, turn_id="turn-legacy-repair")
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=Coordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "把刚才那个文件里的颜色改成 green。",
            session_id="voice-session",
        ))
    assert repaired is True
    assert state.delegate_seen is True
    action = record.call_args.args[0][0]
    assert action["attrs"] == {
        "provider": "codex",
        "workspace_ref": "work_previous",
        "task": "把刚才那个文件里的颜色改成 green。",
        "_host_source_user_text": "把刚才那个文件里的颜色改成 green。",
        "_host_turn_id": "turn-legacy-repair",
    }

    class AmbiguousCoordinator:
        ROWS = [
            {"work_item_id": "work_one", "title": "Create alpha.txt"},
            {"work_item_id": "work_two", "title": "Create beta.txt"},
        ]

        @staticmethod
        def conversation_work_items(_session_id: str, *, limit: int) -> list[dict]:
            assert limit == 8
            return list(AmbiguousCoordinator.ROWS)

        @staticmethod
        def conversation_work_items_by_file(
            _session_id: str,
            name: str,
            *,
            limit: int = 32,
            include_kept_projects: bool = False,
        ) -> list[dict]:
            """Serve the index the amend path queries once task lookup is on.

            The grounding contract is unchanged -- one named file, one matching
            task, bind; anything else refuse -- only the source it asks moved
            from a recency window to the ledger's own index.
            """

            wanted = str(name).lower()
            return [
                row
                for row in AmbiguousCoordinator.ROWS
                if wanted in str(row.get("title") or "").lower()
            ]

    action = {
        "type": "DELEGATE",
        "attrs": {
            "provider": "codex",
            "task": "alpha.txt の内容を green に変更する",
        },
    }
    with patch(
        "server.work_ledger_coordinator.get_work_ledger_coordinator",
        return_value=AmbiguousCoordinator(),
    ):
        grounded = ChatRuntime._ground_present_provider_delegate(
            action,
            "把刚才那个文件里的颜色改成 green。",
            session_id="voice-session",
        )
    assert grounded is True
    assert action["attrs"]["workspace_ref"] == "work_one"
    grounded_task = action["attrs"]["task"]
    assert grounded_task.startswith("目标文件是 alpha.txt。")
    assert "alpha.txt の内容を green に変更する" in grounded_task
    assert "把刚才那个文件里的颜色改成 green。" in grounded_task

    state = SimpleNamespace(delegate_seen=False)
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=AmbiguousCoordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "修改 config.json。",
            session_id="voice-session",
        ))
    assert repaired is False
    record.assert_not_called()

    class SaturatedCoordinator:
        @staticmethod
        def conversation_work_items(_session_id: str, *, limit: int) -> list[dict]:
            assert limit == 8
            return [
                {"work_item_id": f"work_{index}", "title": f"Task {index}"}
                for index in range(8)
            ]

    state = SimpleNamespace(delegate_seen=False)
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=SaturatedCoordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "再新建独立任务，创建 config.json，写入 {}。",
            session_id="voice-session",
        ))
    assert repaired is False
    record.assert_not_called()

    class ExtendedResolutionCoordinator(AmbiguousCoordinator):
        @staticmethod
        def conversation_work_items_for_resolution(
            _session_id: str,
            *,
            limit: int,
        ) -> dict:
            assert limit == 200
            return {
                "items": [
                    {
                        "work_item_id": f"work_{index}",
                        "title": f"Create historical-{index}.txt",
                    }
                    for index in range(8)
                ] + [
                    {
                        "work_item_id": "work_target",
                        "title": "Create d1-e.txt",
                    }
                ],
                "complete": True,
            }

    state = SimpleNamespace(delegate_seen=False)
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=ExtendedResolutionCoordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "把 d1-e.txt 再加一行 END。",
            session_id="voice-session",
        ))
    assert repaired is True
    assert (
        record.call_args.args[0][0]["attrs"]["workspace_ref"]
        == "work_target"
    )

    class IncompleteResolutionCoordinator(ExtendedResolutionCoordinator):
        @staticmethod
        def conversation_work_items_for_resolution(
            _session_id: str,
            *,
            limit: int,
        ) -> dict:
            result = ExtendedResolutionCoordinator.conversation_work_items_for_resolution(
                _session_id,
                limit=limit,
            )
            result["complete"] = False
            return result

    state = SimpleNamespace(delegate_seen=False)
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=IncompleteResolutionCoordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "把 d1-e.txt 再加一行 END。",
            session_id="voice-session",
        ))
    assert repaired is False
    record.assert_not_called()

    state = SimpleNamespace(delegate_seen=False)
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=AmbiguousCoordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "给旧任务加一行 reviewed；这里的旧任务指 alpha.txt。",
            session_id="voice-session",
        ))
    assert repaired is True
    action = record.call_args.args[0][0]
    assert action["attrs"]["workspace_ref"] == "work_one"

    state = SimpleNamespace(
        delegate_seen=False,
        full_response="わかった、alpha.txt の value を 4 に変更するわ。",
    )
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=AmbiguousCoordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "把它的 value 改成 4。",
            session_id="voice-session",
        ))
    assert repaired is True
    assert record.call_args.args[0][0]["attrs"] == {
        "provider": "codex",
        "workspace_ref": "work_one",
        "task": "目标文件是 alpha.txt。把它的 value 改成 4。",
        "_host_source_user_text": "把它的 value 改成 4。",
    }

    state = SimpleNamespace(
        delegate_seen=False,
        full_response="alpha.txt と beta.txt のどちらかを変更するわ。",
    )
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=AmbiguousCoordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "把它的 value 改成 4。",
            session_id="voice-session",
        ))
    assert repaired is False
    record.assert_not_called()

    state = SimpleNamespace(
        delegate_seen=False,
        full_response="config.json の value を 4 に変更するわ。",
    )
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=AmbiguousCoordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "把它的 value 改成 4。",
            session_id="voice-session",
        ))
    assert repaired is False
    record.assert_not_called()

    class ProjectStore:
        @staticmethod
        def get_work_item(work_item_id: str) -> SimpleNamespace:
            assert work_item_id in {"work_one", "work_two"}
            return SimpleNamespace(project_id="project_same")

        @staticmethod
        def get_project(project_id: str) -> SimpleNamespace:
            assert project_id == "project_same"
            return SimpleNamespace(canonical_path="C:/scratch/project")

    project_coordinator = AmbiguousCoordinator()
    project_coordinator.store = ProjectStore()
    state = SimpleNamespace(delegate_seen=False)
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=project_coordinator,
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "再新建独立任务，创建 config.json，写入 {}。",
            session_id="voice-session",
        ))
    assert repaired is True
    assert record.call_args.args[0][0]["attrs"] == {
        "provider": "codex",
        "project_id": "project_same",
        "cwd": "C:/scratch/project",
        "task": "再新建独立任务，创建 config.json，写入 {}。",
        "_host_source_user_text": "再新建独立任务，创建 config.json，写入 {}。",
    }

    class SplitProjectStore(ProjectStore):
        @staticmethod
        def get_work_item(work_item_id: str) -> SimpleNamespace:
            return SimpleNamespace(project_id=f"project_{work_item_id}")

    split_coordinator = AmbiguousCoordinator()
    split_coordinator.store = SplitProjectStore()
    state = SimpleNamespace(delegate_seen=False)
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=split_coordinator,
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "新开独立任务创建 config.json，写入 {}。",
            session_id="voice-session",
        ))
    assert repaired is False
    record.assert_not_called()

    state = SimpleNamespace(delegate_seen=False)
    with patch("core.chat_runtime.record_actions") as record:
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "刚才那个文件任务进展如何？",
            session_id="voice-session",
        ))
    assert repaired is False
    record.assert_not_called()

    state = SimpleNamespace(
        delegate_seen=False,
        full_response='[DELEGATE provider="codex" task="创建 hello.py"]',
    )
    with patch("core.chat_runtime.record_actions") as record:
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "创建 hello.py。",
            session_id="voice-session",
        ))
    assert repaired is False
    record.assert_not_called()

    state = SimpleNamespace(delegate_seen=True)
    with patch("core.chat_runtime.record_actions") as record:
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "创建 hello.py。",
            session_id="voice-session",
        ))
    assert repaired is False
    record.assert_not_called()


@patch.object(settings, "WORK_DELEGATE_REPAIR", False)
@patch.object(settings, "DELEGATE_RESEND_ON_OMISSION", False)
@patch("llm.prompts.registered_provider_ids", return_value=("codex",))
@patch.object(
    provider_runtime,
    "provider_manifests",
    new=lambda: (CODEX_APP_SERVER_MANIFEST,),
)
def test_delegate_repair_can_be_switched_to_observe_only(
    _registered_providers,
) -> None:
    """Observe mode must resolve and log, but never fabricate a mutation run.

    This is the A/B lever for measuring the raw tag-omission rate; the default
    is on because the net demonstrably carries real load (see settings).
    """

    class Coordinator:
        @staticmethod
        def conversation_work_items(_session_id: str, *, limit: int) -> list[dict]:
            return [{"work_item_id": "work_previous"}]

    state = SimpleNamespace(delegate_seen=False, full_response="马上就去改。")
    with (
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=Coordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(ChatRuntime._repair_missing_delegate(
            state,
            "把刚才那个文件里的颜色改成 green。",
            session_id="voice-session",
        ))

    # Same input that the enabled path repairs — here it must stay inert.
    assert repaired is False
    record.assert_not_called()
    assert state.delegate_seen is False, "observe mode must not claim a delegate"


@patch.object(settings, "WORK_DELEGATE_REPAIR", True)
@patch.object(settings, "DELEGATE_RESEND_ON_OMISSION", False)
@patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", False)
@patch.object(settings, "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE", "off")
@patch("llm.prompts.registered_provider_ids", return_value=("codex",))
@patch.object(
    provider_runtime,
    "provider_manifests",
    new=lambda: (CODEX_APP_SERVER_MANIFEST,),
)
def test_first_turn_omission_is_repairable_when_the_project_is_unambiguous(
    _registered_providers,
) -> None:
    """The first turn had no net, and its omission cascades.

    Real run, 2026-07-31: the model dropped the tag on "create theme.txt".
    With an empty roster the resolver had nothing to bind to, so no WorkItem
    was created — and the follow-up then had nothing to bind to either, so
    both steps failed. Nothing exists to continue on a first turn, so the
    instruction is unambiguously new work; a single allowlisted project makes
    the target unambiguous too. Two or more must still refuse.
    """

    import tempfile

    class EmptyRoster:
        @staticmethod
        def conversation_work_items(_session_id: str, *, limit: int) -> list[dict]:
            return []

    with tempfile.TemporaryDirectory() as only_project, tempfile.TemporaryDirectory() as other:
        def repair(question: str, allowlist: str):
            state = SimpleNamespace(delegate_seen=False, full_response="了解、すぐ作るわ。")
            with (
                patch.object(settings, "WORK_PROJECT_ALLOWLIST", allowlist),
                patch(
                    "server.work_ledger_coordinator.get_work_ledger_coordinator",
                    return_value=EmptyRoster(),
                ),
                patch("core.chat_runtime.record_actions") as record,
            ):
                done = asyncio.run(ChatRuntime._repair_missing_delegate(
                    state, question, session_id="voice-session"
                ))
            return done, record

        done, record = repair("请在 scratch 仓创建 theme.txt，写入 color=blue。", only_project)
        assert done is True
        attrs = record.call_args.args[0][0]["attrs"]
        assert attrs["provider"] == "codex"
        assert attrs["cwd"] == str(Path(only_project).resolve())
        assert "workspace_ref" not in attrs, "a first turn has nothing to continue"

        # Two candidate projects: the target is genuinely ambiguous, so refuse.
        done, record = repair(
            "请在 scratch 仓创建 theme.txt，写入 color=blue。",
            f"{only_project};{other}",
        )
        assert done is False
        record.assert_not_called()

        # An anaphoric request on a first turn has no antecedent to resolve.
        done, record = repair("把刚才那个文件改成 green。", only_project)
        assert done is False
        record.assert_not_called()


@patch.object(settings, "WORK_DELEGATE_REPAIR", True)
@patch.object(settings, "DELEGATE_RESEND_ON_OMISSION", False)
@patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", False)
@patch.object(settings, "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE", "off")
def test_missing_delegate_repair_uses_the_only_registered_code_provider() -> None:
    class Coordinator:
        @staticmethod
        def conversation_work_items(_session_id: str, *, limit: int) -> list[dict]:
            assert limit == 8
            return [{"work_item_id": "work_previous"}]

    state = SimpleNamespace(delegate_seen=False, full_response="すぐ直すわね。")
    with (
        patch(
            "llm.prompts.registered_provider_ids",
            return_value=("browser", "codex", "openclaw"),
        ),
        patch.object(
            provider_runtime,
            "provider_manifests",
            return_value=(CODEX_APP_SERVER_MANIFEST,),
        ),
        patch(
            "server.work_ledger_coordinator.get_work_ledger_coordinator",
            return_value=Coordinator(),
        ),
        patch("core.chat_runtime.record_actions") as record,
    ):
        repaired = asyncio.run(
            ChatRuntime._repair_missing_delegate(
                state,
                "把刚才那个文件里的颜色改成 green。",
                session_id="codex-only-session",
            )
        )

    assert repaired is True
    assert record.call_args.args[0][0]["attrs"]["provider"] == "codex"


@patch.object(settings, "WORK_DELEGATE_REPAIR", True)
@patch.object(settings, "DELEGATE_RESEND_ON_OMISSION", False)
@patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", False)
@patch.object(settings, "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE", "off")
def test_taskless_focus_does_not_hide_work_promised_in_the_same_turn() -> None:
    class EmptyRoster:
        @staticmethod
        def conversation_work_items(_session_id: str, *, limit: int) -> list[dict]:
            assert limit == 8
            return []

    async def scenario() -> tuple[bool, object]:
        focus_finished = False

        async def finish_focus() -> None:
            nonlocal focus_finished
            await asyncio.sleep(0)
            focus_finished = True

        state = SimpleNamespace(
            delegate_seen=True,
            work_delegate_seen=False,
            focus_delegate_attrs={
                "provider": "codex",
                "project_id": "project_direct",
            },
            focus_delegate_batches=[finish_focus()],
            full_response="了解。切到 direct-host，然后交给 codex 干这活儿。",
        )

        def record_after_focus(actions):
            assert focus_finished is True
            return None

        with (
            patch(
                "llm.prompts.registered_provider_ids",
                return_value=("browser", "codex", "openclaw"),
            ),
            patch.object(
                provider_runtime,
                "provider_manifests",
                return_value=(CODEX_APP_SERVER_MANIFEST,),
            ),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=EmptyRoster(),
            ),
            patch(
                "core.chat_runtime.record_actions",
                side_effect=record_after_focus,
            ) as record,
        ):
            repaired = await ChatRuntime._repair_missing_delegate(
                state,
                "切换到 direct-host，并创建 direct_host.txt，写入 phase-one。",
                session_id="focus-plus-work",
            )
        return repaired, record

    repaired, record = asyncio.run(scenario())
    assert repaired is True
    attrs = record.call_args.args[0][0]["attrs"]
    assert attrs["provider"] == "codex"
    assert attrs["project_id"] == "project_direct"
    assert "cwd" not in attrs


def _main() -> None:
    test_delegate_prompts_follow_live_provider_registration()
    test_provider_routing_wording_tracks_the_output_language()
    test_missing_delegate_repair_is_mutation_only_and_unambiguous()
    test_delegate_repair_can_be_switched_to_observe_only()
    test_first_turn_omission_is_repairable_when_the_project_is_unambiguous()
    test_missing_delegate_repair_uses_the_only_registered_code_provider()
    test_taskless_focus_does_not_hide_work_promised_in_the_same_turn()
    print("ok: delegate prompts follow live Provider registration")
    print("ok: host-side delegate repair can be switched to observe-only")
    print("ok: a first-turn omission is repairable when the project is unambiguous")
    print("ok: omission repair follows the only registered code provider")
    print("ok: taskless focus cannot hide work promised in the same turn")


if __name__ == "__main__":
    _main()
