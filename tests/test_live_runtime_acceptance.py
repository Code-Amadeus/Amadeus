"""Deterministic contracts for the attach-only live runtime driver."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.e2e_real_work_conversation import EventRecord
from tools.live_runtime_acceptance import (
    PRESENTATION_ACTIVITY_METHODS,
    _is_terminal_observer_decision_for_run,
    _parser,
    _validate_args,
    active_canary_block_reason,
    evaluate_turn,
    write_report,
)


def test_output_idle_ignores_unrelated_runtime_heartbeats() -> None:
    assert "vad.energy" not in PRESENTATION_ACTIVITY_METHODS
    assert "wallpaper.canvas" not in PRESENTATION_ACTIVITY_METHODS
    assert "provider.event" not in PRESENTATION_ACTIVITY_METHODS
    assert "tts.sentence_start" in PRESENTATION_ACTIVITY_METHODS
    assert "chat.work_note" in PRESENTATION_ACTIVITY_METHODS


def _work(*ids: str) -> dict:
    return {"work": {"items": [{"id": value} for value in ids]}}


def _created(provider: str = "locus", work_item_id: str = "work_a") -> EventRecord:
    return EventRecord(
        elapsed_s=1.0,
        method="provider.event",
        params={
            "provider": provider,
            "run_id": "run_a",
            "type": "run.created",
            "work": {"work_item_id": work_item_id},
        },
    )


def _result(status: str = "done", provider: str = "locus") -> EventRecord:
    return EventRecord(
        elapsed_s=2.0,
        method="provider.result",
        params={"provider": provider, "run_id": "run_a", "status": status},
    )


def test_default_mode_is_strictly_passive() -> None:
    args = _parser().parse_args([])
    assert args.mode == "observe"
    assert args.say == ""
    _validate_args(args)

    active_without_text = _parser().parse_args(["--mode", "turn"])
    try:
        _validate_args(active_without_text)
    except ValueError as exc:
        assert "requires --say" in str(exc)
    else:
        raise AssertionError("active mode accepted an empty turn")

    negative_settle = _parser().parse_args(["--settle-timeout", "-1"])
    try:
        _validate_args(negative_settle)
    except ValueError as exc:
        assert "settle-timeout" in str(exc)
    else:
        raise AssertionError("negative terminal settle timeout was accepted")

    passive_barge_in = _parser().parse_args(["--barge-in"])
    try:
        _validate_args(passive_barge_in)
    except ValueError as exc:
        assert "requires --mode turn" in str(exc)
    else:
        raise AssertionError("passive observation accepted a barge-in action")

    active_barge_in = _parser().parse_args(
        ["--mode", "turn", "--say", "continue", "--barge-in"]
    )
    _validate_args(active_barge_in)


def test_terminal_delivery_is_run_scoped() -> None:
    receipt = EventRecord(
        elapsed_s=1.0,
        method="chat.observer_decision",
        params={
            "run_id": "run_a",
            "action": "final_report",
            "terminal": True,
        },
    )
    assert _is_terminal_observer_decision_for_run(receipt, "run_a")
    assert not _is_terminal_observer_decision_for_run(receipt, "run_b")
    assert not _is_terminal_observer_decision_for_run(_created(), "run_a")
    nonterminal = EventRecord(
        elapsed_s=1.0,
        method="chat.observer_decision",
        params={"run_id": "run_a", "action": "speak", "terminal": False},
    )
    assert not _is_terminal_observer_decision_for_run(nonterminal, "run_a")


def test_active_canary_never_overlaps_live_product_activity_by_default() -> None:
    assert active_canary_block_reason(
        {"chat": {"busy": True}, "provider": {"active_runs": []}},
        allow_active_work=False,
    ) == "chat_busy"
    assert active_canary_block_reason(
        {"chat": {"busy": True}, "provider": {"active_runs": [{"id": "run"}]}},
        allow_active_work=True,
        allow_chat_busy=True,
    ) == ""
    assert active_canary_block_reason(
        {"chat": {"busy": False}, "provider": {"active_runs": [{"id": "run"}]}},
        allow_active_work=False,
    ) == "provider_work_active"
    assert active_canary_block_reason(
        {"chat": {"busy": False}, "provider": {"active_runs": [{"id": "run"}]}},
        allow_active_work=True,
    ) == ""


def test_no_work_requires_both_no_run_and_no_new_work_item() -> None:
    passed = evaluate_turn(
        expect="no-work",
        events=[],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert passed["status"] == "passed"

    failed = evaluate_turn(
        expect="no-work",
        events=[_created()],
        before_work=_work("work_old"),
        after_work=_work("work_old", "work_a"),
        chat_completed=True,
    )
    assert failed["status"] == "failed"
    assert len(failed["failures"]) == 2


def test_auip_expectations_require_verified_host_transitions_without_work() -> None:
    launch_requested = EventRecord(
        elapsed_s=1.0,
        method="auip.launch.requested",
        params={"request_id": "launch_a", "artifact_id": "artifact_a"},
    )
    active = EventRecord(
        elapsed_s=1.2,
        method="auip.updated",
        params={"status": "active", "engagement_mode": "observe"},
    )
    launched = evaluate_turn(
        expect="auip-launch",
        events=[launch_requested, active],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert launched["status"] == "passed"

    launch_failed = EventRecord(
        elapsed_s=1.1,
        method="chat.work_note",
        params={"metadata": {"auip_launch_failed": True}},
    )
    rejected = evaluate_turn(
        expect="auip-launch",
        events=[launch_requested, launch_failed],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert rejected["status"] == "failed"
    assert len(rejected["failures"]) == 2

    collaborated = evaluate_turn(
        expect="auip-mode:collaborate",
        events=[
            EventRecord(
                elapsed_s=1.0,
                method="auip.updated",
                params={"status": "active", "engagement_mode": "collaborate"},
            )
        ],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert collaborated["status"] == "passed"

    relaunched = evaluate_turn(
        expect="auip-mode:collaborate",
        events=[launch_requested, active],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert relaunched["status"] == "failed"
    assert any("relaunched" in item for item in relaunched["failures"])

    left = evaluate_turn(
        expect="auip-leave",
        events=[
            EventRecord(
                elapsed_s=1.0,
                method="auip.updated",
                params={"status": "closed", "external_process_stopped": False},
            )
        ],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert left["status"] == "passed"

    owned_pending = evaluate_turn(
        expect="auip-leave",
        events=[
            EventRecord(
                elapsed_s=1.0,
                method="auip.updated",
                params={
                    "status": "closed",
                    "host_surface_id": "surface-a",
                    "surface_close_status": "pending",
                },
            )
        ],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert owned_pending["status"] == "failed"

    owned_closed = evaluate_turn(
        expect="auip-leave",
        events=[
            EventRecord(
                elapsed_s=1.0,
                method="auip.updated",
                params={
                    "status": "closed",
                    "host_surface_id": "surface-a",
                    "surface_close_status": "pending",
                },
            ),
            EventRecord(
                elapsed_s=1.1,
                method="auip.surface.close.requested",
                params={
                    "app_session_id": "app-a",
                    "host_surface_id": "surface-a",
                },
            ),
            EventRecord(
                elapsed_s=1.2,
                method="auip.updated",
                params={
                    "status": "closed",
                    "host_surface_id": "surface-a",
                    "surface_close_status": "closed",
                    "host_surface_closed": True,
                },
            ),
        ],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert owned_closed["status"] == "passed"


def test_auip_preparation_reuses_work_then_requires_verified_launch() -> None:
    prepared_created = _created("locus", "work_old")
    prepared_created.params["metadata"] = {"source": "auip_prepare"}
    launch_requested = EventRecord(
        elapsed_s=3.0,
        method="auip.launch.requested",
        params={"request_id": "launch_a", "artifact_id": "artifact_a"},
    )
    active = EventRecord(
        elapsed_s=3.2,
        method="auip.updated",
        params={"status": "active", "engagement_mode": "collaborate"},
    )
    prepared = evaluate_turn(
        expect="auip-prepare",
        events=[
            prepared_created,
            _result("done", "locus"),
            launch_requested,
            active,
        ],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
        expected_work_item_id="work_old",
    )
    assert prepared["status"] == "passed"
    assert prepared["provider_runs_started"] == 1
    assert prepared["new_work_item_ids"] == []

    forked_created = _created("locus", "work_new")
    forked_created.params["metadata"] = {"source": "auip_prepare"}
    forked = evaluate_turn(
        expect="auip-prepare",
        events=[
            forked_created,
            _result("done", "locus"),
            launch_requested,
            active,
        ],
        before_work=_work("work_old"),
        after_work=_work("work_old", "work_new"),
        chat_completed=True,
        expected_work_item_id="work_old",
    )
    assert forked["status"] == "failed"
    assert any("forked a new WorkItem" in item for item in forked["failures"])
    assert any("expected WorkItem work_old" in item for item in forked["failures"])

    no_launch_created = _created("locus", "work_old")
    no_launch_created.params["metadata"] = {"source": "auip_prepare"}
    no_launch = evaluate_turn(
        expect="auip-prepare",
        events=[no_launch_created, _result("done", "locus")],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert no_launch["status"] == "failed"
    assert any("launch request" in item for item in no_launch["failures"])
    assert any("active AppSession" in item for item in no_launch["failures"])


def test_auip_preparation_accepts_one_signed_progress_only_successor() -> None:
    predecessor = _created("codex", "work_old")
    predecessor.params["metadata"] = {
        "source": "auip_prepare",
        "work": {
            "work_item_id": "work_old",
            "operation_id": "operation_a",
            "attempt_id": "attempt_a",
        },
    }
    predecessor.params["work"] = predecessor.params["metadata"]["work"]
    successor = _created("codex", "work_old")
    successor.elapsed_s = 2.0
    successor.params["run_id"] = "run_b"
    successor.params["metadata"] = {
        "source": "auip_prepare",
        "work": {
            "work_item_id": "work_old",
            "operation_id": "operation_a",
            "attempt_id": "attempt_b",
        },
        "provider_recovery": {
            "reason": "progress_only_completion",
            "root_attempt_id": "attempt_a",
            "predecessor_attempt_id": "attempt_a",
            "ordinal": 1,
        },
    }
    successor.params["work"] = successor.params["metadata"]["work"]
    first_result = _result("error", "codex")
    first_result.elapsed_s = 2.1
    first_result.params["metadata"] = {
        "provider_completion": {
            "classification": "progress_only_completion",
        }
    }
    second_result = _result("done", "codex")
    second_result.elapsed_s = 3.0
    second_result.params["run_id"] = "run_b"
    evaluated = evaluate_turn(
        expect="auip-prepare",
        events=[
            predecessor,
            first_result,
            successor,
            second_result,
            EventRecord(
                elapsed_s=3.1,
                method="auip.launch.requested",
                params={"request_id": "launch_recovered"},
            ),
            EventRecord(
                elapsed_s=3.2,
                method="auip.updated",
                params={"status": "active"},
            ),
        ],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
        expected_work_item_id="work_old",
    )

    assert evaluated["status"] == "passed"
    assert evaluated["provider_runs_started"] == 2
    assert evaluated["terminal_statuses"] == {"run_a": "error", "run_b": "done"}


def test_amend_reuses_identity_while_provider_expectation_checks_selection() -> None:
    amend = evaluate_turn(
        expect="amend",
        events=[_created("locus", "work_old"), _result("done", "locus")],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
    )
    assert amend["status"] == "passed"
    assert amend["related_work_item_ids"] == ["work_old"]

    provider = evaluate_turn(
        expect="provider:openclaw",
        events=[_created("browser"), _result("done", "browser")],
        before_work=_work(),
        after_work=_work("work_a"),
        chat_completed=True,
    )
    assert provider["status"] == "failed"
    assert "openclaw" in provider["failures"][0]


def test_amend_identity_is_read_from_canonical_provider_result_metadata() -> None:
    created = _created("locus", "")
    created.params.pop("work")
    result = _result("done", "locus")
    result.params["metadata"] = {
        "work": {"work_item_id": "work_old", "operation_number": 2}
    }
    evaluated = evaluate_turn(
        expect="amend",
        events=[created, result],
        before_work=_work("work_old"),
        after_work=_work("work_old"),
        chat_completed=True,
        expected_work_item_id="work_old",
    )
    assert evaluated["status"] == "passed"
    assert evaluated["related_work_item_ids"] == ["work_old"]


def test_native_and_host_created_events_count_as_one_provider_run() -> None:
    host_created = _created("locus", "work_a")
    native_created = _created("locus", "work_a")
    native_created.elapsed_s = 2.0
    result = evaluate_turn(
        expect="provider:locus",
        events=[host_created, native_created, _result()],
        before_work=_work(),
        after_work=_work("work_a"),
        chat_completed=True,
    )
    assert result["status"] == "passed"
    assert result["provider_runs_started"] == 1
    assert result["run_ids"] == ["run_a"]


def test_work_expectation_requires_a_successful_terminal_result() -> None:
    missing = evaluate_turn(
        expect="provider:browser",
        events=[_created("browser")],
        before_work=_work(),
        after_work=_work("work_a"),
        chat_completed=True,
    )
    assert missing["status"] == "failed"
    assert "no terminal result" in missing["failures"][-1]

    failed = evaluate_turn(
        expect="provider:browser",
        events=[_created("browser"), _result("error", "browser")],
        before_work=_work(),
        after_work=_work("work_a"),
        chat_completed=True,
    )
    assert failed["status"] == "failed"
    assert failed["terminal_statuses"] == {"run_a": "error"}

    routing_only = evaluate_turn(
        expect="provider:browser",
        events=[
            _created("browser"),
            EventRecord(
                elapsed_s=2.0,
                method="tts.sentence_start",
                params={"sentence_id": "active_observer_note"},
            ),
        ],
        before_work=_work(),
        after_work=_work("work_a"),
        chat_completed=True,
        require_terminal_success=False,
    )
    assert routing_only["status"] == "passed"
    assert routing_only["unclosed_tts_sentence_ids"] == ["active_observer_note"]


def test_named_status_canary_checks_target_identity_and_tts_closure() -> None:
    complete = EventRecord(
        elapsed_s=1.0,
        method="chat.complete",
        params={"work_item_id": "work_note"},
    )
    start = EventRecord(
        elapsed_s=2.0,
        method="tts.sentence_start",
        params={"sentence_id": "sentence_1"},
    )
    end = EventRecord(
        elapsed_s=3.0,
        method="tts.sentence_end",
        params={"sentence_id": "sentence_1"},
    )
    turn_done = EventRecord(
        elapsed_s=3.1,
        method="tts.turn_complete",
        params={},
    )
    passed = evaluate_turn(
        expect="no-work",
        events=[complete, start, end, turn_done],
        before_work=_work("work_note", "work_latest"),
        after_work=_work("work_note", "work_latest"),
        chat_completed=True,
        expected_work_item_id="work_note",
    )
    assert passed["status"] == "passed"
    assert passed["unclosed_tts_sentence_ids"] == []

    failed = evaluate_turn(
        expect="no-work",
        events=[complete, start, turn_done],
        before_work=_work("work_note", "work_latest"),
        after_work=_work("work_note", "work_latest"),
        chat_completed=True,
        expected_work_item_id="work_latest",
    )
    assert failed["status"] == "failed"
    assert any("expected WorkItem" in item for item in failed["failures"])
    assert any("unclosed sentence" in item for item in failed["failures"])

    interrupted = EventRecord(
        elapsed_s=2.5,
        method="chat.interrupted",
        params={"turn_id": "old_turn"},
    )
    replacement_start = EventRecord(
        elapsed_s=2.6,
        method="tts.sentence_start",
        params={"sentence_id": "replacement_sentence"},
    )
    replacement_end = EventRecord(
        elapsed_s=2.7,
        method="tts.sentence_end",
        params={"sentence_id": "replacement_sentence"},
    )
    barge_in = evaluate_turn(
        expect="no-work",
        events=[
            start,
            interrupted,
            replacement_start,
            replacement_end,
            turn_done,
            complete,
        ],
        before_work=_work("work_note"),
        after_work=_work("work_note"),
        chat_completed=True,
    )
    assert barge_in["status"] == "passed"
    assert barge_in["unclosed_tts_sentence_ids"] == []

    observer_start = EventRecord(
        elapsed_s=4.0,
        method="tts.sentence_start",
        params={"sentence_id": "sentence_observer"},
    )
    observer_end = EventRecord(
        elapsed_s=5.0,
        method="tts.sentence_end",
        params={"sentence_id": "sentence_observer"},
    )
    stale_completion = evaluate_turn(
        expect="no-work",
        events=[complete, start, end, turn_done, observer_start, observer_end],
        before_work=_work("work_note"),
        after_work=_work("work_note"),
        chat_completed=True,
    )
    assert stale_completion["status"] == "failed"
    assert stale_completion["latest_tts_utterance_completed"] is False
    assert any("latest TTS utterance" in item for item in stale_completion["failures"])

    observer_idle = evaluate_turn(
        expect="no-work",
        events=[complete, observer_start, observer_end],
        before_work=_work("work_note"),
        after_work=_work("work_note"),
        chat_completed=True,
        output_idle={
            "chat": {"busy": False},
            "tts": {"pending_sentences": 0},
            "playback": {"is_playing": False, "pending_audio": 0},
        },
    )
    assert observer_idle["status"] == "passed"
    assert observer_idle["latest_tts_utterance_completed"] is True


def test_report_writer_keeps_machine_and_human_evidence_together() -> None:
    report = {
        "mode": "turn",
        "status": "passed",
        "started_at_utc": "2026-08-13T00:00:00+00:00",
        "url": "ws://127.0.0.1:17777/ws",
        "evaluation": {
            "expect": "no-work",
            "providers": [],
            "new_work_item_ids": [],
            "failures": [],
        },
        "events": [{"elapsed_s": 1.0, "method": "chat.complete", "params": {}}],
        "limitations": ["not an acoustic oracle"],
    }
    with tempfile.TemporaryDirectory(prefix="live_acceptance_report_") as temp:
        json_path, md_path = write_report(report, Path(temp))
        assert json_path.is_file()
        assert md_path.is_file()
        assert '"status": "passed"' in json_path.read_text(encoding="utf-8")
        markdown = md_path.read_text(encoding="utf-8")
        assert "PASS" in markdown
        assert "not an acoustic oracle" in markdown


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all live runtime acceptance tests passed")


if __name__ == "__main__":
    _main()
