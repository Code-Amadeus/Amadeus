"""Provider-neutral host facts become bounded semantic progress."""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.work_semantic_progress import (
    consume_tool_call,
    remember_tool_call,
    semantic_progress_fact,
)
from server.event_bus import bus
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.protocol import Method


def test_validation_commands_are_facts_but_ordinary_commands_are_not() -> None:
    ordinary = semantic_progress_fact(
        "tool.call",
        {"tool": "Bash", "item_id": "read-1", "command": "git status --short"},
    )
    started = semantic_progress_fact(
        "tool.call",
        {
            "tool": "command_execution",
            "item_id": "test-1",
            "command": "python -m pytest tests/test_game.py",
        },
    )
    finished = semantic_progress_fact(
        "tool.result",
        {"item_id": "test-1", "status": "completed", "exit_code": 0},
        tool_context={
            "tool": "command_execution",
            "item_id": "test-1",
            "command": "python -m pytest tests/test_game.py",
        },
    )
    failed = semantic_progress_fact(
        "tool.result",
        {
            "item_id": "test-2",
            "success": False,
            "status": "completed",
        },
        tool_context={
            "tool": "command_execution",
            "item_id": "test-2",
            "command": "python -m pytest tests/test_game.py",
        },
    )
    assert ordinary is None
    assert started is not None and started.summary == "Project validation started."
    assert finished is not None and finished.summary == "Project validation passed."
    assert failed is not None and failed.summary.startswith("Project validation failed")
    assert started.verified is True and finished.verified is True


def test_untyped_assistant_update_is_candidate_evidence_only() -> None:
    candidate = semantic_progress_fact(
        "assistant.update",
        {"text": "I will inspect the project and then run its tests."},
    )
    assert candidate is not None
    assert candidate.evidence == "candidate"
    assert candidate.verified is False
    assert candidate.milestone == ""


def test_unstructured_dynamic_tool_output_never_becomes_a_progress_fact() -> None:
    result = semantic_progress_fact(
        "tool.result",
        {
            "name": "js",
            "item_id": "dynamic-1",
            "success": True,
            "status": "completed",
            "output": "pvz.html updated; syntax: pass; missing: []",
        },
        tool_context={
            "tool": "js",
            "item_id": "dynamic-1",
        },
    )
    assert result is None


def test_file_results_use_correlated_context_for_both_provider_shapes() -> None:
    contexts = remember_tool_call(
        {},
        {
            "tool": "file_change",
            "item_id": "change-1",
            "changes": [{"path": "src/game.js", "kind": "update"}],
        },
    )
    contexts, direct_context = consume_tool_call(
        contexts,
        {"item_id": "change-1", "status": "completed"},
    )
    direct = semantic_progress_fact(
        "tool.result",
        {"item_id": "change-1", "status": "completed"},
        tool_context=direct_context,
    )
    assert contexts == {}
    assert direct is not None and direct.summary == "Updated project files: game.js."

    locus_contexts = remember_tool_call(
        {},
        {
            "tool": "Write",
            "raw": {
                "toolName": "Write",
                "toolUseId": "write-1",
                "scope": {"kind": "path", "path": "game.html"},
            },
        },
    )
    locus_contexts, locus_context = consume_tool_call(
        locus_contexts,
        {"tool": "Write", "tool_use_id": "write-1", "ok": True},
    )
    locus = semantic_progress_fact(
        "tool.result",
        {"tool": "Write", "tool_use_id": "write-1", "ok": True},
        tool_context=locus_context,
    )
    assert locus_contexts == {}
    assert locus is not None and locus.summary == "Updated project files: game.html."


