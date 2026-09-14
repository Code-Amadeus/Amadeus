"""Future conversation starts are read-only without rewriting old executions."""
import asyncio
from dataclasses import replace
import json

from agent_host.provider_types import ProviderInputDelivery
from test_cooperative_context_recovery import host_factory as host_factory


async def test_legacy_address_keeps_active_run_then_narrows_same_native_conversation(host_factory):
    host = host_factory()
    try:
        host.adapter.manifest = replace(host.adapter.manifest,
            capabilities=replace(host.adapter.manifest.capabilities, append_input=True))
        async def append_input(_run_id, _text):
            return ProviderInputDelivery("delivered")
        host.adapter.append_input = append_input
        host.runtime.register(host.adapter)
        host.adapter.release.clear()
        first = await host.send("先看看这个目录。", "legacy-first")
        await asyncio.wait_for(host.adapter.started.wait(), 2)
        child = host.loop.get_context(first["child_id"])
        original_id, original_path = child.child_id, child.workspace
        original_requirements = child.requirements.to_dict()
        native = child.native_session
        assert original_requirements["workspace_access"] == "write"
        host.loop.context_requirements[child.provider] = replace(child.requirements, workspace_access="read")
        assert host.loop.prepare_conversation_contract(child) is False
        followup = await host.send("主要看哪些文件？", "during-legacy")
        assert followup["state"] == "delivered", followup
        assert len(host.adapter.requests) == 1 and child.requirements.workspace_access == "write"
        host.adapter.release.set()
        await host.loop.wait()
        before_source = host.ledger.find_admission("chat:" + host.ingress.session_id, "legacy-first")
        next_turn = await host.send("刚才看到的结构再解释一下。", "after-legacy")
        await host.loop.wait()
        assert next_turn["state"] == "started" and next_turn["child_id"] == original_id
        assert child.workspace == original_path and child.native_session == native
        assert host.adapter.requests[-1].requirements.workspace_access == "read"
        assert host.adapter.requests[-1].session == native
        assert len(host.loop.context_catalog()) == 1
        stored = host.loop._state.load_context(original_id)
        retired = json.loads(stored["retired_write_contract"])
        assert retired["requirements"] == original_requirements
        assert retired["through_run_id"] == first["run_id"]
        assert stored["requirements"].workspace_access == "read"
        assert host.ledger.find_admission("chat:" + host.ingress.session_id, "legacy-first")["plan_json"] == before_source["plan_json"]
        count = len(host.adapter.requests)
        await host.send("先看看这个目录。", "legacy-first")
        assert len(host.adapter.requests) == count
    finally:
        host.adapter.release.set()
        await host.close()
