"""Atomic publication contracts, without application settings or model imports."""
from contextlib import contextmanager
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import durable_io


def fail(*args, **kwargs):
    raise OSError("synthetic I/O failure")


def test_utf8_and_binary_roundtrip_without_newline_translation(tmp_path):
    path = tmp_path / "state"
    text = json.dumps({"name": "角色 🌟"}, ensure_ascii=False, indent=2) + "\n"
    durable_io.write_text(path, text)
    assert path.read_bytes() == text.encode("utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "角色 🌟"
    durable_io.write_bytes(path, b"\x00\xff\r\n")
    assert path.read_bytes() == b"\x00\xff\r\n"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("stage", ["fsync", "replace"])
def test_precommit_failure_preserves_old_bytes_and_cleans_temporary(tmp_path, monkeypatch, stage):
    path = tmp_path / "state"
    path.write_bytes(b"previous")
    monkeypatch.setattr(durable_io.os, stage, fail)
    with pytest.raises(OSError):
        durable_io.write_bytes(path, b"next")
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


def test_partial_temporary_write_never_truncates_destination(tmp_path, monkeypatch):
    path = tmp_path / "state"
    path.write_bytes(b"previous")
    fdopen = durable_io.os.fdopen

    @contextmanager
    def partial_writer(*args, **kwargs):
        with fdopen(*args, **kwargs) as stream:
            class Partial:
                def write(self, data):
                    stream.write(data[:2])
                    raise OSError("synthetic partial write")
            yield Partial()

    monkeypatch.setattr(durable_io.os, "fdopen", partial_writer)
    with pytest.raises(OSError, match="partial write"):
        durable_io.write_bytes(path, b"replacement")
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("succeeds", [True, False])
def test_windows_sharing_retry_is_bounded(tmp_path, monkeypatch, succeeds):
    path = tmp_path / "state"
    path.write_bytes(b"previous")
    replace = durable_io.os.replace
    attempts, sleeps = [], []

    def sharing(source, destination):
        attempts.append(source)
        if succeeds and len(attempts) == 3:
            return replace(source, destination)
        error = PermissionError("synthetic sharing violation")
        error.winerror = 32
        raise error

    monkeypatch.setattr(durable_io, "_WINDOWS", True)
    monkeypatch.setattr(durable_io.os, "replace", sharing)
    monkeypatch.setattr(durable_io.time, "sleep", sleeps.append)
    if succeeds:
        durable_io.write_bytes(path, b"next")
        assert path.read_bytes() == b"next"
        assert len(attempts) == 3
    else:
        with pytest.raises(PermissionError):
            durable_io.write_bytes(path, b"next")
        assert path.read_bytes() == b"previous"
        assert len(attempts) == len(durable_io._RETRY_DELAYS) + 1
    assert len(sleeps) == len(attempts) - 1
    assert len(set(attempts)) == 1
    assert list(tmp_path.iterdir()) == [path]


def test_other_permission_errors_are_not_retried(tmp_path, monkeypatch):
    attempts = []
    def denied(*args):
        attempts.append(True)
        raise PermissionError("ordinary denial")
    monkeypatch.setattr(durable_io.os, "replace", denied)
    monkeypatch.setattr(durable_io.time, "sleep", lambda _: pytest.fail("unexpected retry"))
    with pytest.raises(PermissionError):
        durable_io.write_bytes(tmp_path / "state", b"next")
    assert attempts == [True]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("winerror", [5, 32])
def test_event_loop_sharing_failure_never_sleeps_or_replaces_old_state(tmp_path, monkeypatch, winerror):
    path = tmp_path / "state"
    path.write_bytes(b"previous")
    attempts = []
    def denied(*args):
        attempts.append(True)
        error = PermissionError("synthetic sharing violation")
        error.winerror = winerror
        raise error
    monkeypatch.setattr(durable_io, "_WINDOWS", True)
    monkeypatch.setattr(durable_io.os, "replace", denied)
    monkeypatch.setattr(durable_io.time, "sleep", lambda _: pytest.fail("event-loop backoff"))
    async def save():
        with pytest.raises(PermissionError):
            durable_io.write_bytes(path, b"next")
    asyncio.run(save())
    assert attempts == [True]
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


def test_postcommit_directory_failure_returns_with_new_file_and_warning(tmp_path, monkeypatch, caplog):
    path = tmp_path / "state"
    path.write_bytes(b"previous")
    monkeypatch.setattr(durable_io, "_sync_directory", fail)
    durable_io.write_text(path, "next")
    assert path.read_bytes() == b"next"
    assert "durability unconfirmed" in caplog.text
    assert "synthetic I/O failure" not in caplog.text


def test_directory_sync_follows_replacement(tmp_path, monkeypatch):
    path = tmp_path / "state"
    observed = []
    def directory_sync(directory):
        assert directory == tmp_path
        assert path.read_bytes() == b"next"
        observed.append(True)
    monkeypatch.setattr(durable_io, "_sync_directory", directory_sync)
    durable_io.write_bytes(path, b"next")
    assert observed == [True]


def test_posix_directory_descriptor_is_closed_when_sync_fails(monkeypatch):
    closed = []
    monkeypatch.setattr(durable_io, "_WINDOWS", False)
    monkeypatch.setattr(durable_io, "os", SimpleNamespace(
        O_RDONLY=0, open=lambda *_: 42, fsync=fail, close=closed.append,
    ))
    with pytest.raises(OSError):
        durable_io._sync_directory(Path("synthetic-directory"))
    assert closed == [42]


def test_bad_text_fails_before_creating_a_temporary(tmp_path):
    path = tmp_path / "state"
    path.write_bytes(b"previous")
    with pytest.raises(UnicodeEncodeError):
        durable_io.write_text(path, "\ud800")
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


def test_vts_token_replacement_failure_preserves_authorization(tmp_path, monkeypatch):
    from vts.connection_manager import VTSConnectionManager
    manager = VTSConnectionManager.__new__(VTSConnectionManager)
    manager._token_file = str(tmp_path / "token.json")
    manager._save_auth_token("previous-token")
    monkeypatch.setattr(durable_io.os, "replace", fail)
    manager._save_auth_token("next-token")
    assert manager._load_auth_token() == "previous-token"
    assert list(tmp_path.iterdir()) == [Path(manager._token_file)]
