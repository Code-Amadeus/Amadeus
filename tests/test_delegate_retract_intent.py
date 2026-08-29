"""Giving withdrawal a verb, so "stop" stops instead of starting.

The prompt named no way to take work back — cancel, stop and retract appeared
nowhere — while the roster still told the model to "stop that task and start
nothing". Handed an instruction with no mechanism, the model used the only
structured action it had and delegated "stop the running task" as work to
execute: measured 2026-07-31 on B1, "把那个停了" created a third WorkItem in 3
of 5 runs, and the intent gate could not catch it because the model genuinely
believed it was executing. Even the harmless variant had the character say it
had stopped something that was never cancelled.

Interruption is host-owned and already built (ProviderRuntime.cancel, plus a
cancel on every adapter), so this is wiring, not capability.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config.settings as settings
from llm.prompts import get_system_prompt
from server.app import _delegate_declared_retract, _handle_delegate


def _both_flags(value: bool = True):
    return (
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", value),
        patch.object(settings, "DELEGATE_RETRACT_INTENT", value),
    )


def test_the_verb_is_only_honoured_while_both_flags_hold() -> None:
    intent_flag, retract_flag = _both_flags()
    with intent_flag, retract_flag:
        assert _delegate_declared_retract({"intent": "retract"}) is True
        assert _delegate_declared_retract({"intent": "RETRACT"}) is True
        assert _delegate_declared_retract({"intent": "execute"}) is False
        assert _delegate_declared_retract({}) is False

    # The value is meaningless unless the attribute is part of the contract,
    # and the verb must not be acted on while the host is not wired for it.
    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", False):
        with patch.object(settings, "DELEGATE_RETRACT_INTENT", True):
            assert _delegate_declared_retract({"intent": "retract"}) is False
    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True):
        with patch.object(settings, "DELEGATE_RETRACT_INTENT", False):
            assert _delegate_declared_retract({"intent": "retract"}) is False


def test_a_retraction_cancels_the_one_running_attempt_and_starts_nothing() -> None:
    async def run() -> None:
        cancel = AsyncMock(return_value={"cancelled": True})
        fake_runtime = type(
            "R",
            (),
            {
                "list_runs": staticmethod(
                    lambda: [
                        {"run_id": "r1", "status": "running"},
                        {"run_id": "r0", "status": "done"},
                    ]
                ),
                "cancel": cancel,
            },
        )()
        intent_flag, retract_flag = _both_flags()
        with (
            intent_flag,
            retract_flag,
            patch.dict(
                "sys.modules",
                {"agent_host.provider_runtime": type("M", (), {"runtime": fake_runtime})},
            ),
            patch("server.app._delegate_provider_for_task") as router,
        ):
            result = await _handle_delegate(
                "停止刚才那个任务",
                {"provider": "locus", "intent": "retract", "task": "stop it"},
            )
        cancel.assert_awaited_once_with("r1"), "the finished run must be left alone"
        router.assert_not_called(), "a withdrawal must never be routed to a provider"
        assert "cancelled" in result

    asyncio.run(run())


def test_an_unresolvable_retraction_says_so_and_never_guesses() -> None:
    """Cancelling the wrong task cannot be undone by asking afterwards."""

    async def check(runs: list[dict], expected: str, cancels: bool) -> None:
        cancel = AsyncMock(return_value={"cancelled": True})
        fake_runtime = type(
            "R", (), {"list_runs": staticmethod(lambda: runs), "cancel": cancel}
        )()
        intent_flag, retract_flag = _both_flags()
        with (
            intent_flag,
            retract_flag,
            patch.dict(
                "sys.modules",
                {"agent_host.provider_runtime": type("M", (), {"runtime": fake_runtime})},
            ),
            patch("server.app._announce_retract_outcome", new=AsyncMock()) as announced,
            patch("server.app._delegate_provider_for_task") as router,
        ):
            result = await _handle_delegate(
                "算了别弄了", {"provider": "locus", "intent": "retract"}
            )
        assert expected in result, result
        assert cancel.await_count == (1 if cancels else 0)
        router.assert_not_called()
        # Silence would leave the character's "I'm stopping it" standing as fact.
        announced.assert_awaited_once()

    asyncio.run(check([], "nothing was running", cancels=False))
    asyncio.run(
        check(
            [{"run_id": "a", "status": "running"}, {"run_id": "b", "status": "running"}],
            "several tasks are running",
            cancels=False,
        )
    )


def test_cancel_unconfirmed_waits_for_ledger_terminal_truth() -> None:
    async def run() -> None:
        snapshots = iter(
            (
                [{"run_id": "r1", "status": "running"}],
                [{"run_id": "r1", "status": "cancelling"}],
            )
        )
        fake_runtime = type(
            "R",
            (),
            {
                "list_runs": staticmethod(lambda: next(snapshots)),
                "cancel": AsyncMock(
                    return_value={"cancelled": False, "reason": "cancel_unconfirmed"}
                ),
            },
        )()
        intent_flag, retract_flag = _both_flags()
        with (
            intent_flag,
            retract_flag,
            patch.dict(
                "sys.modules",
                {"agent_host.provider_runtime": type("M", (), {"runtime": fake_runtime})},
            ),
            patch("server.app._announce_retract_outcome", new=AsyncMock()) as announced,
        ):
            result = await _handle_delegate(
                "停止它", {"provider": "locus", "intent": "retract", "task": "stop it"}
            )
        assert "awaiting terminal confirmation" in result
        announced.assert_not_awaited()

    asyncio.run(run())


def test_retraction_cancels_a_reserved_progress_recovery() -> None:
    async def run() -> None:
        fake_runtime = type(
            "R",
            (),
            {
                "list_runs": staticmethod(lambda: []),
                "cancel": AsyncMock(),
            },
        )()
        cancel_pending = Mock(return_value=True)
        fake_coordinator = type(
            "C",
            (),
            {
                "pending_provider_recoveries": staticmethod(
                    lambda: [
                        {
                            "attempt_id": "attempt-recovery",
                            "work_item_id": "work-recovery",
                        }
                    ]
                ),
                "cancel_pending_provider_recovery": cancel_pending,
            },
        )()
        intent_flag, retract_flag = _both_flags()
        with (
            intent_flag,
            retract_flag,
            patch.dict(
                "sys.modules",
                {"agent_host.provider_runtime": type("M", (), {"runtime": fake_runtime})},
            ),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=fake_coordinator,
            ),
            patch("server.app._announce_retract_outcome", new=AsyncMock()) as announced,
        ):
            result = await _handle_delegate(
                "停止它",
                {"provider": "codex", "intent": "retract", "task": "stop it"},
            )
        assert "cancelled" in result
        cancel_pending.assert_called_once_with("attempt-recovery")
        fake_runtime.cancel.assert_not_awaited()
        announced.assert_not_awaited()

    asyncio.run(run())


def test_retraction_deduplicates_a_visible_recovery_successor() -> None:
    async def run() -> None:
        cancel_runtime = AsyncMock(return_value={"cancelled": True})
        fake_runtime = type(
            "R",
            (),
            {
                "list_runs": staticmethod(
                    lambda: [
                        {
                            "run_id": "run-successor",
                            "status": "queued",
                            "metadata": {
                                "provider_recovery": {
                                    "reason": "progress_only_completion",
                                    "predecessor_attempt_id": "attempt-recovery",
                                    "ordinal": 1,
                                }
                            },
                        }
                    ]
                ),
                "cancel": cancel_runtime,
            },
        )()
        cancel_pending = Mock(return_value=True)
        fake_coordinator = type(
            "C",
            (),
            {
                "pending_provider_recoveries": staticmethod(
                    lambda: [
                        {
                            "attempt_id": "attempt-recovery",
                            "work_item_id": "work-recovery",
                            "successor_run_id": "",
                        }
                    ]
                ),
                "cancel_pending_provider_recovery": cancel_pending,
            },
        )()
        intent_flag, retract_flag = _both_flags()
        with (
            intent_flag,
            retract_flag,
            patch.dict(
                "sys.modules",
                {"agent_host.provider_runtime": type("M", (), {"runtime": fake_runtime})},
            ),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=fake_coordinator,
            ),
            patch("server.app._announce_retract_outcome", new=AsyncMock()) as announced,
        ):
            result = await _handle_delegate(
                "停止它",
                {"provider": "codex", "intent": "retract", "task": "stop it"},
            )
        assert "cancelled" in result
        cancel_pending.assert_called_once_with("attempt-recovery")
        cancel_runtime.assert_awaited_once_with("run-successor")
        announced.assert_not_awaited()

    asyncio.run(run())


def test_the_verb_appears_in_the_prompt_only_while_the_host_acts_on_it() -> None:
    intent_flag, retract_flag = _both_flags()
    with intent_flag, retract_flag:
        prompt = get_system_prompt("with_delegate")
    assert 'intent="retract"' in prompt
    # Claiming the stop already happened is the failure this replaces.
    assert "止めた" in prompt or "has been stopped" in prompt
    assert "cancel_pending" in prompt
    assert "not another retract" in prompt or "retract ではなく" in prompt
    # The tie-break must survive the insertion.
    assert "既存台帳だけで足りるなら report" in prompt or "ledger facts are report" in prompt

    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True):
        with patch.object(settings, "DELEGATE_RETRACT_INTENT", False):
            without = get_system_prompt("with_delegate")
    assert 'intent="retract"' not in without
    assert 'intent="report"' in without, "turning the verb off keeps the rest intact"


if __name__ == "__main__":
    test_the_verb_is_only_honoured_while_both_flags_hold()
    print("ok: the verb is only honoured while both flags hold")
    test_a_retraction_cancels_the_one_running_attempt_and_starts_nothing()
    print("ok: a retraction cancels the one running attempt and starts nothing")
    test_an_unresolvable_retraction_says_so_and_never_guesses()
    print("ok: an unresolvable retraction says so and never guesses")
    test_retraction_cancels_a_reserved_progress_recovery()
    print("ok: a reserved progress recovery remains retractable")
    test_retraction_deduplicates_a_visible_recovery_successor()
    print("ok: a visible recovery successor remains one retract target")
    test_the_verb_appears_in_the_prompt_only_while_the_host_acts_on_it()
    print("ok: the verb appears in the prompt only while the host acts on it")
    print("all delegate retract tests passed")
