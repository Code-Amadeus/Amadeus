"""Lifecycle contracts ported from Amadeus.Wallpaper.Tests, plus wire regressions.

Native tests use uniquely named synthetic Windows pipes; they never touch Lively.
"""
from dataclasses import asdict, replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest

HELPER = Path(__file__).resolve().parents[1] / "electron/resources/windows-wallpaper/helper.py"
spec = importlib.util.spec_from_file_location("windows_wallpaper_helper", HELPER)
h = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = h
spec.loader.exec_module(h)


class FakeHost:
    def __init__(self):
        self.items, self.reported = [], None
        self.options = h.HostOptions(0, 1, False, False)
        self.running = h.ProcessIdentity(100, 200)
        self.live = {self.running}
        self.stopped, self.dispatch, self.identity_failure = None, None, None
        self.fail_next, self.fail_after, self.fail_pending = False, False, False
        self.close_noop, self.ignore_restored = False, False
        self.set_calls = self.restore_calls = self.reads = 0
        self.closed, self.pending, self.on_confirmation = [], None, None
        self.mount_failure = TimeoutError("wallpaper mount confirmation timed out")
        self.pending_failure = OSError("native SetWallpaper RPC deadline exceeded")
        self.options_failure = self.options_read_failure = self.close_failure = self.restore_failure = None

    def is_process_running(self, identity):
        if self.identity_failure:
            raise self.identity_failure
        return identity in self.live

    def get_options(self):
        if self.options_read_failure:
            raise self.options_read_failure
        return self.options

    def get_wallpapers(self):
        self.reads += 1
        return list(self.items if self.reported is None else self.reported)

    def set_wallpaper(self, wallpaper, request_settled=None, request_dispatching=None):
        if request_dispatching:
            if self.dispatch:
                self.dispatch(request_dispatching)
            else:
                if self.identity_failure:
                    raise self.identity_failure
                request_dispatching(self.running)
        self.set_calls += 1
        self.items = [x for x in self.items if x.Monitor != wallpaper.Monitor]
        if self.fail_pending:
            self.fail_pending = False
            self.pending = wallpaper
            raise self.pending_failure
        if self.fail_next:
            self.fail_next = False
            if request_settled:
                request_settled()
            raise OSError("player failed")
        if not self.ignore_restored or wallpaper.Path == "amadeus":
            self.items.append(wallpaper)
        if request_settled:
            request_settled()
        if self.fail_after:
            self.fail_after = False
            if self.on_confirmation:
                self.on_confirmation()
            raise self.mount_failure

    def complete_pending(self):
        self.items = [x for x in self.items if x.Monitor != self.pending.Monitor] + [self.pending]
        self.pending = None

    def close_project(self, project):
        self.closed.append(project)
        if self.close_failure:
            raise self.close_failure
        if not self.close_noop:
            self.items = [x for x in self.items if not h.same_path(x.Path, project)]

    def set_session_options(self):
        self.options = replace(self.options, Browser=1, DesktopImage=False, LockImage=False)
        if self.options_failure:
            raise self.options_failure

    def restore_options(self, previous, images=True):
        self.restore_calls += 1
        if self.restore_failure:
            raise self.restore_failure
        self.options = replace(self.options, Browser=previous.Browser if self.options.Browser == 1 else self.options.Browser,
                               DesktopImage=previous.DesktopImage if images and not self.options.DesktopImage else self.options.DesktopImage,
                               LockImage=previous.LockImage if images and not self.options.LockImage else self.options.LockImage)

    def stop_owned_process(self, process):
        self.stopped = process


@pytest.fixture
def f(tmp_path):
    host = FakeHost()
    session = h.Session(host, tmp_path / "session.json")
    return host, session


def before(host, owned=None):
    return h.Snapshot("amadeus", "primary", host.options, list(host.items), owned)


def original(host):
    host.items = [h.DesktopItem("primary", "old")]


def failure(action):
    with pytest.raises(Exception) as caught:
        action()
    return caught.value


def phase(session, expected):
    assert json.loads(session.journal.read_text())["Phase"] == expected


@pytest.mark.parametrize("arrangement,items", [
    (0, [h.DesktopItem("primary", "old")]),
    (0, [h.DesktopItem("primary", "old"), h.DesktopItem("secondary", "second")]),
    (1, [h.DesktopItem("primary", "span-old")]),
    (2, [h.DesktopItem("primary", "old"), h.DesktopItem("secondary", "old")]),
])
def test_restores_scenes_and_layout_without_stopping_shared_host(f, arrangement, items):
    host, session = f
    host.options = replace(host.options, Arrangement=arrangement)
    host.items = list(items)
    session.mount(before(host))
    if arrangement == 0 and len(items) == 2:
        assert items[1] in host.items
    session.restore()
    assert set(host.items) == set(items)
    assert host.stopped is None and not session.journal.exists()


