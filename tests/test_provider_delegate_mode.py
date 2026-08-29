from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.provider_catalog import (
    BROWSER_MANIFEST,
    CODEX_APP_SERVER_MANIFEST,
    OPENCLAW_MANIFEST,
)
from server.app import (
    _delegate_mode_for_provider,
    _delegate_provider_for_task,
    _delegate_provider_selection,
    _task_requests_file_mutation,
)


_ROUTING_MANIFESTS = (
    BROWSER_MANIFEST,
    CODEX_APP_SERVER_MANIFEST,
    OPENCLAW_MANIFEST,
)


def _provider(task: str, attrs: dict) -> str:
    return _delegate_provider_for_task(
        task,
        attrs,
        manifests=_ROUTING_MANIFESTS,
    )


def test_provider_mode_preserves_explicit_actions_without_transport_special_cases() -> None:
    assert _delegate_mode_for_provider("codex", {}, "") == "delegate"
    assert _delegate_mode_for_provider("codex", {"mode": "plan"}, "") == "plan"
    assert _delegate_mode_for_provider("codex", {}, "inspect") == "inspect"


def test_non_codex_delegate_keeps_delegate_mode() -> None:
    assert _delegate_mode_for_provider("openclaw", {}, "") == "delegate"
    assert _delegate_mode_for_provider("browser", {}, "observe") == "observe"


def test_missing_provider_routes_code_generation_to_codex_not_desktop_default() -> None:
    tasks = (
        "Create a Python chess program and save it on the Desktop",
        "Create README.md",
        "Write config.json",
        "Generate foo.py on Desktop",
        "Fix README.md",
        "Update config.json",
        "Save the result on Desktop",
        "创建文件 note.txt",
        "Add a button to app.py",
        "delete foo.py",
        "Develop a chess game",
        "Refactor the project code",
        "开发一个国际象棋程序放桌面",
        "制作国际象棋游戏到桌面",
        "给我做个游戏放桌面",
        "重构项目代码",
        "添加按钮到 app.py",
        "删除 foo.py",
        "Export result.json",
        "Replace config.json",
        "把结果导出到桌面 result.json",
        "把 note.txt 存到桌面",
        "替换 config.json",
        "改一下 README.md",
        "新建 foo.py",
        "Pythonでチェスプログラムを作成してデスクトップに保存する",
    )
    for task in tasks:
        assert _task_requests_file_mutation(task), task
        assert _provider(task, {}) == "codex", task


def test_file_routing_is_explained_by_requirements_and_manifest() -> None:
    requirements, selection = _delegate_provider_selection(
        "Create README.md and write a short project summary.",
        {},
        manifests=_ROUTING_MANIFESTS,
    )
    assert requirements.task_kind == "workspace_mutation"
    assert requirements.workspace_access == "write"
    assert selection.provider_id == "codex"
    assert selection.reason == "preferred_provider_incompatible"
    assert "workspace_access:write" in selection.rejected["openclaw"]


def test_explicit_second_provider_changes_selection_not_task_requirements() -> None:
    requirements, selection = _delegate_provider_selection(
        "Create README.md and write a short project summary.",
        {"provider": "codex"},
        manifests=_ROUTING_MANIFESTS,
    )
    assert requirements.task_kind == "workspace_mutation"
    assert requirements.workspace_access == "write"
    assert requirements.workspace_ownership is None
    assert selection.provider_id == "codex"
    assert set(selection.compatible_candidates) == {"codex", "codex"}


def test_control_decision_workspace_effect_survives_anaphoric_source_text() -> None:
    requirements, selection = _delegate_provider_selection(
        "五子棋ゲームをHTMLファイルとして作成する。",
        {
            "provider": "codex",
            "intent": "execute",
            "_host_source_user_text": "那你去吧",
            "_host_source_user_context": "在桌面写一个简单的五子棋游戏",
            "_host_workspace_access": "write",
        },
        manifests=_ROUTING_MANIFESTS,
    )
    assert requirements.task_kind == "workspace_mutation"
    assert requirements.workspace_access == "write"
    assert selection.provider_id == "codex"


