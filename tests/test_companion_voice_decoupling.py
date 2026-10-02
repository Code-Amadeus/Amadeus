"""Audit: Companion voice stays generic, and remote ASR stays remote.

The repository structure this pins down:

    Companion generic voice + Companion generic vision
    + Game Companion / VN / readers as scene consumers only

Three things are checked: the Companion control shell owns no VN session, the
remote recognizer never drags in a local model, and the launchers select the
remote recognizer and the local voice output.
"""

from __future__ import annotations

import ast
import re
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HANDLER = ROOT / "server" / "handlers" / "companion_control_handler.py"
LAUNCHER = ROOT / "run_amadeus_companion_python.bat"
LAUNCHER_HIDDEN = ROOT / "run_amadeus_companion_python_hidden.vbs"


# --- 1. the Companion control shell is generic -------------------------------


def test_the_control_shell_imports_no_scene_module():
    source = HANDLER.read_text(encoding="utf-8")
    imported = " ".join(
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    )
    assert "vn" not in imported.lower()
    assert "reading" not in imported.lower()
    for banned in ("vn_player", "vn_launch", "VNPlayer", "ReadingSession"):
        assert banned not in source, f"Companion control shell references {banned}"


def test_the_control_shell_uses_the_generic_asr_and_chat_entry_points():
    source = HANDLER.read_text(encoding="utf-8")
    # A generic conversation source, not a VN session id.
    assert '"source": "companion"' in source
    # Chat is injected, so the shell cannot reach a scene runtime directly.
    assert "_chat_send" in source
    assert "chat_h" not in source
    # Vision is a Companion mode, not a VN overlay toggle.
    assert '{"off", "on_question"}' in source


def test_an_explicit_vision_request_beats_the_ambient_vision_mode():
    """The control shell and the runtime use two vocabularies on purpose.

    The shell reports `off | on_question` for the card, while the runtime reads
    `AMADEUS_VISION_MODE` (`off | on_demand | watching`). They must not fight:
    when the Companion attaches a screenshot explicitly, the runtime captures it
    regardless of the ambient mode.
    """
    runtime = (ROOT / "server" / "visual_runtime.py").read_text(encoding="utf-8")
    explicit_gate = runtime.split("enabled = _config.enabled and mode != \"off\"", 1)[1].split("try:", 1)[0]
    assert "if not enabled and not explicit:" in explicit_gate
    assert "should_capture = explicit" in explicit_gate
    # The ambient mode may only add captures, never veto an explicit one.
    assert explicit_gate.index("should_capture = explicit") < explicit_gate.index('mode == "watching"')

    handler = HANDLER.read_text(encoding="utf-8")
    assert '"on_question"' in handler
    assert "AMADEUS_VISION_MODE" not in handler, (
        "the shell must not read the ambient mode; it only owns its own toggle"
    )


def test_companion_asr_routes_into_the_generic_chat(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_send_wake_text(text, *, source="wake", visual=None, force_auto_send=False):
        captured.update(text=text, source=source, visual=visual, force_auto_send=force_auto_send)

    # The routing lives in a closure inside bootstrap; assert on its source
    # contract instead of importing the whole server.
    source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
    companion_branch = source.split('if source == "companion":', 1)[1].split("return", 1)[0]
    assert "_send_wake_text" in companion_branch
    assert 'source="companion ASR"' in companion_branch
    assert "force_auto_send=True" in companion_branch, (
        "Companion speech must not depend on the wake auto-send preference"
    )
    # The VN branch is separate and reached only for source == "vn_player"; it
    # delegates to a helper, which is the only place that touches the VN runtime.
    assert 'if source == "vn_player":' in source
    vn_branch = source.split('if source == "vn_player":', 1)[1].split('if source == "companion":', 1)[0]
    assert "_handle_vn_player_asr_recognized" in vn_branch
    assert "vn_h.handle_asr" not in companion_branch
    vn_helper = source.split("async def _handle_vn_player_asr_recognized", 1)[1]
    assert "vn_h.handle_asr" in vn_helper


# --- 2. the remote recognizer stays remote -----------------------------------


def test_the_registry_advertises_the_remote_recognizer():
    from asr import registry

    ids = registry.asr_backend_ids()
    assert "qwen_remote" in ids
    descriptor = registry._REGISTRY["qwen_remote"]
    assert descriptor.deployment == "remote"
    assert "no local model runtime" in descriptor.summary


def test_importing_the_registry_does_not_load_the_local_recognizer():
    """Selection must not have a side effect that pulls in qwen3_asr.

    Run in a fresh interpreter: in-process state depends on which other tests
    already imported the local backend.
    """
    import subprocess

    probe = (
        "import sys; sys.path.insert(0, r'%s');\n"
        "from asr import registry;\n"
        "registry.asr_backend_ids();\n"
        "print('LOADED' if 'asr.backends.qwen3_asr' in sys.modules else 'CLEAN')\n"
    ) % ROOT
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-400:]
    assert "CLEAN" in result.stdout, result.stdout


def test_the_remote_probe_reports_availability_without_a_local_model(monkeypatch):
    from asr import registry

    status, detail = registry._qwen_remote_probe()
    assert status in {"remote", "unavailable", "not_installed"}
    # Whatever the config, the probe never claims a local model is needed.
    assert "local" not in detail.lower() or status == "remote"


def test_an_unknown_backend_is_rejected_instead_of_falling_back(monkeypatch):
    """No silent fallback to the local model when the remote name is wrong."""
    from asr import registry

    with pytest.raises(ValueError, match="unknown ASR backend"):
        registry.create_asr_backend("qwen_remote_typo")


def test_the_remote_backend_fails_loudly_without_credentials(monkeypatch):
    from asr.backends.qwen_remote import QwenRemoteASRBackend

    backend = QwenRemoteASRBackend(base_url="", api_key="", model="")
    with pytest.raises(Exception) as failure:
        backend.load("cpu")
    assert "required" in str(failure.value)


def test_the_remote_backend_makes_no_local_import():
    source = (ROOT / "asr" / "backends" / "qwen_remote.py").read_text(encoding="utf-8")
    for banned in ("torch", "transformers", "qwen3_asr", "funasr", "onnxruntime"):
        assert banned not in source, f"remote recognizer imports {banned}"


# --- 3. the launchers select the generic stack --------------------------------


@pytest.mark.parametrize("path", [LAUNCHER, LAUNCHER_HIDDEN])
def test_launchers_select_remote_recognition_and_local_voice_output(path: Path):
    source = path.read_text(encoding="utf-8")

    def selects(name: str, value: str) -> bool:
        # batch: set NAME=value      vbs: env("NAME") = "value"
        return bool(re.search(rf'{name}[")]*\s*=\s*"?{value}', source))

    assert selects("ASR_BACKEND", "qwen_remote"), "must not load the local recognizer"
    assert selects("TTS_BACKEND", "gpt_sovits")
    assert selects("TTS_DEVICE", "cuda")
    assert selects("AMADEUS_VISION_SCOPE", "read_window")


@pytest.mark.parametrize("path", [LAUNCHER, LAUNCHER_HIDDEN])
def test_launchers_do_not_show_companion_only_wording(path: Path):
    source = path.read_text(encoding="utf-8")
    assert "Companion-only" not in source
    assert "companion-only" not in source