def test_permission_and_artifact_projection_stays_truthful_and_bounded() -> None:
    blocked = semantic_progress_fact(
        "permission.requested",
        {
            "tool": "Bash",
            "capability": "tool.execute",
            "action": "invoke_tool",
            "scope": ["python -m unittest"],
            "reason": "approval required",
            "diagnosticOnly": True,
        },
    )
    pending = semantic_progress_fact(
        "permission.required",
        {
            "id": "write-approval",
            "tool": "Write",
            "capability": "filesystem.write",
        },
    )
    runtime_artifact = semantic_progress_fact(
        "artifact.created",
        {"artifact_type": "runtime.events", "role": "events", "path": "events.jsonl"},
    )
    browser = semantic_progress_fact(
        "artifact.created",
        {
            "artifact_type": "browser.snapshot",
            "title": "Amadeus search results",
            "url": "https://example.invalid/search",
        },
    )
    assert blocked is not None and "blocked bash" in blocked.summary.lower()
    assert pending is not None and "waiting for permission" in pending.summary.lower()
    assert runtime_artifact is None
    assert browser is not None and browser.summary == "Browser reached Amadeus search results."


def test_verified_fact_reaches_provider_neutral_work_note() -> None:
    async def run() -> None:
        notes: list[dict] = []

        async def capture(_method: str, params: dict) -> None:
            notes.append(params)

        coordinator = WorkActivityCoordinator()
        bus.on(Method.CHAT_WORK_NOTE, capture)
        try:
            common = {
                "provider": "contract-test",
                "run_id": "semantic-file-run",
                "task": "Update the game",
                "metadata": {"session_id": "semantic-session"},
            }
            await coordinator._on_provider_event(
                Method.PROVIDER_EVENT,
                {
                    **common,
                    "type": "tool.call",
                    "payload": {
                        "tool": "file_change",
                        "item_id": "change-1",
                        "changes": [{"path": "src/game.js", "kind": "update"}],
                    },
                },
            )
            await coordinator._on_provider_event(
                Method.PROVIDER_EVENT,
                {
                    **common,
                    "type": "tool.result",
                    "payload": {"item_id": "change-1", "status": "completed"},
                },
            )
        finally:
            bus.off(Method.CHAT_WORK_NOTE, capture)
            await coordinator._leave_work("semantic-file-run", reason="test")

        semantic = [
            note
            for note in notes
            if note.get("metadata", {}).get("narration_keypoint") == "semantic_progress"
        ]
        assert len(semantic) == 1
        assert semantic[0]["summary"] == "Updated project files: game.js."

    asyncio.run(run())


def test_reported_milestone_note_retains_its_evidence_strength() -> None:
    async def run() -> None:
        notes: list[dict] = []

        async def capture(_method: str, params: dict) -> None:
            notes.append(params)

        coordinator = WorkActivityCoordinator()
        bus.on(Method.CHAT_WORK_NOTE, capture)
        try:
            await coordinator._on_provider_event(
                Method.PROVIDER_EVENT,
                {
                    "provider": "contract-test",
                    "run_id": "reported-design-run",
                    "task": "Build the game",
                    "metadata": {"session_id": "semantic-session"},
                    "type": "semantic.progress",
                    "payload": {
                        "milestone": "design",
                        "summary": "I will map the controls before implementing them.",
                        "source": "provider_explicit_progress",
                        "verified": False,
                    },
                },
            )
        finally:
            bus.off(Method.CHAT_WORK_NOTE, capture)
            await coordinator._leave_work("reported-design-run", reason="test")

        semantic = [
            note
            for note in notes
            if note.get("metadata", {}).get("narration_keypoint")
            == "semantic_progress"
        ]
        assert len(semantic) == 1
        metadata = semantic[0]["metadata"]
        assert metadata["semantic_verified"] is False
        assert metadata["semantic_evidence"] == "reported"
        assert metadata["semantic_source"] == "provider_explicit_progress"
        report = next(
            signal for signal in semantic[0]["signals"] if signal.get("label") == "report"
        )
        assert "not verified" in report["detail"]

    asyncio.run(run())


def main() -> None:
    test_validation_commands_are_facts_but_ordinary_commands_are_not()
    test_untyped_assistant_update_is_candidate_evidence_only()
    test_unstructured_dynamic_tool_output_never_becomes_a_progress_fact()
    test_file_results_use_correlated_context_for_both_provider_shapes()
    test_permission_and_artifact_projection_stays_truthful_and_bounded()
    test_verified_fact_reaches_provider_neutral_work_note()
    test_reported_milestone_note_retains_its_evidence_strength()
    print("ok: canonical provider events project into bounded semantic progress facts")


if __name__ == "__main__":
    main()