def test_declared_amend_is_a_write_requirement_without_reclassifying_prose() -> None:
    task = "将 direct_host.txt 的内容改为恰好为 phase-two 加一个换行。"
    assert not _task_requests_file_mutation(task)
    requirements, selection = _delegate_provider_selection(
        task,
        {"provider": "codex", "intent": "amend"},
        manifests=_ROUTING_MANIFESTS,
    )
    assert requirements.task_kind == "workspace_mutation"
    assert requirements.workspace_access == "write"
    assert selection.provider_id == "codex"


def test_openclaw_file_work_requires_user_force() -> None:
    task = "Create README.md"
    assert _provider(task, {"provider": "openclaw"}) == "codex"
    assert _provider(
        task,
        {"provider": "openclaw", "fallback": "codex_failed"},
    ) == "codex"
    assert _provider(
        task,
        {"provider": "openclaw", "force_provider": "user"},
    ) == "openclaw"


def test_non_file_provider_routing_is_unchanged() -> None:
    previous = settings.PROVIDER_DELEGATE_DEFAULT_PROVIDER
    try:
        settings.PROVIDER_DELEGATE_DEFAULT_PROVIDER = "openclaw"
        assert _provider("Turn off the desk lamp", {}) == "openclaw"
        assert _provider("Create a user profile", {}) == "openclaw"
        assert _provider(
            "Turn off the desk lamp", {"provider": "openclaw"}
        ) == "openclaw"
        external_tasks = (
            "Remove the app from my Desktop",
            "Update the game on Steam",
            "Create an application account",
            "Add a user to the app",
            "Move the app window",
            "Change the UI setting in the desktop app",
            "Build a house",
            "Build a desktop PC",
        )
        for task in external_tasks:
            assert not _task_requests_file_mutation(task), task
            assert _provider(task, {}) == "openclaw", task
            assert _provider(
                task, {"provider": "openclaw"}
            ) == "openclaw", task
    finally:
        settings.PROVIDER_DELEGATE_DEFAULT_PROVIDER = previous


def test_branchless_browser_search_routes_by_research_capability() -> None:
    requirements, selection = _delegate_provider_selection(
        "Actually check the sources before answering.",
        {
            "provider": "browser",
            "intent": "execute",
            "action": "search",
            "_host_source_user_text": (
                "另外帮我查查 Paxos 那篇经典论文最初是在哪里发表的，"
                "给我一个简短摘要。"
            ),
        },
        manifests=_ROUTING_MANIFESTS,
    )
    assert requirements.task_kind == "research"
    assert requirements.preferred_provider == "browser"
    assert requirements.preference_policy == "prefer"
    assert selection.provider_id == "openclaw"
    assert selection.reason == "preferred_provider_incompatible"
    assert "task_kind:research" in selection.rejected["browser"]

    page_search, page_selection = _delegate_provider_selection(
        "Search this page for the award.",
        {
            "provider": "browser",
            "intent": "execute",
            "action": "search",
            "branch": "continue",
        },
        manifests=_ROUTING_MANIFESTS,
    )
    assert page_search.task_kind == "browser"
    assert page_selection.provider_id == "browser"


def test_model_invented_url_does_not_manufacture_browser_authority() -> None:
    requirements, selection = _delegate_provider_selection(
        "Open the Paxos page at https://ja.wikipedia.org/wiki/Paxos.",
        {
            "provider": "browser",
            "intent": "execute",
            "action": "open",
            "url": "https://ja.wikipedia.org/wiki/Paxos",
            "_host_source_user_text": "打开维基百科找到你自己的页面",
        },
        manifests=_ROUTING_MANIFESTS,
    )
    assert requirements.task_kind == "research"
    assert requirements.preference_policy == "prefer"
    assert selection.provider_id == "openclaw"

    exact_requirements, exact_selection = _delegate_provider_selection(
        "Open the requested URL.",
        {
            "provider": "browser",
            "intent": "execute",
            "action": "open",
            "url": "https://example.test/target",
            "_host_source_user_text": "打开 https://example.test/target",
        },
        manifests=_ROUTING_MANIFESTS,
    )
    assert exact_requirements.task_kind == "browser"
    assert exact_requirements.preference_policy == "require"
    assert exact_selection.provider_id == "browser"

    branch_requirements, branch_selection = _delegate_provider_selection(
        "Open the Paxos page at https://ja.wikipedia.org/wiki/Paxos.",
        {
            "provider": "browser",
            "intent": "execute",
            "action": "open",
            "branch": "continue",
            "url": "https://ja.wikipedia.org/wiki/Paxos",
            "_host_source_user_text": "打开你自己的维基百科页面",
        },
        manifests=_ROUTING_MANIFESTS,
    )
    assert branch_requirements.task_kind == "research"
    assert branch_selection.provider_id == "openclaw"