def test_static_wallpaper_revealed_and_owned_host_released(f):
    host, session = f
    owned = h.ProcessIdentity(10, 20)
    session.mount(before(host, owned))
    session.restore()
    assert host.items == [] and host.stopped == owned
    assert not session.journal.exists()


@pytest.mark.parametrize("choice", ["user-new", None, "layout"])
def test_user_choices_win(f, choice):
    host, session = f
    original(host)
    session.mount(before(host, h.ProcessIdentity(10, 20) if choice is not None else None))
    if choice == "layout":
        host.options = replace(host.options, Arrangement=1)
    elif choice is None:
        host.items.clear()
    else:
        host.set_wallpaper(h.DesktopItem("primary", choice))
    session.restore()
    assert [x.Path for x in host.items] == ([choice] if choice == "user-new" else [])
    assert host.stopped is None and not session.journal.exists()
    if choice == "layout":
        assert host.options.Arrangement == 1


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("after", [False, True])
def test_settled_mount_failure_rolls_back_and_allows_next_mount(f, empty, after):
    host, session = f
    if not empty:
        original(host)
    snapshot = before(host, h.ProcessIdentity(10, 20))
    host.fail_after, host.fail_next = after, not after
    error = failure(lambda: session.mount(snapshot))
    assert error is host.mount_failure if after else isinstance(error, OSError)
    assert host.items == snapshot.Wallpapers and not session.journal.exists()
    assert host.closed == ["amadeus"]
    session.mount(before(host))
    session.restore()
    assert not session.journal.exists()


def test_crash_recovery_uses_new_session(f):
    host, session = f
    original(host)
    session.mount(before(host))
    h.Session(host, session.journal).restore()
    assert host.items == [h.DesktopItem("primary", "old")]
    assert not session.journal.exists()


@pytest.mark.parametrize("kind", ["set", "options", "close", "noop", "missing"])
def test_restore_failure_keeps_journal_and_retries(f, kind):
    host, session = f
    if kind != "noop":
        original(host)
    session.mount(before(host))
    if kind == "set":
        host.fail_next = True
    elif kind == "options":
        host.restore_failure = OSError("restore options unavailable")
    elif kind == "close":
        host.close_failure = OSError("close unavailable")
    elif kind == "noop":
        host.close_noop = True
    else:
        host.ignore_restored = True
    error = failure(session.restore)
    assert isinstance(error, OSError) and session.journal.exists() and host.stopped is None
    host.close_failure = host.restore_failure = None
    host.close_noop = host.ignore_restored = False
    h.Session(host, session.journal).restore()
    assert host.items == ([] if kind == "noop" else [h.DesktopItem("primary", "old")]) and not session.journal.exists()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_partial_settings_failure_recovers_settings_only(f, cleanup_fails):
    host, session = f
    host.options = h.HostOptions(0, 0, True, True)
    original(host)
    snapshot = before(host, h.ProcessIdentity(10, 20))
    host.options_failure = OSError("partial settings failure")
    host.restore_failure = OSError("settings rollback failed") if cleanup_fails else None
    error = failure(lambda: session.mount(snapshot))
    if cleanup_fails:
        assert error.exceptions == (host.options_failure, host.restore_failure)
        phase(session, "Preparing")
        host.restore_failure = None
        h.Session(host, session.journal).restore()
    else:
        assert error is host.options_failure
    assert host.options == snapshot.Options and host.items == snapshot.Wallpapers
    assert host.set_calls == 0 and host.closed == [] and not session.journal.exists()


def test_mount_cleanup_dual_errors_and_user_choice_on_retry(f):
    host, session = f
    original(host)
    host.fail_after = True
    host.close_failure = OSError("close unavailable")
    error = failure(lambda: session.mount(before(host, h.ProcessIdentity(10, 20))))
    assert error.exceptions == (host.mount_failure, host.close_failure)
    assert "close unavailable" in h.error_detail(error) and "timed out" in h.error_detail(error)
    phase(session, "Settled")
    host.close_failure = None
    host.set_wallpaper(h.DesktopItem("primary", "user-new"))
    h.Session(host, session.journal).restore()
    assert host.items == [h.DesktopItem("primary", "user-new")]
    assert host.stopped is None and not session.journal.exists()


