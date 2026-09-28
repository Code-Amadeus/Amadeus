"""Bounded inference, ordered effects, and unchanged line-level semantic cadence."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import websockets

from server.handlers.vn_player_handler import VNPlayerHandler
from server.vn_text_sources import AgentVNTextSource, LunaVNTextSource
from vn_player.runtime import VNPlayerRuntime
from vn_player.schemas import default_response


def speech(index):
    return {**default_response("speak"), "importance": .8, "confidence": .9,
            "speak": {"text": f"発言 {index}。", "priority": "normal"}}


async def fixture(root):
    spoken = []
    runtime = VNPlayerRuntime(root, speak_callback=lambda p: spoken.append(p))
    runtime._llm_enabled = runtime._immediate_llm_enabled = True
    runtime._silence_pressure_enabled = False
    await runtime.start({"session_id": "pipeline", "prompt_pack": "base", "script_path": "",
                         "summary_llm_enabled": False, "retrospective_llm_enabled": False,
                         "max_reactions_per_minute": 1000})
    runtime.llm = SimpleNamespace(configured=lambda: True, supports_visual=lambda: False)
    return runtime, spoken


def read(store, name):
    path = store.root / name
    return [json.loads(row) for row in path.read_text("utf-8").splitlines()] if path.exists() else []


def test_inference_overlaps_but_speech_and_state_commit_in_line_order(tmp_path):
    async def run():
        runtime, spoken = await fixture(tmp_path)
        gates = [asyncio.Event() for _ in range(4)]
        started, all_started = [], asyncio.Event()
        async def complete(messages, *, on_ready, **kwargs):
            index = len(started)
            started.append(messages[1]["content"])
            if len(started) == 3:
                all_started.set()
            await on_ready(speech(index + 1))
            await gates[index].wait()
            return speech(index + 1), "{}"
        runtime.llm.complete_json = complete
        for index in range(3):
            result = await runtime.submit_line({"text": f"ROW_{index + 1}_ONLY"})
            assert result["status"] == "accepted"
        fourth = asyncio.create_task(runtime.submit_line({"text": "ROW_4_ONLY"}))
        await asyncio.wait_for(all_started.wait(), 2)
        assert not fourth.done(), "at most three lines may be in flight"
        assert len(started) == 3
        assert all(f"ROW_{index + 1}_ONLY" in text for index, text in enumerate(started))
        assert "ROW_2_ONLY" not in started[0] and "ROW_3_ONLY" not in started[1]
        # Later inference completes first but cannot publish a reaction ahead.
        gates[1].set()
        gates[2].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not read(runtime.store, "reactions.jsonl")
        assert [item["line"]["seq"] for item in spoken] == [1]
        gates[0].set()
        gates[3].set()
        await fourth
        await runtime.drain_lines()
        reactions = read(runtime.store, "reactions.jsonl")
        assert [row["line_id"] for row in reactions] == ["000001", "000002", "000003", "000004"]
        assert [item["line"]["seq"] for item in spoken] == [1], "cooldown is based on each line's ordinal"
        assert len(runtime._recent_speaks) == 1, "early and final handling consume one speech budget"
        timings = [row["payload"] for row in read(runtime.store, "runtime_events.jsonl") if row["type"] == "vn.line.timing"]
        assert len(timings) == 4
        await runtime.stop()
    asyncio.run(run())


def test_truncated_stream_tail_keeps_one_speech_without_memory_writes(tmp_path):
    async def run():
        runtime, spoken = await fixture(tmp_path)
        async def complete(messages, *, on_ready, **kwargs):
            await on_ready(speech(1))
            return None, "truncated after speech"
        runtime.llm.complete_json = complete
        result = await runtime.ingest_line({"text": "Displayed text."})
        assert result["reaction"]["decision"] == "speak"
        assert len(spoken) == 1 and len(read(runtime.store, "reactions.jsonl")) == 1
        assert result["context_applied"] == []
        assert any(row["type"] == "vn.immediate.stream_incomplete"
                   for row in read(runtime.store, "runtime_events.jsonl"))
        await runtime.stop()
    asyncio.run(run())


def test_player_question_streams_while_commentary_is_paused(tmp_path):
    async def run():
        runtime, spoken = await fixture(tmp_path)
        release, header_ready = asyncio.Event(), asyncio.Event()
        async def complete(messages, *, on_ready, **kwargs):
            await on_ready(speech(1))
            header_ready.set()
            await release.wait()
            return speech(1), "{}"
        runtime.llm.complete_json = complete
        await runtime.set_preferences({"session_id": "pipeline", "commentary_paused": True})
        question = asyncio.create_task(runtime.player_intervention("ask", {"text": "What happened?"}))
        await asyncio.wait_for(header_ready.wait(), 2)
        await asyncio.sleep(0)
        assert len(spoken) == 1 and not question.done()
        release.set()
        assert (await question)["reaction"]["decision"] == "speak"
        assert len(spoken) == 1
        await runtime.stop()
    asyncio.run(run())


def test_context_request_can_stream_its_answer_once(tmp_path):
    async def run():
        runtime, spoken = await fixture(tmp_path)
        calls = []
        async def complete(messages, *, lane, on_ready, metrics, **kwargs):
            calls.append(lane)
            if lane == "immediate":
                metrics.update(started_at_ms=10, first_token_at_ms=11, completed_at_ms=12)
                return {**default_response("context_request"), "context_requests": [{"layer": "short_memory"}]}, "{}"
            metrics.update(started_at_ms=20, first_token_at_ms=21, speech_ready_at_ms=23, completed_at_ms=25)
            await on_ready(speech(1))
            return speech(1), "{}"
        runtime.llm.complete_json = complete
        result = await runtime.ingest_line({"text": "Displayed line needing memory."})
        assert calls == ["immediate", "immediate_context_retry"]
        assert result["reaction"]["decision"] == "speak" and len(spoken) == 1
        timing = read(runtime.store, "runtime_events.jsonl")[-1]["payload"]
        assert timing["started_at_ms"] == 10 and timing["first_token_at_ms"] == 11
        assert timing["completed_at_ms"] == 25 and timing["speech_ready_at_ms"] == 23
        assert timing["context_retry"]["started_at_ms"] == 20
        await runtime.stop()
    asyncio.run(run())


def test_stop_before_task_start_releases_old_admission_slots(tmp_path):
    async def run():
        runtime, spoken = await fixture(tmp_path)
        runtime.llm.complete_json = AsyncMock(return_value=(speech(1), "{}"))
        for _ in range(3):
            await runtime.submit_line({"text": "Already admitted."})
        slots = runtime._line_slots
        await runtime.stop()
        # Done callbacks also run for tasks cancelled before their body starts.
        for _ in range(3):
            await asyncio.wait_for(slots.acquire(), 2)
        assert not spoken
        await runtime.start({"session_id": "new", "prompt_pack": "base", "script_path": ""})
        runtime._llm_enabled = False
        assert (await runtime.ingest_line({"text": "New session line."}))["status"] == "ok"
        await runtime.stop()
    asyncio.run(run())


def test_pause_revokes_a_header_waiting_behind_an_earlier_turn(tmp_path):
    async def run():
        runtime, spoken = await fixture(tmp_path)
        release, both_ready = asyncio.Event(), asyncio.Event()
        calls = 0
        async def complete(messages, *, on_ready, **kwargs):
            nonlocal calls
            calls += 1
            index = calls
            await on_ready(speech(index))
            if index == 2:
                both_ready.set()
            await release.wait()
            return speech(index), "{}"
        runtime.llm.complete_json = complete
        await runtime.submit_line({"text": "First."})
        await runtime.submit_line({"text": "Second."})
        await asyncio.wait_for(both_ready.wait(), 2)
        await runtime.set_preferences({"session_id": "pipeline", "commentary_paused": True})
        release.set()
        await runtime.drain_lines()
        assert all(item["line"]["seq"] == 1 for item in spoken)
        assert read(runtime.store, "reactions.jsonl")[1]["response"]["decision"] == "silence"
        await runtime.stop()
    asyncio.run(run())


def test_restart_revokes_running_and_backpressured_old_lines(tmp_path):
    async def run():
        runtime, spoken = await fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def complete(messages, *, on_ready, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # A backend that ignores cancellation.
            await on_ready(speech(1))
            return speech(1), "{}"
        runtime.llm.complete_json = complete
        for _ in range(3):
            await runtime.submit_line({"text": "Real repeated dialogue."})
        await asyncio.wait_for(entered.wait(), 2)
        old_store, tasks = runtime.store, list(runtime._line_tasks)
        fourth = asyncio.create_task(runtime.submit_line({"text": "Queued old dialogue."}))
        await asyncio.sleep(0)
        await runtime.start({"session_id": "replacement", "prompt_pack": "base", "script_path": ""})
        release.set()
        await asyncio.gather(*tasks)
        assert (await asyncio.wait_for(fourth, 2))["reason"] == "session_changed"
        assert len(read(old_store, "raw_lines.jsonl")) == 3
        assert not spoken and not read(old_store, "reactions.jsonl")
        assert not runtime.store.short_memory() and not runtime.activity()
        await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("source_kind", ["agent", "luna"])
def test_both_adapters_reach_the_same_parallel_runtime(tmp_path, source_kind):
    async def run():
        runtime, _ = await fixture(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def complete(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                entered.set()
            await release.wait()
            return default_response("silence"), "{}"
        runtime.llm.complete_json = complete
        handler = VNPlayerHandler()
        handler._runtime = runtime
        async def send(ws):
            for text in ["Repeated dialogue.", "Repeated dialogue.", "Another line."]:
                await ws.send(json.dumps({"type": "copyText", "sentence": text}) if source_kind == "agent" else text)
            await ws.wait_closed()
        async with websockets.serve(send, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            source = (AgentVNTextSource if source_kind == "agent" else LunaVNTextSource)(handler.submit_source_line, AsyncMock())
            await source.start({}, {"agentWsPort": port, "lunaWsUrl": f"ws://127.0.0.1:{port}/api/ws/text/origin"}, target_pid=None)
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert len(runtime.store.short_memory()) == 3
                assert source.status()[1]["lineCount"] == 3
                release.set()
                await runtime.drain_lines()
                assert len(read(runtime.store, "reactions.jsonl")) == 3
            finally:
                release.set()
                await source.stop()
                await runtime.stop()
    asyncio.run(run())