def test_read_only_language_does_not_look_like_file_mutation() -> None:
    tasks = (
        "Explain the prefix rules in Python",
        "Recommend a Python editor",
        "Review the generated Python file",
        "解释代码更改记录",
        "查看代码修改历史",
        "Python 做什么用？",
    )
    for task in tasks:
        assert not _task_requests_file_mutation(task), task


def test_negative_file_constraints_do_not_mint_write_authority() -> None:
    negative_only = (
        "Do not write any files or change the current Project binding.",
        "Use OpenClaw to inspect the page. Never create a file.",
        "There is no need to edit README.md; just report what you see.",
    )
    for task in negative_only:
        assert not _task_requests_file_mutation(task), task

    non_english_negative = (
        "不要创建或修改任何文件。",
        "ファイルの作成や変更はしない。",
        "ファイルを編集せず、ページだけ確認する。",
    )
    for task in non_english_negative:
        assert not _task_requests_file_mutation(task), task

    positive_after_boundary = (
        "Do not edit files; instead create README.md.",
        "Never write scratch files. Then update config.json.",
        "Create README.md. Do not modify anything else.",
    )
    for task in positive_after_boundary:
        assert _task_requests_file_mutation(task), task

    assert _task_requests_file_mutation(
        "ファイルは編集しない。その代わり README.md を作成する。"
    )

    requirements, selection = _delegate_provider_selection(
        "Inspect the local page with the managed browser.",
        {
            "provider": "openclaw",
            "one_off": True,
            "_host_source_user_text": (
                "Use OpenClaw for a separate web WorkItem. "
                "Do not write any files or change the Project binding."
            ),
        },
        manifests=_ROUTING_MANIFESTS,
    )
    assert requirements.workspace_access == "none"
    assert selection.provider_id == "openclaw"

    # A provider task is a model-authored translation, not an authority source.
    # Even a faulty translation that invents a write must not override the
    # exact user turn once the ControlDecision has named a Provider.
    translated_task = (
        "OpenClawの管理ブラウザでページを開き、結果を report.md に保存する。"
    )
    source = (
        "Use OpenClaw for a separate web WorkItem. "
        "Do not write any files or change the Project binding."
    )
    requirements, selection = _delegate_provider_selection(
        translated_task,
        {
            "provider": "openclaw",
            "one_off": True,
            "_host_source_user_text": source,
        },
        manifests=_ROUTING_MANIFESTS,
    )
    assert requirements.workspace_access == "none"
    assert requirements.preference_policy == "require"
    assert selection.provider_id == "openclaw"

    # The inverse boundary remains intact: an explicit user write request is
    # authoritative even when the model selected a no-workspace Provider.
    requirements, selection = _delegate_provider_selection(
        "Open the page and summarize it.",
        {
            "provider": "openclaw",
            "one_off": True,
            "_host_source_user_text": "Create README.md with the findings.",
        },
        manifests=_ROUTING_MANIFESTS,
    )
    assert requirements.workspace_access == "write"
    assert selection.provider_id == "codex"


def _main() -> None:
    test_provider_mode_preserves_explicit_actions_without_transport_special_cases()
    test_non_codex_delegate_keeps_delegate_mode()
    test_missing_provider_routes_code_generation_to_codex_not_desktop_default()
    test_file_routing_is_explained_by_requirements_and_manifest()
    test_explicit_second_provider_changes_selection_not_task_requirements()
    test_control_decision_workspace_effect_survives_anaphoric_source_text()
    test_declared_amend_is_a_write_requirement_without_reclassifying_prose()
    test_openclaw_file_work_requires_user_force()
    test_non_file_provider_routing_is_unchanged()
    test_branchless_browser_search_routes_by_research_capability()
    test_model_invented_url_does_not_manufacture_browser_authority()
    test_read_only_language_does_not_look_like_file_mutation()
    test_negative_file_constraints_do_not_mint_write_authority()
    print("ok: provider delegate modes are provider-aware")


if __name__ == "__main__":
    _main()