def test_mount_and_options_cleanup_errors_retry_original_scene(f):
    host, session = f
    original(host)
    host.fail_after = True
    host.restore_failure = OSError("restoration unavailable")
    error = failure(lambda: session.mount(before(host)))
    assert error.exceptions == (host.mount_failure, host.restore_failure)
    phase(session, "Settled")
    host.restore_failure = None
    h.Session(host, session.journal).restore()
    assert host.items == [h.DesktopItem("primary", "old")] and not session.journal.exists()


@pytest.mark.parametrize("wrong_monitor", [False, True])
def test_original_project_is_valid_only_on_original_monitor(f, wrong_monitor):
    host, session = f
    host.items = [h.DesktopItem("primary", "amadeus")]
    session.mount(before(host))
    if wrong_monitor:
        host.reported = [h.DesktopItem("secondary", "amadeus")]
        assert isinstance(failure(session.restore), OSError) and session.journal.exists()
    else:
        session.restore()
        assert host.items == [h.DesktopItem("primary", "amadeus")] and not session.journal.exists()


@pytest.mark.parametrize("restart", ["owned", "manual", "reused-pid"])
def test_pending_late_mount_requires_original_receiver_exit(f, restart):
    host, session = f
    original(host)
    owned = h.ProcessIdentity(10, 20) if restart == "owned" else None
    host.fail_pending = True
    assert failure(lambda: session.mount(before(host, owned))) is host.pending_failure
    phase(session, "Pending")
    saved = json.loads(session.journal.read_text())
    assert saved["RequestProcess"] == asdict(host.running)
    assert saved["OwnedProcess"] == (asdict(owned) if owned else None)
    reads = host.reads
    for identity in (None, owned, h.ProcessIdentity(11, 30)):
        assert isinstance(failure(lambda: h.Session(host, session.journal).restore(identity)), RuntimeError)
    assert host.reads == reads and host.restore_calls == 0 and host.closed == [] and host.stopped is None
    host.complete_pending()
    assert host.items == [h.DesktopItem("primary", "amadeus")]
    assert isinstance(failure(session.restore), RuntimeError)
    host.items.clear()
    host.live.clear()
    host.running = h.ProcessIdentity(host.running.Id, host.running.Started + 1) if restart == "reused-pid" else h.ProcessIdentity(11, 30)
    host.live.add(host.running)
    h.Session(host, session.journal).restore(host.running if owned else None)
    assert host.items == [h.DesktopItem("primary", "old")] and not session.journal.exists()
    assert host.stopped == (host.running if owned else None)


def test_pending_identity_failures_preserve_original_error_and_fence(f):
    host, session = f
    host.identity_failure = OSError("identity unavailable")
    assert failure(lambda: session.mount(before(host))) is host.identity_failure
    assert host.set_calls == 0 and host.restore_calls == 1 and not session.journal.exists()
    host.identity_failure = None
    host.fail_pending = True
    failure(lambda: session.mount(before(host)))
    content = session.journal.read_bytes()
    host.identity_failure = OSError("liveness unavailable")
    assert failure(lambda: session.restore(h.ProcessIdentity(11, 30))) is host.identity_failure
    assert session.journal.read_bytes() == content and host.stopped is None and host.closed == []


@pytest.mark.parametrize("user_choice", [False, True])
def test_pending_receiver_exit_allows_retry_but_is_not_restore_proof(f, user_choice):
    host, session = f
    original(host)
    host.fail_pending = True
    failure(lambda: session.mount(before(host, host.running)))
    host.live.clear()
    if user_choice:
        host.items = [h.DesktopItem("primary", "user-new")]
    host.restore_failure = OSError("recovery unavailable")
    assert failure(session.restore) is host.restore_failure and session.journal.exists()
    host.restore_failure = None
    h.Session(host, session.journal).restore()
    assert host.items == [h.DesktopItem("primary", "user-new" if user_choice else "old")]
    assert not session.journal.exists()
    if user_choice:
        assert host.stopped is None


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("invalid", ["missing", "integer-phase", "bad-identity"])
def test_invalid_pending_journal_is_untouched_without_host_calls(f, running, invalid):
    host, session = f
    original(host)
    saved = asdict(before(host)) | {"Phase": "Pending"}
    if invalid == "integer-phase":
        saved["Phase"] = 1
    elif invalid == "bad-identity":
        saved["RequestProcess"] = {"Id": 100, "Started": "200"}
    session.journal.write_text(json.dumps(saved))
    content = session.journal.read_bytes()
    if not running:
        host.live.clear()
    assert isinstance(failure(session.restore), ValueError)
    assert session.journal.read_bytes() == content
    assert host.set_calls == host.restore_calls == host.reads == 0 and host.closed == [] and host.stopped is None


