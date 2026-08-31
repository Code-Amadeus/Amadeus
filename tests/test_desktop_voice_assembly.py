from __future__ import annotations

import ast
import asyncio
import copy
import logging
from pathlib import Path
from types import SimpleNamespace


_APP_PATH = Path(__file__).resolve().parents[1] / "server" / "app.py"


class _LiftNonlocal(ast.NodeTransformer):
    def visit_Nonlocal(self, node: ast.Nonlocal) -> ast.Global:
        return ast.copy_location(ast.Global(names=node.names), node)


def _load_bootstrap_function(name: str, namespace: dict):
    tree = ast.parse(_APP_PATH.read_text(encoding="utf-8"), filename=str(_APP_PATH))
    bootstrap = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "bootstrap"
    )
    target = next(
        node
        for node in ast.walk(bootstrap)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )
    lifted = _LiftNonlocal().visit(copy.deepcopy(target))
    module = ast.fix_missing_locations(ast.Module(body=[lifted], type_ignores=[]))
    exec(compile(module, str(_APP_PATH), "exec"), namespace)
    return namespace[name]


class _RecordingAsr:
    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.completions: list[str] = []
        self.stops: list[str] = []
        self.dispatched: list[str] = []

    async def start_listening(self, params: dict) -> None:
        self.starts.append(params)

    async def notify_turn_complete(self, reason: str) -> None:
        self.completions.append(reason)

    async def stop_listening(self, reason: str) -> None:
        self.stops.append(reason)

    async def _dispatch_recognized(self, text: str) -> None:
        self.dispatched.append(text)


def test_first_wake_uses_only_the_configured_awake_window(monkeypatch) -> None:
    asr = _RecordingAsr()

    async def allow_voice(_source: str) -> bool:
        return True

    monkeypatch.setattr(
        "core.turn_coordinator.get_turn_coordinator",
        lambda: SimpleNamespace(on_wake_detected=lambda: None),
    )
    function = _load_bootstrap_function(
        "_start_asr_from_wake",
        {
            "ASR_IDLE_UNLOAD_SECONDS": 180,
            "WAKE_AUTO_SEND_TO_CHAT": False,
            "WAKE_AWAKE_SECONDS": 60,
            "_main_voice_allowed_now": allow_voice,
            "asr_h": asr,
            "logger": logging.getLogger(__name__),
        },
    )

    asyncio.run(function({"phrase": "hi amadeus"}))

    assert asr.starts == [
        {
            "source": "wake",
            "wake": {"phrase": "hi amadeus"},
            "awake_seconds": 60.0,
            "finish_after_turn_complete": False,
        }
    ]


def test_barge_in_rearm_uses_only_the_configured_awake_window(monkeypatch) -> None:
    asr = _RecordingAsr()
    detector = SimpleNamespace(stop=lambda: None)

    async def allow_voice(_source: str) -> bool:
        return True

    class _InterruptFlow:
        async def interrupt(self, **_kwargs) -> None:
            return None

    monkeypatch.setattr(
        "server.interrupt_flow.get_interrupt_flow",
        lambda: _InterruptFlow(),
    )
    function = _load_bootstrap_function(
        "_interrupt_for_barge_in",
        {
            "ASR_IDLE_UNLOAD_SECONDS": 180,
            "WAKE_AWAKE_SECONDS": 60,
            "_last_barge_in_interrupt_at": 0.0,
            "_main_voice_allowed_now": allow_voice,
            "asr_h": asr,
            "barge_in_detector": detector,
            "logger": logging.getLogger(__name__),
            "time": SimpleNamespace(monotonic=lambda: 10.0),
        },
    )

    asyncio.run(function())

    assert asr.completions == ["barge_in"]
    assert asr.starts == [
        {
            "source": "wake",
            "wake": {"source": "barge_in"},
            "awake_seconds": 60.0,
            "finish_after_turn_complete": False,
        }
    ]


def test_wake_bridge_host_gate_does_not_depend_on_loaded_asr_session() -> None:
    asr = _RecordingAsr()
    public_events: list[tuple[object, dict]] = []
    chat_texts: list[str] = []

    class _Bus:
        async def emit(self, method, payload) -> None:
            public_events.append((method, payload))

    async def send_text(text: str, **_kwargs) -> None:
        chat_texts.append(text)

    function = _load_bootstrap_function(
        "_handle_wake_bridge_text",
        {
            "Method": SimpleNamespace(ASR_RECOGNIZED="asr.recognized"),
            "_send_wake_text": send_text,
            "asr_h": asr,
            "bus": _Bus(),
            "logger": logging.getLogger(__name__),
        },
    )

    asyncio.run(function({"text": "停止对话", "source": "wake", "bridge": True}))
    asyncio.run(function({"text": "如何停止对话", "source": "wake", "bridge": True}))

    assert asr.stops == ["voice_stop_command"]
    assert asr.dispatched == []
    assert [payload["text"] for _, payload in public_events] == ["如何停止对话"]
    assert chat_texts == ["如何停止对话"]
