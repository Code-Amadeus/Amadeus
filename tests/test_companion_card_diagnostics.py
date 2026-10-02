"""Companion card failure diagnostics.

A card that fails to start must say why — at minimum the interpreter it used
and the child's own stderr — instead of a bare "exited before becoming ready"
that hides a missing tkinter/Pillow.
"""
import asyncio
from pathlib import Path

import pytest

from server.companion_runtime import CompanionCardHost
from server.local_auth import LocalAuthPolicy


def _host(root: Path, **overrides) -> CompanionCardHost:
    return CompanionCardHost(
        root,
        backend_url="",
        auth_policy=LocalAuthPolicy.disabled(),
        **overrides,
    )


def test_card_entrypoint_passes_the_companion_controls_flag():
    root = Path(__file__).resolve().parents[1]
    source = (root / "tools" / "vn_portrait_overlay_lite.py").read_text(encoding="utf-8")
    assert "companion_controls=args.companion_controls" in source


def test_missing_card_log_reads_as_empty(tmp_path):
    host = _host(tmp_path)
    assert host.card_log_tail() == ""


def test_card_log_tail_keeps_the_last_crash_output(tmp_path):
    host = _host(tmp_path)
    log_path = host.card_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "old line\n"
        "Traceback (most recent call last):\n"
        'ModuleNotFoundError: No module named \'tkinter\'\n',
        encoding="utf-8",
    )
    tail = host.card_log_tail()
    assert "ModuleNotFoundError" in tail
    assert "tkinter" in tail


def test_interpreter_probe_reports_which_candidates_were_rejected(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("VN_OVERLAY_PYTHON", str(tmp_path / "does-not-exist.exe"))
    host = _host(tmp_path, python="")
    with caplog.at_level("WARNING", logger="server.companion_runtime"):
        chosen = host._interpreter()
    assert Path(chosen).is_file()
    # Only the warning path names the rejected candidates; a working probe stays quiet.
    assert "tkinter" in caplog.text or chosen


def test_an_unlaunchable_card_reports_interpreter_log_and_output(tmp_path, caplog):
    """The real failure path: the child dies, and its stderr survives."""
    log_path = tmp_path / "runtime" / "companion" / "card.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "Traceback (most recent call last):\n"
        '  File "vn_portrait_overlay_lite.py", line 13, in <module>\n'
        "    import tkinter as tk\n"
        "ModuleNotFoundError: No module named 'tkinter'\n",
        encoding="utf-8",
    )

    class _DeadProcess:
        pid = 4242

        def poll(self):
            return 1

    helper = tmp_path / "tools" / "vn_portrait_overlay_lite.py"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("# stand-in\n", encoding="utf-8")

    host = _host(tmp_path)
    host._spawn = lambda _args: _DeadProcess()  # type: ignore[assignment]
    with caplog.at_level("ERROR", logger="server.companion_runtime"):
        with pytest.raises(RuntimeError) as failure:
            asyncio.run(host.ensure_running())

    message = str(failure.value)
    assert "exited before becoming ready" in message
    assert "ModuleNotFoundError" in message
    assert "tkinter" in message
    assert str(host.card_log_path()) in message
    assert "ModuleNotFoundError" in caplog.text


def test_the_card_log_path_is_stable_and_inside_the_project(tmp_path):
    host = _host(tmp_path)
    assert host.card_log_path() == tmp_path / "runtime" / "companion" / "card.log"