def test_legacy_pascalcase_journals_keep_unmounted_and_mounted_semantics(f):
    host, session = f
    original(host)
    saved = asdict(before(host))
    for key in ("Phase", "RequestProcess"):
        saved.pop(key)
    session.journal.write_text(json.dumps(saved))
    host.items.clear()
    session.restore()
    assert host.items == [h.DesktopItem("primary", "old")] and not session.journal.exists()
    saved["Mounted"] = True
    session.journal.write_text(json.dumps(saved))
    host.items = [h.DesktopItem("primary", "user-new")]
    session.restore()
    assert host.items == [h.DesktopItem("primary", "user-new")] and not session.journal.exists()


def test_mounted_body_does_not_run_or_repeat_mount_failure_cleanup(f):
    host, session = f
    original(host)
    host.fail_next = True
    def unexpected():
        pytest.fail("failed mount entered body")
    assert isinstance(failure(lambda: session.run_mounted(before(host), unexpected)), OSError)
    assert host.closed == ["amadeus"] and host.restore_calls == 2 and not session.journal.exists()


def test_body_and_cleanup_errors_preserved_and_retryable(f):
    host, session = f
    original(host)
    body_error = OSError("mounted body failed")
    host.restore_failure = OSError("mounted cleanup failed")
    def body():
        raise body_error
    error = failure(lambda: session.run_mounted(before(host), body))
    assert error.exceptions == (body_error, host.restore_failure)
    assert host.closed == ["amadeus"] and host.restore_calls == 1 and session.journal.exists()
    host.restore_failure = None
    h.Session(host, session.journal).restore()
    assert host.items == [h.DesktopItem("primary", "old")] and not session.journal.exists()


@pytest.mark.parametrize("stage", ["mount", "body", "recovery"])
def test_outer_cleanup_does_not_stop_host_kept_for_user_choice(f, stage):
    host, session = f
    owned = h.ProcessIdentity(10, 20)
    def user_choice():
        host.items = [h.DesktopItem("primary", "user-new")]
    if stage == "recovery":
        session.save(replace(before(host, h.ProcessIdentity(9, 10)), Mounted=True))
        user_choice()
        session.restore(owned)
        host.options_read_failure = OSError("snapshot failed")
        failure(lambda: host.get_options())
    else:
        def body():
            user_choice()
            raise OSError("body failed")
        if stage == "mount":
            host.fail_after, host.on_confirmation = True, user_choice
        failure(lambda: session.run_mounted(before(host, owned), body))
    session.release_unstarted_host(owned)
    assert host.items == [h.DesktopItem("primary", "user-new")]
    assert host.stopped is None and not session.journal.exists()


def test_initial_journal_failure_releases_only_unstarted_host(f, tmp_path):
    host, _ = f
    blocked = tmp_path / "file"
    blocked.write_text("obstruction")
    session = h.Session(host, blocked / "session.json")
    owned = h.ProcessIdentity(10, 20)
    assert isinstance(failure(lambda: session.mount(before(host, owned))), OSError)
    session.release_unstarted_host(owned)
    assert host.stopped == owned and host.set_calls == host.restore_calls == 0


def test_existing_pending_journal_blocks_outer_cleanup_and_new_mount(f):
    host, session = f
    owned = h.ProcessIdentity(10, 20)
    session.save(replace(before(host, owned), Phase="Pending", RequestProcess=host.running))
    assert isinstance(failure(lambda: session.mount(before(host, owned))), RuntimeError)
    session.release_unstarted_host(owned)
    assert session.journal.exists() and host.stopped is None and host.set_calls == host.restore_calls == 0


def test_new_identity_can_be_released_after_old_recovery(f):
    host, session = f
    recovered = h.ProcessIdentity(10, 20)
    session.save(before(host, h.ProcessIdentity(9, 10)))
    session.restore(recovered)
    assert host.stopped == recovered
    newly_started = h.ProcessIdentity(10, 21)
    session.release_unstarted_host(newly_started)
    assert host.stopped == newly_started


def test_first_run_preparation_is_idempotent_and_preserves_existing_settings(tmp_path):
    assert h.LivelyHost.prepare_first_run(tmp_path)
    settings = json.loads((tmp_path / "Settings.json").read_text())
    assert not settings["IsFirstRun"] and not settings["Startup"]
    (tmp_path / "Settings.json").write_text("user-owned-settings")
    assert not h.LivelyHost.prepare_first_run(tmp_path)
    assert (tmp_path / "Settings.json").read_text() == "user-owned-settings"


