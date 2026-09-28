from __future__ import annotations

import pytest

from server.control_ledger import ControlLedgerConflict
from server.cooperative_provider_loop import LoopConflict
from test_cooperative_context_recovery import host_factory as host_factory


async def test_clear_rotates_durable_binding_without_removing_active_context(
        host_factory) -> None:
    host = host_factory()
    host.adapter.release.clear()
    try:
        started = await host.send("检查目录。", "active-before-clear")
        await host.adapter.started.wait()
        child_id = started["child_id"]
        run_id = started["run_id"]
        child = host.loop.get_context(child_id)
        old_token = host.loop._binding.token

        host.loop.bind_context("")

        assert host.loop.bound_context_id == ""
        assert host.loop._binding.token != old_token
        assert host.loop.get_context(child_id) is child
        assert host.runtime.get_run(run_id) is not None
        assert host.runtime.get_run(run_id).status in {"queued", "running"}
        binding, rows = host.loop._state.load_catalog()
        assert (binding["context_id"] or "") == ""
        assert binding["token"] == host.loop._binding.token
        retained = next(row for row in rows if row["context_id"] == child_id)
        assert retained["run_id"] == run_id
        assert retained["run_status"] in {"dispatching", "queued", "running"}

        with pytest.raises(ControlLedgerConflict, match="binding changed"):
            host.loop._state.bind(
                "",
                expected_token=old_token,
                expected_context_id=child_id,
            )

        host.adapter.release.set()
        await host.loop.wait()
        assert host.loop.bound_context_id == ""
        assert host.runtime.get_run(run_id).status == "done"
        assert host.loop.get_context(child_id).run_status == "done"
        assert not host.loop.get_context(child_id).closed

        cleared_token = host.loop._binding.token
        host.loop.bind_context(child_id)
        assert host.loop.bound_context_id == child_id
        assert host.loop._binding.token not in {old_token, cleared_token}
        assert host.loop.get_context(child_id) is child
        assert host.runtime.get_run(run_id) is not None
    finally:
        host.adapter.release.set()
        await host.loop.wait()
        await host.close()


async def test_empty_binding_survives_reload_and_old_child_can_be_rebound(
        host_factory) -> None:
    first = host_factory()
    database = first.ledger.path
    try:
        started = await first.send("检查目录。", "first-context")
        await first.loop.wait()
        child_id = started["child_id"]
        child = first.loop.get_context(child_id)
        native_session = child.native_session
        first.loop.bind_context("")
        cleared_token = first.loop._binding.token
    finally:
        await first.close()

    restored = host_factory(database=database, allow_allocate=False)
    try:
        assert restored.loop.bound_context_id == ""
        assert restored.loop._binding.token == cleared_token
        assert any(
            row["context_id"] == child_id
            for row in restored.loop.context_catalog()
        )

        restored.loop.bind_context(child_id)
        rebound = restored.loop.get_context(child_id)
        assert rebound is not None
        assert rebound.native_session == native_session
        continued = await restored.send("继续检查。", "continued-context")
        await restored.loop.wait()

        assert continued["child_id"] == child_id
        assert restored.adapter.requests[-1].session == native_session
        assert restored.loop._binding.token != cleared_token
    finally:
        await restored.close()


@pytest.mark.parametrize("invalid", [None, " ", "missing-context"])
async def test_invalid_nonempty_or_nonstring_binding_stays_rejected(
        host_factory, invalid) -> None:
    host = host_factory()
    try:
        started = await host.send("检查目录。", "valid-context")
        await host.loop.wait()
        before = host.loop._binding
        durable_before = host.loop._state.load_catalog()[0]

        with pytest.raises(LoopConflict, match="recipient absent or closed"):
            host.loop.bind_context(invalid)

        assert host.loop._binding is before
        assert host.loop._state.load_catalog()[0] == durable_before
        assert host.loop.bound_context_id == started["child_id"]
    finally:
        await host.close()
