"""Project reports share report intent while keeping Provider execution closed."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as settings
from agent_host.work_ledger_store import WorkLedgerStore
from server.app import _handle_delegate
from server.project_report import answer_project_report, normalize_report_subject
from server.work_ledger_coordinator import WorkLedgerCoordinator


def _fixture(root: Path) -> tuple[WorkLedgerStore, WorkLedgerCoordinator, str, str]:
    store = WorkLedgerStore(root / "ledger.sqlite3")
    first_root = root / "eternal-loop"
    second_root = root / "amadeus"
    first_root.mkdir()
    second_root.mkdir()
    first = store.create_or_get_project(first_root, name="Eternal Loop")
    accepted = store.create_work_item(
        first.project_id,
        title="Improve monster rendering",
        goal="Make the game clearer.",
    )
    store.set_work_item_state(accepted.work_item_id, "accepted")
    second = store.create_or_get_project(second_root, name="Amadeus")
    running = store.create_work_item(
        second.project_id,
        title="Repair project report routing",
        goal="Read project truth from the ledger.",
    )
    attempt = store.create_attempt(
        running.work_item_id,
        provider="codex",
        task="repair routing",
        metadata={"session_id": "origin-chat"},
    )
    store.update_attempt(attempt.attempt_id, execution_status="running")
    coordinator = WorkLedgerCoordinator(store)
    coordinator.configure()
    return store, coordinator, first.project_id, second.project_id


def test_subject_contract_is_small_and_backward_compatible() -> None:
    assert normalize_report_subject(None) == "work_item"
    assert normalize_report_subject("") == "work_item"
    assert normalize_report_subject("work-item") == "work_item"
    assert normalize_report_subject("project") == "project"
    assert normalize_report_subject("projects") == "project"
    assert normalize_report_subject("workspace") is None


def test_project_report_reads_specific_and_recent_ledger_truth() -> None:
    with tempfile.TemporaryDirectory(prefix="project_report_subject_") as temp:
        store, coordinator, accepted_project_id, _running_project_id = _fixture(Path(temp))
        try:
            with patch(
                "server.work_ledger_coordinator.cwd_in_project_registry",
                return_value=True,
            ):
                recent = answer_project_report(coordinator)
            assert recent.status == "answered"
            assert "Eternal Loop" in recent.display_text
            assert "Amadeus" in recent.display_text
            assert "已验收" in recent.display_text
            assert "执行中" in recent.display_text

            specific = answer_project_report(
                coordinator,
                project_id=accepted_project_id,
            )
            assert specific.status == "answered"
            assert specific.project_id == accepted_project_id
            assert "Eternal Loop" in specific.display_text
            assert "已验收" in specific.display_text
            assert "Amadeus" not in specific.display_text

            missing = answer_project_report(coordinator, project_id="project_missing")
            assert missing.status == "not_found"
            assert "找不到" in missing.display_text
        finally:
            coordinator.close()


def test_project_report_never_routes_or_creates_work() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="project_report_dispatch_") as temp:
            store, coordinator, _first_project_id, second_project_id = _fixture(Path(temp))
            before = [item.work_item_id for item in store.list_work_items(limit=200)]
            spoken: list[dict[str, str]] = []

            async def capture(answer: str, **kwargs: str) -> bool:
                spoken.append({"answer": answer, **kwargs})
                return True

            try:
                with (
                    patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
                    patch.object(settings, "TASK_LOOKUP_ENABLED", True),
                    patch("server.app._delegate_provider_for_task") as router,
                    patch("server.app._speak_task_lookup_answer", side_effect=capture),
                    patch("server.app._observer_display_language", return_value="simplified_chinese"),
                ):
                    result = await _handle_delegate(
                        "这个项目怎么样？",
                        {
                            "provider": "locus",
                            "intent": "report",
                            "subject": "project",
                            "project_id": second_project_id,
                        },
                    )
                    invalid = await _handle_delegate(
                        "查一下状态",
                        {
                            "provider": "codex",
                            "intent": "report",
                            "subject": "workspace",
                        },
                    )
                    router.assert_not_called()
                assert result == "[report] answered project from the ledger", result
                assert invalid == "[report] invalid subject", invalid
                assert spoken[0]["history_marker"] == "PROJECT_STATUS"
                assert spoken[0]["source"] == "project_ledger_status"
                assert "Amadeus" in spoken[0]["answer"]
                assert spoken[1]["history_marker"] == "LEDGER_STATUS"
                assert [item.work_item_id for item in store.list_work_items(limit=200)] == before
            finally:
                coordinator.close()

    asyncio.run(run())


if __name__ == "__main__":
    test_subject_contract_is_small_and_backward_compatible()
    print("ok: report subject remains small and backward compatible")
    test_project_report_reads_specific_and_recent_ledger_truth()
    print("ok: project reports read specific and recent ledger truth")
    test_project_report_never_routes_or_creates_work()
    print("ok: project reports never route or create work")
    print("all project report subject tests passed")