def test_launch_image_backup_recovers_old_pascalcase_backup_and_unknown_json(tmp_path):
    class OfflineWindows:
        def running(self):
            return None
    host = h.LivelyHost(OfflineWindows(), tmp_path, object())
    settings = dict(DesktopAutoWallpaper=True, LockScreenAutoWallpaper=True, Future={"values": [1, "future"]})
    h.atomic_json(tmp_path / "Settings.json", settings)
    host.prepare_launch_images()
    assert json.loads(host.launch_backup.read_text()) == dict(Desktop=True, Lock=True)
    current = json.loads((tmp_path / "Settings.json").read_text())
    assert current == settings | dict(DesktopAutoWallpaper=False, LockScreenAutoWallpaper=False)
    host.prepare_launch_images()  # Interrupted preparation must not overwrite the backup.
    host.recover_launch_images()
    assert json.loads((tmp_path / "Settings.json").read_text()) == settings
    assert not host.launch_backup.exists()


def test_broken_parent_stdout_does_not_change_helper_exit_status():
    code = "import os, runpy, sys; helper = runpy.run_path(sys.argv[1]); os.read(sys.stdin.fileno(), 1); helper['report'](dict(status='restored'))"
    child = subprocess.Popen([sys.executable, "-I", "-c", code, str(HELPER)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child.stdout.close()
    child.stdin.write(b"x")
    child.stdin.close()
    assert child.wait(timeout=15) == 0, child.stderr.read().decode("utf-8", errors="replace")
    assert child.stderr.read() == b""


def test_settings_rewrite_preserves_unknown_bytes_order_and_defaults():
    # Noncanonical varint, duplicate scalar, fixed32/64, group, bytes, wrong-wire
    # occurrence of a known field: every unowned byte must survive unchanged.
    unknown = [b"\x18\x81\x00", h.blob(70, b"future\x00value"), b"\x7d1234", b"\x79abcdefgh",
               b"\x53\x08\x01\x54", h.blob(28, b"unknown-wire"), h.scalar(16, 2)]
    source = unknown[0] + h.scalar(28, 3) + b"".join(unknown[1:4]) + h.scalar(48, 1) + b"".join(unknown[4:]) + h.scalar(49, 1) + h.scalar(28, 2)
    output = h.rewrite_settings(source, {28: 1, 48: 0, 49: 0})
    unowned = [x.raw for x in h.fields(output) if not (x.wire == 0 and x.number in {28, 48, 49})]
    assert unowned == unknown
    assert [(x.number, x.value) for x in h.fields(output) if x.wire == 0 and x.number in {28, 48, 49}] == [(28, 1)]
    assert h.rewrite_settings(b"\x18\x81\x00", {48: 0, 49: 0}) == b"\x18\x81\x00"
    assert failure(lambda: h.rewrite_settings(source, {16: 0})).args


@pytest.mark.parametrize("browser,desktop,lock", [(0, False, False), (2, True, True), (1, False, True)])
@pytest.mark.parametrize("previous_images", [False, True])
def test_settings_restore_preserves_newer_user_choices(browser, desktop, lock, previous_images):
    raw = h.scalar(28, browser) + h.scalar(49, int(desktop)) + h.scalar(48, int(lock)) + b"\x18\x81\x00"
    class SettingsRpc:
        def call(self, service, method, payload=b"", **kwargs):
            assert service == "Settings"
            if method == "GetSettings":
                return [raw]
            self.result = payload
            return [b""]
    rpc = SettingsRpc()
    host = h.LivelyHost(None, Path("unused"), rpc)
    host.restore_options(h.HostOptions(0, 0, previous_images, previous_images))
    result = getattr(rpc, "result", raw)
    assert h.value(result, 28) == (0 if browser == 1 else browser)
    assert bool(h.value(result, 48)) == (lock or previous_images)
    assert bool(h.value(result, 49)) == (desktop or previous_images)
    assert b"\x18\x81\x00" in result


@pytest.mark.parametrize("data", [b"\x00", b"\x08\x80", b"\x0a\x03ab", b"\x0c", b"\x0b\x14", b"\x09abc"])
def test_malformed_wire_is_observable(data):
    assert isinstance(failure(lambda: h.fields(data)), ValueError)


class MemoryPipe:
    def __init__(self, packets):
        self.packets = iter(packets)
        self.writes, self.closed = [], False
    def receiver(self):
        return h.ProcessIdentity(100, 200)
    def write(self, packet, budget):
        budget.check()
        self.writes.append(packet)
    def read(self, budget):
        budget.check()
        return next(self.packets)
    def close(self):
        self.closed = True


def rpc_for(pipe):
    return h.Rpc(None, "synthetic", lambda *_: pipe)


def test_transport_inline_separate_stream_and_trailers():
    # Literal wire vectors are independent of the production envelope encoder.
    pipe = MemoryPipe([b"\x02\x12\x00\x06\x1a\x04\x08\x03\x10\x01abc\x04\x1a\x02\x08\x04", b"data", b"\x02\x2a\x00"])
    assert rpc_for(pipe).call("Desktop", "GetWallpapers", streaming=True) == [b"abc", b"data"]
    assert pipe.closed
    request = pipe.writes[0]
    assert b"/Lively.Grpc.Common.Proto.Desktop.DesktopService/GetWallpapers" in request


@pytest.mark.parametrize("code,detail", [(7, "permission denied"), (13, "original failure")])
def test_rpc_retains_original_status_and_detail(code, detail):
    pipe = MemoryPipe([h.transport_message(5, h.scalar(2, code) + h.string(3, detail))])
    error = failure(lambda: rpc_for(pipe).call("Desktop", "SetWallpaper"))
    assert isinstance(error, h.RpcError) and (error.code, error.detail) == (code, detail)
    assert pipe.closed


@pytest.mark.parametrize("packets", [[b"\x02\x2a\x00"], [b"\x04\x1a\x02\x08"], [b"\x06\x1a\x04\x08\x05\x10\x01ab"]])
def test_rpc_never_accepts_missing_or_truncated_unary_payload(packets):
    pipe = MemoryPipe(packets)
    assert isinstance(failure(lambda: rpc_for(pipe).call("Desktop", "SetWallpaper")), ValueError)
    assert pipe.closed


@pytest.mark.parametrize("stage", ["identity", "journal", "cancel", "deadline"])
def test_pre_dispatch_failure_sends_no_bytes_and_preserves_cause(stage):
    pipe = MemoryPipe([])
    primary = OSError("synthetic identity or persistence failure")
    cancel = threading.Event()
    def dispatching(_):
        if stage in ("identity", "journal"):
            raise primary
        if stage == "cancel":
            cancel.set()
        else:
            time.sleep(0.03)
    if stage == "identity":
        pipe.receiver = lambda: (_ for _ in ()).throw(primary)
    error = failure(lambda: rpc_for(pipe).call("Desktop", "SetWallpaper", dispatching=dispatching,
                                              timeout=0.01 if stage == "deadline" else 1, cancel=cancel))
    assert error is primary if stage in ("identity", "journal") else isinstance(error, h.RpcError)
    assert pipe.writes == [] and pipe.closed


@pytest.mark.parametrize("cancelled", [False, True])
def test_expired_calls_do_not_identify_persist_or_write(cancelled):
    pipe = MemoryPipe([])
    cancel = threading.Event()
    if cancelled:
        cancel.set()
    error = failure(lambda: rpc_for(pipe).call("Desktop", "SetWallpaper", timeout=1 if cancelled else 0,
                    cancel=cancel, dispatching=lambda _: pytest.fail("ended request entered persistence")))
    assert error.code == (1 if cancelled else 4) and not pipe.writes and pipe.closed


@pytest.mark.parametrize("cancelled", [False, True])
def test_deadline_waits_for_entered_persistence_and_retains_pending(f, cancelled):
    host, session = f
    pipe = MemoryPipe([])
    entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def dispatch(dispatching):
        def persist(identity):
            entered.set()
            assert release.wait(3)
            dispatching(identity)
        rpc_for(pipe).call("Desktop", "SetWallpaper", timeout=2 if cancelled else 0.1, cancel=cancel, dispatching=persist)
    host.dispatch = dispatch
    thread = threading.Thread(target=lambda: errors.append(failure(lambda: session.mount(before(host)))))
    thread.start()
    try:
        assert entered.wait(2)
        if cancelled:
            cancel.set()
        else:
            time.sleep(0.15)
        assert thread.is_alive() and host.restore_calls == 0
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and errors[0].code == (1 if cancelled else 4)
    phase(session, "Pending")
    assert host.restore_calls == 0 and pipe.writes == []


native = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows process/message-pipe contract")


@native
def test_real_process_identity_matches_csharp_and_fences_old_pending_journal(f):
    windows = h.Windows()
    identity = windows.identity(os.getpid())
    # Compile the same C# expression used by the old helper, against this Python
    # process. No float timestamp, .NET SDK, NuGet, or test-double identity.
    code = 'public static class ProcessTicks { public static long Read(int id) { using (var p = System.Diagnostics.Process.GetProcessById(id)) { return p.StartTime.ToUniversalTime().Ticks; } } }'
    script = "Add-Type -TypeDefinition '" + code + "'; [ProcessTicks]::Read(" + str(os.getpid()) + ")"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, check=True, timeout=30)
    assert identity.Started == int(result.stdout.strip())
    assert windows.is_running(identity) and not windows.is_running(replace(identity, Started=identity.Started + 1))
    host, session = f
    host.is_process_running = windows.is_running
    session.save(replace(before(host), Phase="Pending", RequestProcess=identity))
    content = session.journal.read_bytes()
    assert isinstance(failure(session.prepare_recovery), RuntimeError)
    assert session.journal.read_bytes() == content and host.restore_calls == host.set_calls == host.reads == 0


@native
def test_cross_process_journal_lock_is_exclusive(tmp_path):
    windows = h.Windows()
    lock = tmp_path / "session.lock"
    handle = windows.lock(lock)
    try:
        assert isinstance(failure(lambda: windows.lock(lock)), OSError)
    finally:
        windows.close(handle)
    windows.close(windows.lock(lock))


class NativeServer:
    """One synthetic message-mode pipe, bounded overlapped connect and IO."""
    def __init__(self, action, name=None):
        import ctypes
        from ctypes import wintypes as w
        self.windows = h.Windows()
        self.name = name or "amadeus-wallpaper-contract-" + uuid.uuid4().hex
        api = self.windows.api
        api.CreateNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.LPVOID]
        api.CreateNamedPipeW.restype = w.HANDLE
        api.ConnectNamedPipe.argtypes = [w.HANDLE, ctypes.POINTER(h.OVERLAPPED)]
        api.ConnectNamedPipe.restype = w.BOOL
        self.handle = api.CreateNamedPipeW("\\\\.\\pipe\\" + self.name, 0x40000003, 6, 1, 65536, 65536, 0, None)
        assert self.handle != ctypes.c_void_p(-1).value
        self.errors, self.connected, self.stop = [], threading.Event(), threading.Event()
        def run():
            event = self.windows.check(api.CreateEventW(None, True, False, None))
            overlap = h.OVERLAPPED(hEvent=event)
            count = w.DWORD()
            try:
                if not api.ConnectNamedPipe(self.handle, ctypes.byref(overlap)):
                    error = ctypes.get_last_error()
                    if error == 997:
                        until = time.monotonic() + 4
                        while not self.stop.is_set() and time.monotonic() < until:
                            if api.WaitForSingleObject(event, 20) == 0:
                                break
                        else:
                            api.CancelIoEx(self.handle, ctypes.byref(overlap))
                            api.GetOverlappedResult(self.handle, ctypes.byref(overlap), ctypes.byref(count), True)
                            if self.stop.is_set():
                                return
                            raise TimeoutError("synthetic server connect")
                        self.windows.check(api.GetOverlappedResult(self.handle, ctypes.byref(overlap), ctypes.byref(count), False))
                    elif error != 535:
                        raise ctypes.WinError(error)
                self.connected.set()
                pipe = h.Pipe.__new__(h.Pipe)
                pipe.windows, pipe.handle = self.windows, self.handle
                action(pipe)
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.windows.close(event)
                self.windows.close(self.handle)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self):
        self.thread.join(6)
        assert not self.thread.is_alive()
        if self.errors:
            raise self.errors[0]


