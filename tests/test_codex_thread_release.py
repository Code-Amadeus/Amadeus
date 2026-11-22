"""Sequential Work release of exclusive MCP connection ownership.

A loaded native Codex thread owns the MCP runtime that the App Server spawned
for the explicitly bound connections, and that ownership is exclusive while
the thread is loaded. These tests cover the Host-side contract: when a Work's
last dependent run ends, the adapter unsubscribes the thread, waits for the
App Server to confirm the unload through ``thread/loaded/list``, and only then
lets the next Work open an owner of the same connection.

The final section models the app-server side of that boundary with real
synthetic MCP children, mirroring the issue evidence: each child takes an
exclusive data-root lock, writes a cleanup sentinel from a ``finally`` block,
and launches a detached descendant that represents a user application. The
Host never reaches into that process tree; unloading a thread is protocol
work whose settlement is the App Server's business.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import psutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai_codex.errors import TransportClosedError

from agent_host.adapters.codex_app_server import CodexAppServerAdapter
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderSessionHandle
from test_codex_app_server_adapter import (
    _FakeCodex,
    _FakeThread,
    _FakeTurn,
    _request,
    _success_events,
    _turn_completed,
)


async def _drain(event) -> None:
    del event


def _recorded(events: list):
    async def emit(event) -> None:
        events.append(event)

    return emit


def _release(result) -> str:
    return (result.metadata or {}).get("codex", {}).get("thread_release", "")


def test_successful_run_releases_thread_before_returning() -> None:
    async def scenario(root: Path) -> None:
        sdk = _FakeCodex(
            [_FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1")))]
        )
        adapter = CodexAppServerAdapter(codex=sdk)
        events: list = []

        result = await adapter.run(
            _request(root, "first", write=True), "run-1", _recorded(events)
        )

        assert result.status == "done"
        assert sdk.unsubscribes == ["thread-1"]
        assert sdk.loaded == set()
        assert sdk.order == [("open", "thread-1"), ("release", "thread-1")]
        assert _release(result) == "released"
        releases = [event for event in events if event.type == "native.release"]
        assert releases and releases[0].payload["release"] == "released"
        assert releases[0].payload["thread_id"] == "thread-1"
        # A terminal turn is not the cleanup fact; the release outcome is a
        # separate Host-observed field that cannot overwrite the business one.
        assert result.metadata["codex"]["turn_status"] == "completed"

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_sequential_works_serialize_release_before_rebind() -> None:
    async def scenario(root: Path) -> None:
        sdk = _FakeCodex(
            [
                _FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1"))),
                _FakeThread("thread-2", _FakeTurn("turn-2", _success_events("turn-2"))),
            ]
        )
        adapter = CodexAppServerAdapter(codex=sdk)

        first = await adapter.run(_request(root, "first", write=True), "run-1", _drain)
        second = await adapter.run(_request(root, "second", write=True), "run-2", _drain)

        assert first.status == "done" and second.status == "done"
        assert sdk.order == [
            ("open", "thread-1"),
            ("release", "thread-1"),
            ("open", "thread-2"),
            ("release", "thread-2"),
        ]
        assert sdk.unsubscribes == ["thread-1", "thread-2"]
        assert _release(first) == _release(second) == "released"

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_next_thread_never_opens_while_release_is_unconfirmed() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-1", _success_events("turn-1"))
        sdk = _FakeCodex(
            [
                _FakeThread("thread-1", turn),
                _FakeThread("thread-2", _FakeTurn("turn-2", _success_events("turn-2"))),
            ]
        )
        sdk.unload_gate = asyncio.Event()
        adapter = CodexAppServerAdapter(codex=sdk, release_confirm_timeout_s=5)

        first = asyncio.create_task(
            adapter.run(_request(root, "first", write=True), "run-1", _drain)
        )
        # Dispatch the next Work while the first owner's unload is still
        # unconfirmed. It must not open a new owner of the same connection
        # until that release concludes.
        deadline = asyncio.get_running_loop().time() + 5
        while sdk.order != [("open", "thread-1"), ("release", "thread-1")]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        second = asyncio.create_task(
            adapter.run(_request(root, "second", write=True), "run-2", _drain)
        )
        await asyncio.sleep(0.2)
        assert len(sdk.starts) == 1

        sdk.unload_gate.set()
        assert (await asyncio.wait_for(first, 5)).status == "done"
        assert (await asyncio.wait_for(second, 5)).status == "done"
        assert sdk.order == [
            ("open", "thread-1"),
            ("release", "thread-1"),
            ("open", "thread-2"),
            ("release", "thread-2"),
        ]

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_explicit_resume_reloads_only_after_confirmed_release() -> None:
    async def scenario(root: Path) -> None:
        sdk = _FakeCodex(
            [_FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1")))]
        )
        adapter = CodexAppServerAdapter(codex=sdk)

        first = await adapter.run(_request(root, "first", write=True), "run-1", _drain)
        assert first.status == "done" and sdk.loaded == set()
        sdk.threads.append(
            _FakeThread("thread-1", _FakeTurn("turn-2", _success_events("turn-2")))
        )
        second = await adapter.run(
            _request(
                root,
                "continue",
                write=True,
                session=ProviderSessionHandle(provider="codex", session_id="thread-1"),
            ),
            "run-2",
            _drain,
        )

        assert second.status == "done"
        assert sdk.resumes and sdk.resumes[0][0] == "thread-1"
        assert sdk.order == [
            ("open", "thread-1"),
            ("release", "thread-1"),
            ("open", "thread-1"),
            ("release", "thread-1"),
        ]
        assert _release(second) == "released"

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_last_dependent_run_releases_shared_thread() -> None:
    async def scenario(root: Path) -> None:
        first_turn = _FakeTurn("turn-1", blocked=True)
        second_turn = _FakeTurn("turn-2", _success_events("turn-2"))
        # Both siblings attach to one loaded thread; the App Server keeps one
        # subscription-owned runtime while either side is active.
        thread = _FakeThread(
            "thread-1",
            first_turn,
            queued_turns=[second_turn],
        )
        sdk = _FakeCodex([thread])
        adapter = CodexAppServerAdapter(codex=sdk)

        first = asyncio.create_task(
            adapter.run(_request(root, "first", write=True), "run-1", _drain)
        )
        await asyncio.wait_for(first_turn.started.wait(), 2)
        second = await adapter.run(
            _request(
                root,
                "second",
                write=True,
                session=ProviderSessionHandle(provider="codex", session_id="thread-1"),
            ),
            "run-2",
            _drain,
        )
        # The second sibling ended while the first still owned the thread, so
        # no unsubscribe may have happened yet.
        assert sdk.unsubscribes == []
        assert _release(second) == "shared"

        await first_turn.control_events.put(_turn_completed("turn-1", "completed"))
        first_result = await asyncio.wait_for(first, 5)
        assert first_result.status == "done"
        assert _release(first_result) == "released"
        assert sdk.unsubscribes == ["thread-1"]
        assert _release(second) == "shared"

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_concurrent_runs_open_without_waiting_for_each_other() -> None:
    """Only an in-flight release gates an open; parallel runs stay parallel."""

    async def scenario(root: Path) -> None:
        first_turn = _FakeTurn("turn-1", blocked=True)
        second_turn = _FakeTurn("turn-2", blocked=True)
        sdk = _FakeCodex(
            [
                _FakeThread("thread-1", first_turn),
                _FakeThread("thread-2", second_turn),
            ]
        )
        adapter = CodexAppServerAdapter(codex=sdk)

        first = asyncio.create_task(
            adapter.run(_request(root, "first", write=True), "run-1", _drain)
        )
        second = asyncio.create_task(
            adapter.run(_request(root, "second", write=True), "run-2", _drain)
        )
        await asyncio.wait_for(first_turn.started.wait(), 2)
        await asyncio.wait_for(second_turn.started.wait(), 2)
        # Both runs reached execution while neither had released; the gate
        # must never collapse parallel Work into an accidental serialization.
        assert len(sdk.starts) == 2

        await first_turn.control_events.put(_turn_completed("turn-1", "completed"))
        await second_turn.control_events.put(_turn_completed("turn-2", "completed"))
        assert (await asyncio.wait_for(first, 5)).status == "done"
        assert (await asyncio.wait_for(second, 5)).status == "done"
        assert sorted(sdk.unsubscribes) == ["thread-1", "thread-2"]

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_cancelled_run_releases_thread() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-1", blocked=True)
        sdk = _FakeCodex([_FakeThread("thread-1", turn)])
        adapter = CodexAppServerAdapter(codex=sdk, cancel_confirm_timeout_s=2)

        run = asyncio.create_task(
            adapter.run(_request(root, "task", write=True), "run-1", _drain)
        )
        await asyncio.wait_for(turn.started.wait(), 2)
        cancelled = await asyncio.wait_for(adapter.cancel("run-1"), 3)
        assert cancelled["confirmed"] and cancelled["cancelled"]

        result = await asyncio.wait_for(run, 5)
        assert result.status == "cancelled"
        assert _release(result) == "released"
        assert sdk.unsubscribes == ["thread-1"]

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_timed_out_run_releases_thread() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-1", blocked=True)
        sdk = _FakeCodex([_FakeThread("thread-1", turn)])
        adapter = CodexAppServerAdapter(
            codex=sdk, turn_timeout_s=1, cancel_confirm_timeout_s=2
        )
        request = _request(root, "task", write=True)
        request.metadata = {"timeout_s": 1}

        result = await adapter.run(request, "run-1", _drain)

        assert result.status == "error"
        assert "timed out" in (result.error or "")
        assert result.metadata["codex"]["turn_status"] == "timeout"
        assert _release(result) == "released"
        assert sdk.unsubscribes == ["thread-1"]

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_unconfirmed_release_is_observable_and_distinct() -> None:
    async def scenario(root: Path) -> None:
        sdk = _FakeCodex(
            [_FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1")))]
        )
        sdk.unload_on_unsubscribe = False
        adapter = CodexAppServerAdapter(codex=sdk, release_confirm_timeout_s=0.6)

        result = await adapter.run(_request(root, "task", write=True), "run-1", _drain)

        assert result.status == "done"
        assert _release(result) == "release_unconfirmed"
        assert result.metadata["codex"]["thread_release_detail"] == "unsubscribed"
        assert sdk.unsubscribes == ["thread-1"]

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_failed_release_is_observable_and_preserves_business_outcome() -> None:
    async def scenario(root: Path) -> None:
        sdk = _FakeCodex(
            [_FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1")))]
        )
        sdk.unsubscribe_error = RuntimeError("rpc down")
        adapter = CodexAppServerAdapter(codex=sdk)

        result = await adapter.run(_request(root, "task", write=True), "run-1", _drain)

        assert result.status == "done"
        assert _release(result) == "release_failed"
        assert result.metadata["codex"]["thread_release_detail"].startswith(
            "thread_unsubscribe:RuntimeError"
        )
        assert sdk.unsubscribes == ["thread-1"]

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_injected_client_without_release_api_reports_unsupported() -> None:
    async def scenario(root: Path) -> None:
        sdk = _FakeCodex(
            [_FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1")))]
        )
        # An externally owned client may predate this contract; the state is
        # explicit rather than silently fabricated as released.
        sdk.thread_unsubscribe = None
        adapter = CodexAppServerAdapter(codex=sdk)

        result = await adapter.run(_request(root, "task", write=True), "run-1", _drain)

        assert result.status == "done"
        assert _release(result) == "unsupported"
        assert sdk.unsubscribes == []

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_orphaned_submission_retains_thread_for_graceful_close() -> None:
    async def scenario(root: Path) -> None:
        sdk = _FakeCodex(
            [_FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1")))]
        )

        async def unknown_submission(*args, **kwargs):
            raise TransportClosedError("transport severed")

        sdk.turn_correlated = unknown_submission
        adapter = CodexAppServerAdapter(codex=sdk)

        result = await adapter.run(_request(root, "task", write=True), "run-1", _drain)

        assert result.status == "orphaned"
        # The native turn may still be live; the Host retains the thread
        # rather than tearing down unknown execution.
        assert _release(result) == "retained"
        assert sdk.unsubscribes == []

        outcome = await adapter.close()
        assert outcome is None
        assert sdk.unsubscribes == ["thread-1"]

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_adapter_close_releases_threads_before_transport_teardown() -> None:
    async def scenario(root: Path) -> None:
        turn = _FakeTurn("turn-1", blocked=True)
        sdk = _FakeCodex([_FakeThread("thread-1", turn)])
        adapter = CodexAppServerAdapter(
            codex_factory=lambda config: sdk, sync_desktop_provider=False
        )
        run = asyncio.create_task(
            adapter.run(_request(root, "task", write=True), "run-1", _drain)
        )
        await asyncio.wait_for(turn.started.wait(), 2)

        close_task = asyncio.create_task(adapter.close())
        # close() first settles tracked threads, then tears the SDK transport
        # down; the stream drains once the in-flight turn ends.
        deadline = asyncio.get_running_loop().time() + 5
        while ("close", "") not in sdk.order:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.02)
        release_index = sdk.order.index(("release", "thread-1"))
        close_index = sdk.order.index(("close", ""))
        assert release_index < close_index

        await turn.control_events.put(_turn_completed("turn-1", "interrupted"))
        assert (await asyncio.wait_for(close_task, 5)) is None
        result = await asyncio.wait_for(run, 5)
        assert result.status == "cancelled"

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_orphaned_run_keeps_thread_until_runtime_close() -> None:
    async def scenario(root: Path) -> None:
        sdk = _FakeCodex(
            [_FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1")))]
        )
        adapter = CodexAppServerAdapter(codex=sdk)

        async def unknown_submission(*args, **kwargs):
            raise TransportClosedError("transport severed")

        sdk.turn_correlated = unknown_submission
        runtime = ProviderRuntime()
        runtime.register(adapter)
        record = await runtime.start(_request(root, "task", write=True))

        await asyncio.wait_for(asyncio.shield(record.task_handle), 5)
        assert record.status == "orphaned"
        assert record.metadata["codex"]["thread_release"] == "retained"

        outcome = await asyncio.wait_for(runtime.close(), 5)
        # Orphaned runs intentionally remain pending; the close only has to
        # settle the retained thread's graceful release without errors.
        assert outcome["errors"] == []
        assert sdk.unsubscribes == ["thread-1"]

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


# ---------------------------------------------------------------------------
# Real synthetic MCP children behind a fake app-server boundary.
# ---------------------------------------------------------------------------

_MCP_CHILD = r'''
import os, subprocess, sys
from pathlib import Path
lock_path, ready_path, record_path, sentinel_path = sys.argv[1:5]
fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
if os.name == "nt":
    import msvcrt
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.stderr.write("exclusive lock busy\n")
        sys.exit(3)
else:
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.stderr.write("exclusive lock busy\n")
        sys.exit(3)
spawn = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
         "stderr": subprocess.DEVNULL}
if os.name == "nt":
    spawn["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
else:
    spawn["start_new_session"] = True
app = subprocess.Popen(
    [sys.executable, "-I", "-c", "import time; time.sleep(300)"], **spawn)
Path(record_path).write_text("{} {}".format(os.getpid(), app.pid))
Path(ready_path).write_text("ready")
try:
    for _line in sys.stdin:
        pass
finally:
    Path(sentinel_path).write_text("clean")
'''


class _McpOwningCodex(_FakeCodex):
    """Fake app-server boundary that owns real stdio MCP children.

    Each loaded thread spawns one synthetic MCP server holding an exclusive
    data-root lock. ``thread/unsubscribe`` ends the child's stdin so its
    ``finally`` writes the sentinel before the process exits (graceful), or
    kills it outright (abrupt) — mirroring the two unload policies compared
    in the issue. Launched descendants are detached on purpose: their fate
    after unload is the App Server's teardown contract, never the Host's.
    """

    def __init__(self, threads, workspace: Path, *, graceful: bool = True):
        super().__init__(threads)
        self.workspace = Path(workspace)
        self.graceful = graceful
        self.children_all: list[subprocess.Popen] = []
        self.children: dict[str, subprocess.Popen] = {}
        self.records: dict[str, Path] = {}
        self.sentinels: dict[str, Path] = {}
        self.spawn_ok: dict[str, bool] = {}
        self._script = self.workspace / "mcp_child.py"
        self._script.write_text(_MCP_CHILD)
        self._lock = self.workspace / "mcp-data" / "mcp.lock"
        self._lock.parent.mkdir(parents=True, exist_ok=True)

    async def thread_start(self, **kwargs):
        thread = await super().thread_start(**kwargs)
        await self._spawn_mcp(thread.id)
        return thread

    async def thread_resume(self, thread_id: str, **kwargs):
        was_loaded = thread_id in self.loaded
        thread = await super().thread_resume(thread_id, **kwargs)
        if not was_loaded:
            await self._spawn_mcp(thread_id)
        return thread

    async def _spawn_mcp(self, thread_id: str) -> None:
        seq = len(self.children_all)
        record = self.workspace / f"mcp-record-{seq}.txt"
        ready = self.workspace / f"mcp-ready-{seq}.txt"
        sentinel = self.workspace / f"mcp-sentinel-{seq}.txt"
        child = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(self._script),
                str(self._lock),
                str(ready),
                str(record),
                str(sentinel),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children_all.append(child)
        self.children[thread_id] = child
        self.records[thread_id] = record
        self.sentinels[thread_id] = sentinel
        self.spawn_ok[thread_id] = await asyncio.to_thread(
            self._wait_ready, child, ready
        )

    @staticmethod
    def _wait_ready(child: subprocess.Popen, ready: Path) -> bool:
        import time

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if ready.exists():
                return True
            if child.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    async def thread_unsubscribe(self, thread_id: str):
        self.unsubscribes.append(thread_id)
        self.order.append(("release", thread_id))
        if thread_id not in self.loaded:
            return SimpleNamespace(status="notLoaded")
        child = self.children.get(thread_id)
        if child is not None:
            if self.graceful and child.poll() is None:
                child.stdin.close()
            elif child.poll() is None:
                child.kill()
            try:
                await asyncio.to_thread(child.wait, 10)
            except Exception:
                child.kill()
                await asyncio.to_thread(child.wait, 10)
        self.loaded.discard(thread_id)
        return SimpleNamespace(status="unsubscribed")

    async def close(self) -> None:
        self.closes += 1
        self.order.append(("close", ""))
        for child in self.children_all:
            if child.poll() is None:
                child.kill()
        for child in self.children_all:
            try:
                child.wait(5)
            except Exception:
                pass

    def app_pids(self) -> list[int]:
        pids: list[int] = []
        for record in self.records.values():
            if record.exists():
                pids.extend(int(part) for part in record.read_text().split())
        return pids

    def kill_apps(self) -> None:
        for pid in self.app_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass


def test_synthetic_lock_conflict_proves_exclusive_ownership_model() -> None:
    """The fixture itself must model a real exclusive resource, not a stub."""
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        sdk = _McpOwningCodex([], workspace)
        try:
            asyncio.run(sdk._spawn_mcp("first"))
            assert sdk.spawn_ok["first"]
            asyncio.run(sdk._spawn_mcp("second"))
            assert not sdk.spawn_ok["second"]
        finally:
            asyncio.run(sdk.close())
            sdk.kill_apps()


def test_sequential_works_release_synthetic_mcp_before_next_owner() -> None:
    async def scenario(root: Path) -> None:
        sdk = _McpOwningCodex(
            [
                _FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1"))),
                _FakeThread("thread-2", _FakeTurn("turn-2", _success_events("turn-2"))),
            ],
            root,
        )
        adapter = CodexAppServerAdapter(codex=sdk, release_confirm_timeout_s=10)
        try:
            first = await adapter.run(_request(root, "first", write=True), "run-1", _drain)
            assert first.status == "done"
            assert _release(first) == "released"
            # The first MCP's finally wrote its cleanup sentinel before the
            # Host let any next owner bind the same exclusive resource.
            assert sdk.sentinels["thread-1"].exists()
            assert sdk.children["thread-1"].poll() is not None
            # Graceful unload leaves the MCP-launched user application alive;
            # the Host never reaches into the server process tree.
            app1 = int(sdk.records["thread-1"].read_text().split()[1])
            assert psutil.pid_exists(app1)

            second = await adapter.run(
                _request(root, "second", write=True), "run-2", _drain
            )
            assert second.status == "done"
            # The second owner only acquired the resource after the first
            # release concluded — otherwise the exclusive lock exits the
            # child before its ready sentinel exists.
            assert sdk.spawn_ok["thread-2"]
            assert sdk.sentinels["thread-1"].stat().st_mtime <= sdk.records[
                "thread-2"
            ].stat().st_mtime
            assert sdk.order == [
                ("open", "thread-1"),
                ("release", "thread-1"),
                ("open", "thread-2"),
                ("release", "thread-2"),
            ]
        finally:
            await adapter.close()
            sdk.kill_apps()

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


def test_abrupt_unload_is_confirmed_release_without_fabricated_cleanup() -> None:
    async def scenario(root: Path) -> None:
        sdk = _McpOwningCodex(
            [_FakeThread("thread-1", _FakeTurn("turn-1", _success_events("turn-1")))],
            root,
            graceful=False,
        )
        adapter = CodexAppServerAdapter(codex=sdk, release_confirm_timeout_s=10)
        try:
            result = await adapter.run(_request(root, "task", write=True), "run-1", _drain)

            assert result.status == "done"
            # The unload itself is confirmed released, but that fact never
            # claims the server's cleanup ran: an abrupt teardown leaves no
            # sentinel, and the Host does not fabricate one.
            assert _release(result) == "released"
            assert sdk.children["thread-1"].poll() is not None
            assert not sdk.sentinels["thread-1"].exists()
        finally:
            await adapter.close()
            sdk.kill_apps()

    with tempfile.TemporaryDirectory() as temp_dir:
        asyncio.run(scenario(Path(temp_dir)))


if __name__ == "__main__":
    test_successful_run_releases_thread_before_returning()
    test_sequential_works_serialize_release_before_rebind()
    test_next_thread_never_opens_while_release_is_unconfirmed()
    test_explicit_resume_reloads_only_after_confirmed_release()
    test_last_dependent_run_releases_shared_thread()
    test_concurrent_runs_open_without_waiting_for_each_other()
    test_cancelled_run_releases_thread()
    test_timed_out_run_releases_thread()
    test_unconfirmed_release_is_observable_and_distinct()
    test_failed_release_is_observable_and_preserves_business_outcome()
    test_injected_client_without_release_api_reports_unsupported()
    test_orphaned_submission_retains_thread_for_graceful_close()
    test_adapter_close_releases_threads_before_transport_teardown()
    test_orphaned_run_keeps_thread_until_runtime_close()
    test_synthetic_lock_conflict_proves_exclusive_ownership_model()
    test_sequential_works_release_synthetic_mcp_before_next_owner()
    test_abrupt_unload_is_confirmed_release_without_fabricated_cleanup()
