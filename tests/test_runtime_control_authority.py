"""Reversible ControlDecision authority at the transport commit point."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.chat_runtime import ChatRuntime, _TurnState
from server.control_authority import resolve_control_authority


def _evidence(*, status="ok", outcome="agree", actions=(), reason=""):
    return SimpleNamespace(
        decision_status=status,
        outcome=outcome,
        canonical_actions=tuple(actions),
        notes=(),
        reason=reason,
    )


def _state(question: str = "do it") -> _TurnState:
    return _TurnState(
        gui_callback=None,
        turn_id="turn-authority",
        question=question,
        session_id="session-authority",
    )


def test_policy_uses_only_unavailable_as_non_focus_fallback() -> None:
    fallback = ({"provider": "browser", "intent": "execute", "task": "open"},)
    unavailable = resolve_control_authority(
        decision_status="unavailable",
        decision_outcome="unavailable",
        fallback_actions=fallback,
    )
    assert unavailable.disposition == "audit_unavailable"
    assert unavailable.actions == fallback

    for status in ("invalid", "incomplete"):
        result = resolve_control_authority(
            decision_status=status,
            decision_outcome=status,
            fallback_actions=fallback,
        )
        assert result.disposition == "failed_closed"
        assert result.actions == ()


def test_policy_keeps_persistent_focus_fail_closed_when_unavailable() -> None:
    for control in (
        {"provider": "locus", "intent": "focus", "project_id": "project_a"},
        {"provider": "locus", "intent": "execute", "focus": "set", "task": "edit"},
        {"provider": "locus", "intent": "execute", "focus": "clear", "task": "edit"},
    ):
        result = resolve_control_authority(
            decision_status="unavailable",
            decision_outcome="unavailable",
            fallback_actions=(control,),
        )
        assert result.disposition == "failed_closed"
        assert result.actions == ()


def test_policy_distinguishes_correction_from_suppression() -> None:
    corrected = resolve_control_authority(
        decision_status="ok",
        decision_outcome="diverge",
        canonical_actions=(
            {"provider": "browser", "intent": "execute", "action": "open"},
        ),
    )
    assert corrected.disposition == "corrected"
    assert len(corrected.actions) == 1

    suppressed = resolve_control_authority(
        decision_status="ok",
        decision_outcome="suppressed",
    )
    assert suppressed.disposition == "suppressed"
    assert suppressed.should_announce_block is False


def test_authority_does_not_turn_a_control_target_into_export_authority() -> None:
    runtime = ChatRuntime()
    actions = runtime._prepare_authority_actions(
        _state("那你去做吧。"),
        (
            {
                "provider": "locus",
                "intent": "execute",
                "target": "desktop",
                "task": "井字棋を作る。",
            },
        ),
    )

    assert len(actions) == 1
    attrs = actions[0]["attrs"]
    assert attrs["target"] == "desktop"
    assert "_host_external_export_target" not in attrs
    assert attrs["_host_source_user_text"] == "那你去做吧。"


def test_authority_starts_at_tag_close_without_blocking_later_role_text() -> None:
    async def run() -> None:
        gate = asyncio.Event()
        captured = []

        class Observer:
            def capture(self, batch):
                captured.append(batch)

                async def decide():
                    await gate.wait()
                    return _evidence(
                        outcome="diverge",
                        actions=(
                            {
                                "provider": "browser",
                                "intent": "execute",
                                "action": "open",
                                "task": "open the verified page",
                            },
                        ),
                    )

                return decide()

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=Observer(),
            control_proposal_authority=True,
            control_proposal_authority_timeout_s=1.0,
        )
        st = _state("open the page")
        dispatched = []
        with patch(
            "core.chat_runtime.record_actions",
            side_effect=lambda actions: dispatched.extend(actions),
        ):
            first = runtime._consume_stream_chunk(
                st,
                '先说明。[DELEGATE provider="openclaw" intent="execute" task="open it"]',
            )
            assert first == "先说明。"
            assert len(captured) == 1
            assert captured[0].commit_point == "delegate_tag_closed"
            assert dispatched == []
            assert st.delegate_seen is True
            assert st.work_delegate_seen is False

            # Returning from tag consumption did not await the decision. Text
            # before the tag can keep playing while the callback is pending.
            await asyncio.sleep(0)
            assert dispatched == []

            gate.set()
            await runtime._wait_for_control_authority(st)

        assert len(dispatched) == 1
        attrs = dispatched[0]["attrs"]
        assert attrs["provider"] == "browser"
        assert attrs["task"] == "open the verified page"
        assert attrs["_host_source_user_text"] == "open the page"
        assert st.work_delegate_seen is True
        assert "openclaw" not in st.history_response
        assert "\x00CONTROL_AUTHORITY" not in st.history_response
        assert st.history_response.startswith(
            '先说明。[DELEGATE provider="browser"'
        )
        assert st.history_response.endswith("]")

    asyncio.run(run())


def test_unavailable_non_focus_falls_back_once_without_a_second_query() -> None:
    async def run() -> None:
        calls = 0
        blocked = []

        class Observer:
            def capture(self, _batch):
                nonlocal calls
                calls += 1
                raise RuntimeError("offline")

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=Observer(),
            control_proposal_authority=True,
            control_authority_block_callback=lambda resolution, session: blocked.append(
                (resolution, session)
            ),
        )
        st = _state("open it")
        dispatched = []
        with patch(
            "core.chat_runtime.record_actions",
            side_effect=lambda actions: dispatched.extend(actions),
        ):
            runtime._consume_stream_chunk(
                st,
                '[DELEGATE provider="browser" intent="execute" action="open" task="open it"]',
            )
            await runtime._wait_for_control_authority(st)

        assert calls == 1
        assert len(dispatched) == 1
        assert dispatched[0]["attrs"]["provider"] == "browser"
        assert blocked == []
        assert "[DELEGATE" in st.history_response

    asyncio.run(run())


def test_authority_deadline_uses_the_same_bounded_fallback() -> None:
    async def run() -> None:
        class Observer:
            def capture(self, _batch):
                async def never_finishes():
                    await asyncio.Event().wait()

                return never_finishes()

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=Observer(),
            control_proposal_authority=True,
            control_proposal_authority_timeout_s=0.01,
        )
        st = _state("open it")
        dispatched = []
        with patch(
            "core.chat_runtime.record_actions",
            side_effect=lambda actions: dispatched.extend(actions),
        ):
            runtime._consume_stream_chunk(
                st,
                '[DELEGATE provider="browser" intent="execute" action="open" task="open it"]',
            )
            await runtime._wait_for_control_authority(st)

        assert len(dispatched) == 1
        assert dispatched[0]["attrs"]["provider"] == "browser"
        assert st.control_authority_resolved is True

    asyncio.run(run())


def test_unavailable_focus_is_visible_and_never_dispatches() -> None:
    async def run() -> None:
        blocked = []

        class Observer:
            def capture(self, _batch):
                async def fail():
                    raise RuntimeError("offline")

                return fail()

        async def announce(resolution, session_id):
            blocked.append((resolution.disposition, session_id))

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=Observer(),
            control_proposal_authority=True,
            control_authority_block_callback=announce,
        )
        st = _state("switch to Amadeus")
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                st,
                '[DELEGATE provider="locus" intent="focus" project_id="project_a"]',
            )
            await runtime._wait_for_control_authority(st)

        record.assert_not_called()
        assert blocked == [("failed_closed", "session-authority")]
        assert "[DELEGATE" not in st.history_response
        assert st.focus_delegate_attrs == {}

    asyncio.run(run())


def test_unexpected_application_error_removes_marker_and_fails_visibly() -> None:
    async def run() -> None:
        blocked = []

        class Observer:
            def capture(self, _batch):
                async def accept():
                    return _evidence(
                        actions=(
                            {"provider": "browser", "intent": "execute", "task": "open"},
                        )
                    )

                return accept()

        async def announce(resolution, _session_id):
            blocked.append(resolution.disposition)

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=Observer(),
            control_proposal_authority=True,
            control_authority_block_callback=announce,
        )
        st = _state("open")
        with (
            patch.object(
                runtime,
                "_prepare_authority_actions",
                side_effect=RuntimeError("bad adapter"),
            ),
            patch("core.chat_runtime.record_actions") as record,
        ):
            runtime._consume_stream_chunk(
                st,
                '[DELEGATE provider="browser" intent="execute" task="open"]',
            )
            await runtime._wait_for_control_authority(st)

        record.assert_not_called()
        assert blocked == ["failed_closed"]
        assert st.control_authority_resolved is True
        assert st.control_effective_actions == []
        assert "\x00CONTROL_AUTHORITY" not in st.history_response

    asyncio.run(run())


def test_suppressed_proposal_cannot_be_reinterpreted_as_an_omission() -> None:
    async def run() -> None:
        class Observer:
            def capture(self, _batch):
                async def suppress():
                    return _evidence(status="ok", outcome="suppressed", actions=())

                return suppress()

        runtime = ChatRuntime()
        blocked = []
        runtime.configure(
            control_proposal_observer=Observer(),
            control_proposal_authority=True,
            control_authority_block_callback=lambda resolution, session_id: blocked.append(
                (resolution.disposition, session_id)
            ),
        )
        st = _state("create denied.txt")
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                st,
                '[DELEGATE provider="locus" intent="execute" task="create denied.txt"]',
            )
            await runtime._wait_for_control_authority(st)
            with patch.object(
                ChatRuntime,
                "_request_delegate_resend",
                side_effect=AssertionError("suppression reached omission resend"),
            ):
                repaired = await runtime._repair_missing_delegate(
                    st,
                    st.question,
                    session_id=st.session_id,
                )

        record.assert_not_called()
        assert blocked == []
        assert repaired is False
        assert st.control_authority_resolved is True

    asyncio.run(run())


def test_authority_configuration_requires_a_capture_boundary() -> None:
    runtime = ChatRuntime()
    try:
        runtime.configure(control_proposal_authority=True)
    except RuntimeError as exc:
        assert "capture-capable" in str(exc)
    else:
        raise AssertionError("authority started without a ControlDecision observer")


def test_compound_authority_dispatches_only_the_ordered_compound_capture() -> None:
    async def run() -> None:
        captures = []

        class Observer:
            def capture(self, _batch):
                raise AssertionError("single A capture ran beside compound authority")

            def capture_compound_shadow(self, batch):
                captures.append(batch)

                async def decide():
                    return _evidence(
                        outcome="diverge",
                        actions=(
                            {
                                "provider": "codex",
                                "intent": "amend",
                                "task": "edit alpha",
                            },
                            {
                                "provider": "codex",
                                "intent": "report",
                                "subject": "work_item",
                                "workspace_ref": "work_beta",
                            },
                        ),
                    )

                return decide()

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=Observer(),
            control_proposal_authority=True,
            compound_control_authority=True,
        )
        st = _state("edit alpha and report beta")
        dispatched = []
        with patch(
            "core.chat_runtime.record_actions",
            side_effect=lambda actions: dispatched.extend(actions),
        ):
            runtime._consume_stream_chunk(
                st,
                '[DELEGATE provider="codex" intent="amend" task="edit alpha and report beta"]',
            )
            await runtime._wait_for_control_authority(st)

        assert len(captures) == 1
        assert [action["attrs"]["intent"] for action in dispatched] == [
            "amend",
            "report",
        ]
        assert st.control_authority_resolved is True
        assert st.history_response.count("[DELEGATE") == 2

    asyncio.run(run())


def test_compound_authority_failure_never_uses_the_single_proposal_fallback() -> None:
    async def run() -> None:
        class Observer:
            def capture(self, _batch):
                raise AssertionError("single A capture ran beside compound authority")

            def capture_compound_shadow(self, _batch):
                raise RuntimeError("decomposition offline")

        blocked = []
        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=Observer(),
            control_proposal_authority=True,
            compound_control_authority=True,
            control_authority_block_callback=lambda resolution, _session: blocked.append(
                resolution.disposition
            ),
        )
        st = _state("edit alpha and report beta")
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                st,
                '[DELEGATE provider="codex" intent="amend" task="edit alpha and report beta"]',
            )
            await runtime._wait_for_control_authority(st)

        record.assert_not_called()
        assert blocked == ["failed_closed"]
        assert "[DELEGATE" not in st.history_response

    asyncio.run(run())


def test_production_block_callback_publishes_a_provider_neutral_fact() -> None:
    async def run() -> None:
        from server import app as server_app
        from server.protocol import Method

        resolution = resolve_control_authority(
            decision_status="invalid",
            decision_outcome="invalid",
            reason="bad protocol",
        )
        emitted = AsyncMock()
        notes = []
        with (
            patch("server.event_bus.bus.emit", new=emitted),
            patch("server.work_context.add_work_note", side_effect=notes.append),
        ):
            await server_app._announce_control_authority_block(
                resolution,
                "session-authority",
            )

        assert len(notes) == 1
        note = notes[0]
        assert note["provider"] == "host"
        assert note["session_id"] == "session-authority"
        assert note["speak"] is True
        assert note["phase"] == "Checkpoint"
        assert note["metadata"]["control_authority_blocked"] is True
        assert note["metadata"]["execution_started"] is False
        assert note["metadata"]["narration_keypoint"] == "execution_blocked"
        emitted.assert_awaited_once_with(Method.CHAT_WORK_NOTE, note)

    asyncio.run(run())
