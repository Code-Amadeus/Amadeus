"""Report knowledge survives short narration without crossing receiving addresses."""
from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_host.provider_runtime import ProviderRuntime
from agent_host.work_ledger_store import WorkLedgerStore
from server.ai_os_schema import work_note_payload
from server.control_ledger import ControlLedgerStore
from server.work_control import WorkControl
from server.work_effect_executor import WorkEffectExecutor
from server.work_ledger_coordinator import WorkLedgerCoordinator
from server.work_observer import ObserverSession, WorkObserverCoordinator
from server import work_observer_llm
from test_cooperative_work_provider_facts import _manager, _role_frame
from test_work_effect_executor import _host


# Synthetic content: the late details model the lost information, not real news.
REPORT = ("# Synthetic research report\n\n" + "Background and methodology. " * 65
    + "\nThe fictional release has 722 manuscripts grouped into 372 families.\n"
    + "Each family needs independent review. " * 65
    + "\nCaveat: Lean checks only the formalized statements, not the informal claims.\n"
    + "Source: https://example.invalid/research/report")


def _note(phase):
    return work_note_payload(source="work_ledger", provider="synthetic",
        run_id="run-report", session_id="session-report", phase=phase,
        title="Research report", summary=REPORT, signals=[],
        metadata={"narration_keypoint": "terminal" if phase == "Result" else "semantic_progress"})


@pytest.mark.parametrize("repair", [False, True])
def test_full_terminal_evidence_reaches_narrator_and_repair_but_speech_stays_short(monkeypatch, repair):
    note = _note("Result")
    assert note["summary"] == REPORT
    observer = WorkObserverCoordinator()
    session = ObserverSession(narration_id="run-report", run_id="run-report",
        session_id="session-report", provider="synthetic", goal="Research")
    progress = dict(_note("Work"), summary="Independent validation is still required.")
    for item in (progress, note):
        session.add_note(item)
        observer._narration_governor.observe(item, output_busy=True)
    merged = observer._merged_narration_note(session)
    assert REPORT in merged["summary"]
    assert progress["summary"] in merged["summary"]
    requests = []
    spoken = "架空の報告は722本を372系列に整理しているけれど、独立した検証はまだ必要よ。"

    def create(**kwargs):
        requests.append(kwargs)
        text = "The task is finished." if repair and len(requests) == 1 else spoken
        content = json.dumps({"action": "final_report", "display_text": text,
            "main_chat_entry": text, "speak": True, "append_to_main_chat": True})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(work_observer_llm, "_client", lambda _: client)
    monkeypatch.setattr(work_observer_llm, "_model", lambda _: "synthetic-model")
    decision = work_observer_llm._decide_sync(provider="deepseek", note=merged,
        notes=session.recent_notes(), recent_chat=[], recent_spoken_updates=[],
        display_language="japanese")
    assert len(requests) == (2 if repair else 1)
    payload = json.loads(requests[0]["messages"][-1]["content"])
    assert REPORT in payload["current_note"]["summary"]
    if repair:
        payload = json.loads(requests[1]["messages"][-1]["content"])
        assert REPORT in payload["result_summary"]
    assert decision["display_text"] == decision["main_chat_entry"] == spoken
    assert decision["speak"] and decision["append_to_main_chat"]
    assert len(decision["display_text"]) < 300


def test_progress_keeps_its_input_budgets():
    note = _note("Work")
    assert len(note["summary"]) <= 520
    assert len(work_observer_llm._compact_note(note)["summary"]) <= 420
    observer = WorkObserverCoordinator()
    session = ObserverSession(narration_id="run-report", run_id="run-report",
        session_id="session-report", provider="synthetic", goal="Research")
    session.add_note(note)
    observer._narration_governor.observe(note, output_busy=True)
    assert len(observer._merged_narration_note(session)["summary"]) <= 420