@native
def test_real_set_pipe_persists_actual_receiver_before_any_request_bytes(f):
    host, session = f
    calls = []
    def receive(pipe):
        packet = pipe.read(h.Budget(3))
        saved = session.read()
        assert saved.RequestProcess == pipe.windows.identity(os.getpid()) and saved.Phase == "Pending"
        assert b"SetWallpaper" in packet and b"synthetic" in packet
        calls.append(packet)
        pipe.write(b"\x04\x1a\x02\x10\x01\x02\x2a\x00", h.Budget(3))
    server = NativeServer(receive)
    rpc = h.Rpc(server.windows, server.name)
    host.dispatch = lambda callback: rpc.call("Desktop", "SetWallpaper", h.string(1, "synthetic"), dispatching=callback)
    try:
        session.mount(before(host))
    finally:
        server.join()
    assert len(calls) == 1
    session.restore()
    assert not session.journal.exists()


@native
@pytest.mark.parametrize("stage", ["identity", "journal"])
def test_real_set_pipe_identity_or_persistence_failure_sends_zero_bytes(f, stage, monkeypatch):
    host, session = f
    received = []
    def receive(pipe):
        try:
            received.append(pipe.read(h.Budget(2)))
        except (OSError, EOFError):
            pass
    server = NativeServer(receive)
    primary = OSError("original identity failure")
    if stage == "identity":
        monkeypatch.setattr(server.windows, "receiver", lambda _: (_ for _ in ()).throw(primary))
    rpc = h.Rpc(server.windows, server.name)
    def dispatch(callback):
        if stage == "journal":
            session.journal.with_name(session.journal.name + ".tmp").mkdir()
        rpc.call("Desktop", "SetWallpaper", dispatching=callback)
    host.dispatch = dispatch
    try:
        error = failure(lambda: session.mount(before(host)))
        assert error is primary if stage == "identity" else isinstance(error, PermissionError)
    finally:
        server.join()
    assert received == [] and host.restore_calls == 1 and not session.journal.exists()


