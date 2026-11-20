"""Narration retains evidence modality and the latest lifecycle facts."""
from server.work_observer import ObserverSession, WorkObserverCoordinator
from server.work_observer_llm import _compact_note


def _merge(*notes):
    observer = WorkObserverCoordinator()
    session = ObserverSession(narration_id="run", run_id="run", session_id="session", provider="synthetic", goal="Browse documentation")
    for note in notes:
        note.update(run_id="run", session_id="session", provider="synthetic")
        session.add_note(note)
        observer._narration_governor.observe(note, output_busy=True)
    return observer._merged_narration_note(session)


def test_pending_permission_does_not_adopt_planned_actions_as_its_summary():
    plan = {"phase": "Work", "summary": "I will start the host and open the isolated browser.", "metadata": {"narration_keypoint": "semantic_progress", "semantic_milestone": "design", "semantic_verified": False, "semantic_evidence": "reported"}}
    permission = {"phase": "Checkpoint", "importance": "blocking", "summary": "Allow the status check?", "metadata": {"narration_keypoint": "permission_pending", "attention": "permission"}}
    merged = _merge(plan, permission)
    assert merged["summary"] == "Allow the status check?"
    compact = _compact_note(plan)
    assert compact["progress_context"]["semantic_milestone"] == "design"
    assert compact["progress_context"]["semantic_verified"] is False
    assert _compact_note(merged)["progress_context"]["attention"] == "permission"


def test_confirmed_cancellation_supersedes_pending_cancellation_narration():
    pending = {"phase": "Work", "summary": "Cancellation was requested but has not been confirmed yet.", "metadata": {"narration_keypoint": "stalled", "cancellation": {"in_flight": True}}}
    terminal = {"source": "work_ledger", "phase": "Result", "summary": "The attempt was cancelled; partial changes need review.", "metadata": {"narration_keypoint": "terminal", "execution_status": "cancelled", "completeness": "incomplete", "attention": "review", "rationale": "The attempt was cancelled; partial changes need review."}}
    merged = _merge(pending, terminal)
    assert merged["summary"] == terminal["summary"]
    truth = _compact_note(merged)["terminal_truth"]
    assert truth["execution_status"] == "cancelled"
    assert truth["completeness"] == "incomplete"
    assert truth["attention"] == "review"


def test_pending_cancellation_is_not_promoted_to_confirmed():
    note = {"phase": "Work", "summary": "Cancellation requested.", "metadata": {"narration_keypoint": "stalled", "cancellation": {"in_flight": True}, "execution_status": "running"}}
    compact = _compact_note(note)
    assert compact["progress_context"]["cancellation"]["in_flight"] is True
    assert compact["terminal_truth"]["execution_status"] == "running"


def test_terminal_merge_keeps_actual_validation_but_not_earlier_plan():
    plan = {"phase": "Work", "summary": "I will run validation.", "metadata": {"narration_keypoint": "directional_progress"}}
    validation = {"phase": "Work", "summary": "Validation found an error.", "metadata": {"narration_keypoint": "semantic_progress", "semantic_milestone": "validation", "semantic_verified": True}}
    terminal = {"phase": "Result", "summary": "The attempt failed.", "metadata": {"narration_keypoint": "terminal", "execution_status": "failed", "completeness": "incomplete"}}
    merged = _merge(plan, validation, terminal)
    assert plan["summary"] not in merged["summary"]
    assert validation["summary"] in merged["summary"]
    assert terminal["summary"] in merged["summary"]


def test_outcome_verdict_does_not_erase_host_lifecycle():
    note = {"phase": "Result", "metadata": {"execution_status": "cancelled", "completeness": "incomplete", "outcome_verdict": {"verified": False, "provider_report_allowed": False, "facet": "browser.page_state"}}}
    truth = _compact_note(note)["terminal_truth"]
    assert truth["execution_status"] == "cancelled"
    assert truth["completeness"] == "incomplete"
    assert truth["verified"] is False
    assert truth["provider_report_allowed"] is False
