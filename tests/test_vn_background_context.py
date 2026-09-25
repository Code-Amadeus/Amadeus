"""Background context preserves trigger snapshots without delaying either source."""
import asyncio
import json
import re
from unittest.mock import AsyncMock

import pytest
import websockets

from server.vn_text_sources import AgentVNTextSource, LunaVNTextSource
from vn_player.runtime import VNPlayerRuntime
from vn_player.schemas import VNProfile, default_response


def model_result(lane, pack):
    if lane == "retrospective":
        return {"window": pack["window"], "strength": .25, "ttl_lines": 30}, "{}"
    seq = pack["current_line"]["seq"]
    return {"decision": "context_patch", "context_patches": [{
        "layer": "summary", "target": "story_summary_log", "action": "append",
        "item": {"id": f"summary_{seq}", "summary": pack["current_line"]["text"], "evidence_refs": []},
    }]}, "{}"


async def make_runtime(path, emit=None):
    runtime = VNPlayerRuntime(path, event_emit=emit)
    runtime._llm_enabled = runtime._immediate_llm_enabled = True
    await runtime.start({"session_id": "old", "prompt_pack": "base", "script_path": "",
                         "summary_llm_enabled": True, "retrospective_llm_enabled": True})
    runtime.llm.configured = lambda: True
    return runtime