@pytest.mark.parametrize("status", ["done", "error", "cancelled"])
async def test_followup_frame_reads_full_latest_report_with_host_completion(tmp_path, status):
    async with _host(tmp_path, result_status=status) as host:
        run = host.adapter.run

        async def report_run(*args):
            return replace(await run(*args), result=REPORT)

        host.adapter.run = report_run
        finished = await host.executor.execute(host.effect_id)
        binding = finished["binding"]
        manager = _manager(host)
        frame = await _role_frame(manager, tmp_path)
        current = frame["context"]["current_work"]
        attempt = host.work.get_attempt(binding["attempt_id"])
        assessment = host.work.latest_completion(binding["work_item_id"])
        assert current["work_item_id"] == binding["work_item_id"]
        assert current["result"] == attempt.result == REPORT
        assert current["provider"] == attempt.provider
        assert current["execution_status"] == attempt.execution_status
        assert current["completeness"] == assessment.completeness
        assert current["attention"] == assessment.attention
        if status != "done":
            assert current["completeness"] == "incomplete"
        assert host.adapter.calls == 1  # Reading the report does not re-run Work.
        assert manager.work_for_recipient("foreign-session", "") is None
        assert manager.work_for_recipient("session-c2", "foreign-context") is None
        database = host.database

    # Reopen only persisted stores: no Observer, short spoken history or live run.
    work = WorkLedgerStore(database)
    ledger = ControlLedgerStore(database)
    control = WorkControl(ledger, work)
    coordinator = WorkLedgerCoordinator(work, work_control=control)
    runtime = ProviderRuntime()
    executor = WorkEffectExecutor(control, runtime, coordinator)
    try:
        restored = _manager(SimpleNamespace(control_store=ledger, control=control,
            executor=executor, runtime=runtime))
        replay = await _role_frame(restored, tmp_path)
        assert replay["context"]["current_work"] == current
        assert runtime.list_runs() == []
    finally:
        await runtime.close()
        coordinator.close()
        ledger.close()
        work.close()


@pytest.mark.parametrize("status", ["queued", "running", "orphaned", "failed"])
async def test_new_attempt_does_not_reuse_previous_terminal_report(tmp_path, status):
    async with _host(tmp_path) as host:
        finished = await host.executor.execute(host.effect_id)
        binding = finished["binding"]
        original = host.work.get_attempt(binding["attempt_id"])
        host.work.update_attempt(original.attempt_id, result=REPORT)
        manager = _manager(host)
        assert manager.work_for_recipient("session-c2", "")["result"] == REPORT
        latest = host.work.create_attempt(binding["work_item_id"],
            operation_id=original.operation_id, provider="retry-provider", task="Retry",
            provider_run_id="retry-run", metadata={"session_id": "session-c2"})
        host.work.update_attempt(latest.attempt_id, execution_status=status,
            result="Draft partial text" if status != "failed" else "")
        current = manager.work_for_recipient("session-c2", "")
        assert current["provider"] == "retry-provider"
        assert current["execution_status"] == status
        assert current["completeness"] == "unknown"
        assert "result" not in current


async def test_untracked_provider_narration_gets_the_same_report_as_slice(monkeypatch):
    from server.handlers.work_activity_handler import WorkActivityCoordinator
    from server.protocol import Method

    emit = AsyncMock()
    monkeypatch.setattr("server.handlers.work_activity_handler.bus.emit", emit)
    monkeypatch.setattr("server.handlers.work_activity_handler.add_work_note", lambda _: None)
    state = {"provider": "synthetic", "run_id": "report-run", "task": "Research",
        "session_id": "session-report", "status": "done", "result": REPORT}
    await WorkActivityCoordinator()._emit_result_canvas(state)
    payloads = {call.args[0]: call.args[1] for call in emit.await_args_list}
    assert REPORT in payloads[Method.WALLPAPER_CANVAS]["markdown"]
    assert payloads[Method.CHAT_WORK_NOTE]["summary"] == REPORT


async def test_archived_work_does_not_return_report_context(tmp_path):
    async with _host(tmp_path) as host:
        finished = await host.executor.execute(host.effect_id)
        binding = finished["binding"]
        host.work.update_attempt(binding["attempt_id"], result=REPORT)
        host.work.set_work_item_state(binding["work_item_id"], "archived")
        assert _manager(host).work_for_recipient("session-c2", "") is None
