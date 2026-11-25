"""Independent retrieval windows precede one bounded final validity stage."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from config.environment import EnvironmentReader
from config.memory import MemoryConfig, MEMORY_FINAL_VALIDITY_TIMEOUT_S
from server import cooperative_provider_loop as loop_module
from server.reference_deadline import deadline_now
from test_cooperative_character_rag import loop_for


@pytest.mark.parametrize("disabled_callback", [False, True])
async def test_450ms_character_rag_keeps_half_second_budget_when_memory_disabled(disabled_callback):
    async def role_reference(_text):
        await asyncio.sleep(.45)
        return "SYNTHETIC_CHARACTER_REFERENCE"

    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, role_reference)
    if disabled_callback:
        loop.memory_reference = AsyncMock(return_value=None)
    try:
        await loop.submit("ordinary", input_id="budget")
        assert "SYNTHETIC_CHARACTER_REFERENCE" in query.call_args.args[0][0]["content"]
    finally:
        await loop.close()


async def test_retrievals_overlap_then_final_validation_gets_one_separate_short_window(monkeypatch):
    started, windows, timeouts = set(), {}, []
    release = asyncio.Event()
    original_await_until = loop_module.await_until
    async def observed_await_until(awaitable, deadline):
        timeouts.append(deadline - deadline_now())
        return await original_await_until(awaitable, deadline)
    monkeypatch.setattr(loop_module, "await_until", observed_await_until)

    async def role_reference(_text):
        started.add("role")
        if len(started) == 2:
            release.set()
        await release.wait()
        return "ROLE_REFERENCE"

    async def memory_reference(_text, _turn, *, deadline):
        windows["recall"] = deadline - deadline_now()
        started.add("memory")
        if len(started) == 2:
            release.set()
        await release.wait()
        return "versioned-candidates"

    async def validate(reference, *, deadline):
        assert reference == "versioned-candidates" and started == {"role", "memory"}
        windows["final"] = deadline - deadline_now()
        return {"authority":"user_reference_only", "entries":[{"text":"VALIDATED_REFERENCE"}]}

    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, role_reference)
    loop.memory_reference, loop.memory_validate = memory_reference, validate
    loop.memory_timeout_s = 1.25
    try:
        await loop.submit("ordinary", input_id="overlap")
        assert 0 < windows["recall"] <= 1.25
        assert 0 < windows["final"] <= MEMORY_FINAL_VALIDITY_TIMEOUT_S + 1e-9
        assert any(.49 < timeout <= .5 for timeout in timeouts)
        assert any(1.24 < timeout <= 1.25 for timeout in timeouts)
        assert any(0 < timeout <= MEMORY_FINAL_VALIDITY_TIMEOUT_S + 1e-9 for timeout in timeouts)
        frame = json.loads(query.call_args.args[0][1]["content"])
        assert frame["memory_reference"]["entries"] == [{"text":"VALIDATED_REFERENCE"}]
    finally:
        await loop.close()


async def test_role_timeout_keeps_timely_memory_for_final_validation_after_recall_window():
    deadlines, validation_started = [], []
    async def role_reference(_text):
        await asyncio.Event().wait()
    async def memory_reference(_text, _turn, *, deadline):
        deadlines.append(deadline)
        return "candidate"
    async def validate(_reference, *, deadline):
        now = deadline_now()
        assert deadlines[0] < now < deadline
        validation_started.append(True)
        return {"entries":[{"text":"VALIDATED_REFERENCE"}]}
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, role_reference)
    loop.memory_reference, loop.memory_validate = memory_reference, validate
    loop.memory_timeout_s = .05
    loop.role_reference_timeout_s = .15
    try:
        await loop.submit("ordinary", input_id="expired-recall")
        frame = json.loads(query.call_args.args[0][1]["content"])
        assert frame["memory_reference"]["entries"] == [{"text":"VALIDATED_REFERENCE"}]
        assert validation_started == [True]
    finally:
        await loop.close()


@pytest.mark.parametrize("late_return", [False, True])
async def test_final_validation_timeout_omits_memory_without_another_recall_window(late_return):
    cancelled = []
    async def validate(_reference, *, deadline):
        assert 0 < deadline - deadline_now() <= MEMORY_FINAL_VALIDITY_TIMEOUT_S + 1e-9
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if late_return:
                return {"entries":[{"text":"LATE_REFERENCE"}]}
            raise
        finally:
            cancelled.append(True)
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, AsyncMock(return_value="ROLE_REFERENCE"))
    loop.memory_reference, loop.memory_validate = AsyncMock(return_value="candidate"), validate
    loop.memory_timeout_s = 1.25
    try:
        await loop.submit("ordinary", input_id="final-timeout")
        assert cancelled == [True]
        loop.memory_reference.assert_awaited_once()
        messages = query.call_args.args[0]
        assert "ROLE_REFERENCE" in messages[0]["content"]
        assert "memory_reference" not in json.loads(messages[1]["content"])
    finally:
        await loop.close()


@pytest.mark.parametrize("late_return", [False, True])
async def test_timed_out_memory_cannot_return_late_or_cancel_independent_role_reference(late_return):
    cancelled = []
    async def role_reference(_text):
        await asyncio.sleep(.04)
        return "ROLE_REFERENCE"
    async def memory_reference(_text, _turn, *, deadline):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            if late_return:
                return "LATE_CANDIDATE"
            raise
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, role_reference)
    loop.memory_reference, loop.memory_validate = memory_reference, AsyncMock()
    loop.memory_timeout_s = .01
    try:
        await loop.submit("ordinary", input_id="memory-timeout")
        assert cancelled == [True]
        loop.memory_validate.assert_not_awaited()
        messages = query.call_args.args[0]
        assert "ROLE_REFERENCE" in messages[0]["content"]
        assert "memory_reference" not in json.loads(messages[1]["content"])
    finally:
        await loop.close()


@pytest.mark.parametrize("stop", ["close", "cancel"])
async def test_close_or_cancel_during_retrieval_leaves_no_deliverable_late_reference(stop):
    started, release = asyncio.Event(), asyncio.Event()
    async def memory_reference(_text, _turn, *, deadline):
        started.set()
        await release.wait()
        return "LATE_CANDIDATE"
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, None)
    loop.memory_reference, loop.memory_validate = memory_reference, AsyncMock()
    submission = asyncio.create_task(loop.submit("ordinary", input_id="stopped"))
    closing = None
    try:
        await started.wait()
        if stop == "close":
            closing = asyncio.create_task(loop.close())
            await asyncio.sleep(0)
            assert loop._closed
        else:
            loop._inputs["stopped"][1].cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await submission
        if closing is not None:
            await closing
        query.assert_not_awaited()
        loop.memory_validate.assert_not_awaited()
    finally:
        release.set()
        await loop.close()


async def test_default_memory_phases_add_at_most_200ms_without_role_rag(monkeypatch):
    config = MemoryConfig.from_environment(EnvironmentReader({}))
    assert MemoryConfig().recall_timeout_s == config.recall_timeout_s == .15
    assert MemoryConfig.from_environment(EnvironmentReader({
        "AMADEUS_MEMORY_RECALL_TIMEOUT_S":"1.25"})).recall_timeout_s == 1.25
    assert config.recall_timeout_s + MEMORY_FINAL_VALIDITY_TIMEOUT_S == pytest.approx(.2)
    timeouts = []
    original_await_until = loop_module.await_until
    async def observed_await_until(awaitable, deadline):
        timeouts.append(deadline - deadline_now())
        return await original_await_until(awaitable, deadline)
    monkeypatch.setattr(loop_module, "await_until", observed_await_until)
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, None)
    loop.memory_reference = AsyncMock(return_value="candidate")
    loop.memory_validate = AsyncMock(return_value={"entries":[{"text":"VALIDATED_REFERENCE"}]})
    try:
        assert loop.memory_timeout_s == config.recall_timeout_s
        assert loop.role_reference_timeout_s == .5
        await loop.submit("ordinary", input_id="default-phases")
        assert len(timeouts) == 2
        assert .14 < max(timeouts) <= config.recall_timeout_s
        assert 0 < min(timeouts) <= MEMORY_FINAL_VALIDITY_TIMEOUT_S + 1e-9
        assert sum(timeouts) <= .2 + 1e-9
        assert "memory_reference" in json.loads(query.call_args.args[0][1]["content"])
    finally:
        await loop.close()


async def test_cancel_during_final_validation_propagates_even_when_callback_suppresses_it():
    started = asyncio.Event()
    async def validate(_reference, *, deadline):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"entries":[{"text":"CANCELLED_REFERENCE"}]}
    query = AsyncMock(return_value='{"action":null,"say":"ordinary"}')
    loop = loop_for(query, None)
    loop.memory_reference = AsyncMock(return_value="candidate")
    loop.memory_validate = validate
    submission = asyncio.create_task(loop.submit("ordinary", input_id="cancel-validation"))
    try:
        await started.wait()
        loop._inputs["cancel-validation"][1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission
        query.assert_not_awaited()
    finally:
        await loop.close()