@native
@pytest.mark.parametrize("cancelled", [False, True])
def test_real_pipe_after_dispatch_deadline_or_cancel_keeps_pending_and_sends_cancel(f, cancelled):
    host, session = f
    received, sent, cancel = [], threading.Event(), threading.Event()
    def receive(pipe):
        received.append(pipe.read(h.Budget(3)))
        sent.set()
        if cancelled:
            cancel.set()
        received.append(pipe.read(h.Budget(3)))
    server = NativeServer(receive)
    rpc = h.Rpc(server.windows, server.name)
    host.dispatch = lambda callback: rpc.call("Desktop", "SetWallpaper", dispatching=callback,
                                              timeout=2 if cancelled else 0.15, cancel=cancel)
    try:
        error = failure(lambda: session.mount(before(host)))
    finally:
        server.join()
    assert sent.is_set() and error.code == (1 if cancelled else 4)
    assert received[1] == b"\x02\x20\x01"
    phase(session, "Pending")
    assert host.restore_calls == 0


@native
def test_real_connection_deadline_cannot_later_identify_or_persist_or_send(f):
    host, session = f
    name = "amadeus-wallpaper-absent-" + uuid.uuid4().hex
    rpc = h.Rpc(h.Windows(), name)
    calls = []
    host.dispatch = lambda callback: rpc.call("Desktop", "SetWallpaper", timeout=0.05,
                                              dispatching=lambda identity: (calls.append(identity), callback(identity)))
    assert failure(lambda: session.mount(before(host))).code == 4
    assert host.restore_calls == 1 and calls == [] and not session.journal.exists()
    server = NativeServer(lambda _: pytest.fail("ended request connected to a late server"), name)
    try:
        assert not server.connected.wait(0.15)
        assert calls == [] and not session.journal.exists()
    finally:
        server.stop.set()
        server.join()