@pytest.mark.parametrize("source_name", ["agent", "luna"])
def test_slow_context_lanes_preserve_cadence_snapshot_and_immediate_order(tmp_path, source_name):
    async def run():
        shown, summaries, immediate = [], [], []
        published_all = asyncio.Event()
        async def emit(method, payload):
            if method == "vn.line":
                shown.append(payload["line"]["text"])
                if len(shown) == 81:
                    published_all.set()
            if method == "vn.summary":
                summaries.append(payload)
        runtime = await make_runtime(tmp_path, emit)
        release = asyncio.Event()
        entered = {lane: asyncio.Event() for lane in ("summary", "retrospective")}
        active = {lane: 0 for lane in entered}
        calls = {lane: [] for lane in entered}
        async def model(messages, *, lane, **kwargs):
            if lane == "immediate":
                immediate.append(int(re.search(r"咖啡馆的第 (\d+) 句", messages[-1]["content"])[1]))
                await asyncio.sleep(0)
                return default_response("silence"), "{}"
            content = messages[-1]["content"]
            pack = json.loads(content[content.index("{"):])
            calls[lane].append(pack)
            active[lane] += 1
            assert active[lane] == 1, "each context lane must remain serial"
            entered[lane].set()
            try:
                await release.wait()
                return model_result(lane, pack)
            finally:
                active[lane] -= 1
        runtime.llm.complete_json = model
        lines = [f"咖啡馆的第 {index} 句台词。" for index in range(1, 82)]
        async def stream(ws):
            for line in lines:
                await ws.send(json.dumps({"type": "copyText", "sentence": line}) if source_name == "agent" else line)
            await ws.wait_closed()
        source = (AgentVNTextSource if source_name == "agent" else LunaVNTextSource)(runtime.submit_line, AsyncMock())
        async with websockets.serve(stream, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            params = {"bridgeMode": "websocket", "agentWsPort": port, "lunaWsUrl": f"ws://127.0.0.1:{port}/api/ws/text/origin"}
            await source.start({}, params, target_pid=None)
            try:
                await asyncio.wait_for(published_all.wait(), 5)
                await asyncio.gather(*(event.wait() for event in entered.values()))
                assert shown == lines
                assert not summaries, "a model result must not be fabricated while blocked"
                await runtime.drain_lines()
                release.set()
                await asyncio.wait_for(runtime.wait_for_context_updates(), 5)
                assert immediate == list(range(1, 82))
                assert [p["current_line"]["seq"] for p in calls["summary"]] == [12, 24, 36, 48, 60, 72]
                assert [p["current_line"]["seq"] for p in calls["retrospective"]] == [40, 80]
                for lane, snapshots in calls.items():
                    for pack in snapshots:
                        recent = pack["short_memory" if lane == "summary" else "recent_lines"]
                        assert max(row["seq"] for row in recent) == pack["current_line"]["seq"]
                        assert len(recent) == min(pack["current_line"]["seq"], runtime.profile.short_memory_lines)
                assert len(summaries) == 6 and len(runtime.store.story_summary_log()) == 6
                assert runtime.store.retrospective_bias()["expires_at_line_count"] == 110
            finally:
                await source.stop()
                await runtime.stop()
    asyncio.run(run())


@pytest.mark.parametrize("lane,trigger", [("summary", 12), ("retrospective", 40)])
@pytest.mark.parametrize("boundary", ["stop", "restart"])
def test_late_context_cannot_write_after_stop_or_restart(tmp_path, lane, trigger, boundary):
    async def run():
        emitted = []
        runtime = await make_runtime(tmp_path, lambda m, p: emitted.append((m, p)))
        runtime.profile.capabilities.update(immediate=False, summary=lane == "summary", retrospective=lane == "retrospective")
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def model(messages, **kwargs):
            content = messages[-1]["content"]
            pack = json.loads(content[content.index("{"):])
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()  # Simulate a backend completing despite cancellation.
            return model_result(lane, pack)
        runtime.llm.complete_json = model
        for index in range(trigger * 2):
            await runtime.ingest_line({"text": f"旧会话第 {index} 句。"})
        await asyncio.wait_for(entered.wait(), 2)
        pending = list(runtime._context_tasks.values())
        old_store = runtime.store
        before_bias = old_store.retrospective_bias()
        if boundary == "stop":
            await runtime.stop()
        else:
            await runtime.start({"session_id": "new", "prompt_pack": "base", "script_path": ""})
        await asyncio.wait_for(cancelled.wait(), 2)
        new_bias = runtime.store.retrospective_bias()
        release.set()
        await asyncio.gather(*pending)
        assert not old_store.story_summary_log() and old_store.retrospective_bias() == before_bias
        assert not runtime.store.story_summary_log() and runtime.store.retrospective_bias() == new_bias
        assert not [row for row in emitted if row[0] in {"vn.summary", "vn.context.updated"}]
        assert not runtime._context_tasks and not runtime._context_queues
        await runtime.stop()
    asyncio.run(run())


def test_lookahead_input_alias_has_one_capability_source_of_truth():
    profile = VNProfile.from_params({"lookahead_enabled": False})
    assert profile.capabilities["lookahead"] is False
    assert "lookahead_enabled" not in profile.to_dict()
    override = VNProfile.from_params({"lookahead_enabled": False, "capabilities": {"lookahead": True}})
    assert override.capabilities["lookahead"] is True


def test_failed_background_job_is_visible_and_next_trigger_still_runs(tmp_path):
    async def run():
        emitted = []
        runtime = await make_runtime(tmp_path, lambda m, p: emitted.append((m, p)))
        runtime.profile.capabilities.update(immediate=False, retrospective=False)
        calls = []
        async def model(messages, *, lane, **kwargs):
            content = messages[-1]["content"]
            pack = json.loads(content[content.index("{"):])
            calls.append(pack["current_line"]["seq"])
            if len(calls) == 1:
                raise RuntimeError("summary service unavailable")
            return model_result(lane, pack)
        runtime.llm.complete_json = model
        for index in range(24):
            await runtime.ingest_line({"text": f"第 {index} 句。"})
        await runtime.wait_for_context_updates()
        assert calls == [12, 24]
        assert [p["source"] for m, p in emitted if m == "vn.error"] == ["summary"]
        assert len(runtime.store.story_summary_log()) == 1
        await runtime.stop()
    asyncio.run(run())


def test_invalid_replacement_does_not_cancel_current_context_work(tmp_path):
    async def run():
        runtime = await make_runtime(tmp_path)
        runtime.profile.capabilities.update(immediate=False, retrospective=False)
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(messages, *, lane, **kwargs):
            content = messages[-1]["content"]
            pack = json.loads(content[content.index("{"):])
            entered.set()
            await release.wait()
            return model_result(lane, pack)
        runtime.llm.complete_json = model
        for index in range(12):
            await runtime.ingest_line({"text": f"第 {index} 句。"})
        await entered.wait()
        with pytest.raises(ValueError):
            await runtime.start({"prompt_pack": "unsupported"})
        release.set()
        await runtime.wait_for_context_updates()
        assert len(runtime.store.story_summary_log()) == 1
        assert runtime.profile.session_id == "old"
        await runtime.stop()
    asyncio.run(run())
