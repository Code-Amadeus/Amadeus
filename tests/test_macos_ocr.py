"""macOS OCR contracts without reading a user's screen or calling a model."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image
import pytest

from server import macos_capture, visual_runtime
from server.vn_launch_manager import VNLaunchManager
from server.vn_profiles import LaunchProfile
from server.vn_text_sources import MacVisionOCRTextSource, capture_macos_window_preview

WINDOW = {"id": 11, "pid": 42, "title": "Dialogue", "app": "Game", "width": 800, "height": 600}
OTHER = {**WINDOW, "id": 12, "title": "Settings"}


@pytest.mark.parametrize("selectors", [
    {"window_id": 11}, {"pid": 42, "window_name": "Dialogue"},
    {"window_name": "dialogue", "app_name": "game"}, {"app_name": "Game"},
])
def test_unique_window_resolution(selectors):
    assert macos_capture.resolve_macos_window([WINDOW], **selectors) == WINDOW


@pytest.mark.parametrize("windows,selectors", [
    ([OTHER], {"window_id": 11, "window_name": "Settings", "app_name": "Game"}),
    ([{**WINDOW, "pid": 99}], {"window_id": 11, "pid": 42}),
    ([WINDOW, OTHER], {"app_name": "Game"}),
    ([WINDOW, OTHER], {"pid": 42}),
    ([WINDOW], {"window_name": "Dialogue", "app_name": "Unrelated"}),
    ([WINDOW], {}),
])
def test_missing_or_ambiguous_identity_never_falls_back(windows, selectors):
    with pytest.raises(RuntimeError):
        macos_capture.resolve_macos_window(windows, **selectors)


@pytest.mark.parametrize("crop", [
    {}, {"x": 0, "y": 0, "w": 1}, {"x": 0, "y": 0, "w": 1, "h": 1, "extra": 0},
    {"x": -0.1, "y": 0, "w": 1, "h": 1}, {"x": 2, "y": 2, "w": 1, "h": 1},
    {"x": 0, "y": 0, "w": 0, "h": 1}, {"x": 0, "y": 0, "w": 1, "h": -1},
    {"x": 0.5, "y": 0, "w": 0.6, "h": 1}, {"x": 0, "y": 0.5, "w": 1, "h": 0.6},
    {"x": float("nan"), "y": 0, "w": 1, "h": 1},
    {"x": 0, "y": 0, "w": float("inf"), "h": 1},
])
def test_profile_rejects_invalid_crop(crop):
    with pytest.raises(ValueError):
        LaunchProfile.model_validate({"name": "Synthetic", "textSource": "ocr", "ocrCropRect": crop})


@pytest.mark.parametrize("crop", [
    {"x": 0, "y": 0, "w": 1, "h": 1}, {"x": 0, "y": 0.65, "w": 1, "h": 0.35},
    {"x": 0.8, "y": 0.8, "w": 0.2, "h": 0.2},
])
def test_valid_crop_including_window_edges_is_preserved(crop):
    assert LaunchProfile.model_validate({"name": "Synthetic", "textSource": "ocr", "ocrCropRect": crop}).ocrCropRect == crop


@pytest.mark.parametrize("selectors", [
    {"ocrWindowId": 11}, {"ocrWindowName": "Dialogue"}, {"ocrAppName": "Game"}, {},
])
def test_start_resolves_and_binds_exact_window(selectors):
    async def run():
        source = MacVisionOCRTextSource(AsyncMock(), AsyncMock())
        proc = Mock(pid=123, returncode=None, stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
        proc.wait = AsyncMock(return_value=0)
        proc.communicate = AsyncMock(return_value=(b"", b""))
        with patch("server.vn_text_sources.sys.platform", "darwin"), \
             patch("server.vn_text_sources.ensure_macos_helper", return_value=Path("/synthetic/helper")), \
             patch("server.vn_text_sources.list_macos_capture_windows", return_value=[WINDOW]), \
             patch("server.vn_text_sources.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
            await source.start(selectors, {}, target_pid=42 if not selectors else None)
            args = spawn.call_args.args
            assert args[args.index("--window-id") + 1] == "11"
            assert args[args.index("--pid") + 1] == "42"
            assert source.status()[1]["window"] == WINDOW
            assert source.status()[1]["status"] == "connecting"
            await source.stop()
            proc.terminate.assert_called_once()
    asyncio.run(run())


def test_invalid_crop_override_cannot_start_capture():
    async def run():
        source = MacVisionOCRTextSource(AsyncMock(), AsyncMock())
        with patch("server.vn_text_sources.sys.platform", "darwin"), \
             patch("server.vn_text_sources.ensure_macos_helper") as build:
            with pytest.raises(ValueError):
                await source.start({"ocrWindowId": 11}, {"ocrCropRect": {"x": 2, "y": 0, "w": 1, "h": 1}}, target_pid=None)
            build.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("messages,code,expected", [
    ([{"type": "status", "status": "running"}], 0, "exited"),
    ([{"type": "status", "status": "running"}], 3, "error"),
    ([{"type": "error", "error": "Synthetic capture failure"}], 1, "error"),
    ([{"type": "status", "status": "error", "error": "PermissionDenied: synthetic"}], 1, "error"),
])
def test_native_errors_and_eof_are_observable(messages, code, expected):
    async def run():
        source = MacVisionOCRTextSource(AsyncMock(), AsyncMock())
        stream = asyncio.StreamReader()
        for message in messages:
            stream.feed_data(json.dumps(message).encode() + b"\n")
        stream.feed_eof()
        source._proc = Mock(pid=123, returncode=code, stdout=stream, wait=AsyncMock(return_value=code))
        await source._read_loop()
        state = source.status()[1]
        assert state["status"] == expected
        assert state.get("error")
        if messages[-1].get("error"):
            assert state["error"] == messages[-1]["error"]
        assert source.status()[0]["status"] == "exited"
    asyncio.run(run())


def test_consumer_failure_terminates_helper_and_does_not_block_launcher_stop(tmp_path):
    async def run():
        runtime_stop = AsyncMock()
        manager = VNLaunchManager(tmp_path, runtime_start=AsyncMock(), runtime_stop=runtime_stop,
                                  runtime_status=AsyncMock(), runtime_line=AsyncMock())
        source = MacVisionOCRTextSource(AsyncMock(side_effect=RuntimeError("Synthetic consumer failure")), AsyncMock())
        source._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", 'import time; print(\'{"type":"line","text":"Synthetic dialogue"}\', flush=True); time.sleep(60)',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        proc = source._proc
        source._read_task = asyncio.create_task(source._read_loop())
        await asyncio.wait_for(asyncio.shield(source._read_task), 5)
        assert proc.returncode is not None
        assert source.status()[1]["error"] == "Synthetic consumer failure"
        manager._text_source = source
        manager._runtime_owned = True
        await manager.stop()
        assert source._proc is None
        assert manager._text_source is None
        runtime_stop.assert_awaited_once()
    asyncio.run(run())


def test_helper_stderr_is_drained_and_bounded():
    async def run():
        source = MacVisionOCRTextSource(AsyncMock(), AsyncMock())
        source._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", 'import sys; sys.stderr.write("x" * 200000); sys.exit(2)',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        source._stderr_task = asyncio.create_task(source._drain_stderr())
        try:
            await asyncio.wait_for(source._read_loop(), 5)
            assert source.status()[1]["status"] == "error"
            assert source.status()[1]["error"] == "x" * 500
        finally:
            await source.stop()
    asyncio.run(run())


def test_stop_cleans_up_even_if_reader_task_has_already_failed():
    async def run():
        source = MacVisionOCRTextSource(AsyncMock(), AsyncMock())
        async def fail():
            raise RuntimeError("Synthetic task failure")
        source._read_task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        source._proc = Mock(returncode=None, wait=AsyncMock(return_value=0))
        source._proc.communicate = AsyncMock(return_value=(b"", b""))
        proc = source._proc
        await source.stop()
        proc.terminate.assert_called_once()
        assert source.status()[1]["error"] == "Synthetic task failure"
    asyncio.run(run())


@pytest.mark.parametrize("window_id,windows", [(11, [WINDOW]), (11, [OTHER])])
def test_macos_visual_capture_uses_exact_identity(window_id, windows):
    with patch.object(visual_runtime.os, "name", "posix"), patch.object(sys, "platform", "darwin"), \
         patch("server.macos_capture.list_macos_capture_windows", return_value=windows), \
         patch("server.window_capture.capture_window_frame", return_value=Image.new("RGB", (800, 600))) as capture:
        if windows == [WINDOW]:
            result = visual_runtime.capture_game_window(42, "", window_id=window_id)
            assert result["actualScope"] == "game_window"
            assert result["game"]["windowId"] == 11
            capture.assert_called_once_with(11, expected_pid=42)
        else:
            with pytest.raises(RuntimeError, match="Select"):
                visual_runtime.capture_game_window(42, "", window_id=window_id, app_name="Game")
            capture.assert_not_called()


def test_preview_rejects_stale_selection_without_capture():
    async def run():
        with patch.object(sys, "platform", "darwin"), \
             patch("server.vn_text_sources.list_macos_capture_windows", return_value=[OTHER]), \
             patch("server.vn_text_sources.ensure_macos_helper") as build:
            result = await capture_macos_window_preview(window_id=11, app_name="Game")
            assert "Select" in result["error"]
            build.assert_not_called()
    asyncio.run(run())


def test_launcher_capture_uses_session_binding_not_edited_profile(tmp_path):
    async def run():
        manager = VNLaunchManager(tmp_path, runtime_start=AsyncMock(), runtime_stop=AsyncMock(),
                                  runtime_status=AsyncMock(return_value={"visual": {"supported": True}, "capabilities": {"interaction": {"enabled": True}}}),
                                  runtime_line=AsyncMock())
        manager._state.update(status="active", textSource="ocr", profileId="synthetic")
        manager._text_source = Mock(status=Mock(return_value=({}, {"window": WINDOW})))
        with patch.object(manager, "_profile_by_id", return_value={"ocrWindowId": 12}), \
             patch.object(visual_runtime, "capture_game_window", return_value={"frame": {}}) as capture:
            await manager.capture()
            capture.assert_called_once_with(42, "", window_id=11)
    asyncio.run(run())


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot directly execute POSIX shell scripts")
def test_helper_refreshes_existing_binary_and_reports_failed_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_capture, "__file__", str(tmp_path / "server/macos_capture.py"))
    tools = tmp_path / "tools"
    tools.mkdir()
    script = tools / "build_mac_screen_ocr.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "tools/build_mac_screen_ocr.sh", script)
    script.chmod(0o755)
    source = tools / "mac_screen_ocr.swift"
    source.write_text("version-one")
    (tools / "mac_ocr_dialogue.swift").touch()
    commands = tmp_path / "commands"
    commands.mkdir()
    uname = commands / "uname"
    uname.write_text('#!/bin/sh\nif [ "$1" = "-s" ]; then echo Darwin; else echo arm64; fi\n')
    uname.chmod(0o755)
    compiler = commands / "swiftc"
    compiler.write_text('#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\nshift\ncat "$TEST_SWIFT_SOURCE" > "$1"\nchmod +x "$1"\n')
    compiler.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TEST_SWIFT_SOURCE", str(source))
    binary = macos_capture.ensure_macos_helper()
    assert binary.read_text() == "version-one"
    source.write_text("version-two")
    newer = binary.stat().st_mtime + 1
    os.utime(source, (newer, newer))
    assert macos_capture.ensure_macos_helper().read_text() == "version-two"
    compiler.write_text('#!/bin/sh\necho "Synthetic build failure" >&2\nexit 1\n')
    with pytest.raises(RuntimeError, match="Synthetic build failure"):
        macos_capture.ensure_macos_helper()
    assert binary.read_text() == "version-two", "failed compilation must preserve the last binary"


@pytest.fixture(scope="module")
def dialogue_executable(tmp_path_factory):
    compiler = shutil.which("swiftc")
    if not compiler:
        pytest.skip("Swift compiler is unavailable")
    root = tmp_path_factory.mktemp("ocr-dialogue")
    main = root / "main.swift"
    main.write_text('''import Foundation
let snapshots = try JSONDecoder().decode([String].self, from: FileHandle.standardInput.readDataToEndOfFile())
var tracker = OCRDialogueTracker()
let result = snapshots.compactMap { tracker.observe($0) }
FileHandle.standardOutput.write(try JSONEncoder().encode(result))
''')
    executable = root / "dialogue"
    subprocess.run([compiler, str(Path(__file__).resolve().parents[1] / "tools/mac_ocr_dialogue.swift"),
                    str(main), "-o", str(executable)], check=True, capture_output=True, timeout=60)
    return executable


@pytest.mark.parametrize("snapshots,expected", [
    (["你", "你好", "你好世界", "你好世界", "你好世界"], ["你好世界"]),
    (["Dialogue", "Dialogue", "Dialogve", "Dialogue", "Dialogue"], ["Dialogue"]),
    (["Dialogue", "Dialogue", "", "", "Dialogue", "Dialogue"], ["Dialogue", "Dialogue"]),
    (["Dialogue", "Dialogue", "", "Dialogue", "Dialogue"], ["Dialogue"]),
    (["", "", "First", "First", "Second", "Second"], ["First", "Second"]),
])
def test_native_snapshot_semantics(dialogue_executable, snapshots, expected):
    result = subprocess.run([str(dialogue_executable)], input=json.dumps(snapshots), text=True,
                            capture_output=True, check=True, timeout=5)
    assert json.loads(result.stdout) == expected


@pytest.fixture(scope="module")
def native_capture_helper():
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("macOS native capture compiler is unavailable")
    return macos_capture.ensure_macos_helper()


@pytest.mark.parametrize("crop", ["2,2,1,1", "0,0,-1,1", "0,0,1,0", "nan,0,1,1", "bad", "bad,0,0,1,1", "0,,0,1,1"])
def test_native_crop_rejected_before_screen_access(native_capture_helper, crop):
    result = subprocess.run([str(native_capture_helper), "--stream", "--crop", crop],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert "crop" in result.stderr
    assert not result.stdout, "invalid crop must fail before starting capture or requesting permission"


def test_consumer_failure_releases_helper_with_full_stdout_pipe():
    async def run():
        source = MacVisionOCRTextSource(AsyncMock(), AsyncMock())
        source._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", 'import sys,time; print(\'{"type":"line","text":"Synthetic dialogue"}\', flush=True); sys.stdout.write("x" * 1000000); sys.stdout.flush(); time.sleep(60)',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        async def consumer(_payload):
            # Wait for pipe backpressure to make cleanup's drain necessary.
            while not source._proc.stdout._transport._paused:
                await asyncio.sleep(0)
            raise RuntimeError("Synthetic consumer failure")
        source._on_line = consumer
        source._read_task = asyncio.create_task(source._read_loop())
        try:
            await asyncio.wait_for(asyncio.shield(source._read_task), 5)
            assert source._proc.returncode is not None
            assert source.status()[1]["error"] == "Synthetic consumer failure"
        finally:
            await source.stop()
    asyncio.run(run())