@native
def test_real_pipe_deadline_waits_for_entered_durable_boundary(f):
    host, session = f
    entered, release = threading.Event(), threading.Event()
    received, errors = [], []
    def receive(pipe):
        try:
            received.append(pipe.read(h.Budget(3)))
        except (OSError, EOFError):
            pass
    server = NativeServer(receive)
    rpc = h.Rpc(server.windows, server.name)
    def dispatch(callback):
        def persist(identity):
            entered.set()
            assert release.wait(3)
            callback(identity)
        rpc.call("Desktop", "SetWallpaper", dispatching=persist, timeout=0.1)
    host.dispatch = dispatch
    thread = threading.Thread(target=lambda: errors.append(failure(lambda: session.mount(before(host)))))
    thread.start()
    try:
        assert entered.wait(2)
        time.sleep(0.15)
        assert thread.is_alive() and host.restore_calls == 0
    finally:
        release.set()
        thread.join(3)
        server.join()
    assert not thread.is_alive() and errors[0].code == 4 and received == []
    phase(session, "Pending")
    assert host.restore_calls == 0


@native
def test_native_large_message_and_separate_payload_are_read_completely():
    payload = bytes(range(256)) * 350
    def receive(pipe):
        pipe.read(h.Budget(3))
        pipe.write(h.transport_message(3, h.scalar(1, len(payload))), h.Budget(3))
        pipe.write(payload, h.Budget(3))
        pipe.write(b"\x02\x2a\x00", h.Budget(3))
    server = NativeServer(receive)
    try:
        assert h.Rpc(server.windows, server.name).call("Settings", "GetSettings") == [payload]
    finally:
        server.join()
