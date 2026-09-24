"""WebSocket handler for VN Player mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server.protocol import Method
from server.ws_handler import RequestHandler
from vn_player import VNPlayerRuntime


class VNPlayerHandler(RequestHandler):
    methods = [
        Method.VN_START,
        Method.VN_STOP,
        Method.VN_STATUS,
        Method.VN_LINE,
        Method.VN_PLAYER_NOTE,
        Method.VN_PLAYER_ASK,
        Method.VN_PLAYER_PIN,
        Method.VN_CHOICE_ASK,
        Method.VN_MODE_SET,
    ]

    def __init__(self) -> None:
        self._runtime: VNPlayerRuntime | None = None

    def configure(self, project_root: Path, *, event_emit=None, speak_callback=None) -> None:
        self._runtime = VNPlayerRuntime(
            project_root,
            event_emit=event_emit,
            speak_callback=speak_callback,
        )

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("VN Player handler is not configured")
        if method == Method.VN_START:
            return await runtime.start(params)
        if method == Method.VN_STOP:
            return await runtime.stop(params)
        if method == Method.VN_STATUS:
            return runtime.status()
        if method == Method.VN_LINE:
            return await runtime.ingest_line(params)
        if method == Method.VN_PLAYER_NOTE:
            return await runtime.player_intervention("note", params)
        if method == Method.VN_PLAYER_ASK:
            return await runtime.player_intervention("ask", params)
        if method == Method.VN_PLAYER_PIN:
            return await runtime.player_intervention("pin", params)
        if method == Method.VN_CHOICE_ASK:
            return await runtime.player_intervention("choice", params)
        if method == Method.VN_MODE_SET:
            # MVP stores mode changes as runtime policy events. Full profile
            # switching can be layered on top without changing vn.line.
            return await runtime.player_intervention("mode", params)
        return None

    async def handle_asr(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Route speech only to the still-active VN session that requested it."""
        runtime = self._runtime
        source = payload.get("source_payload")
        if not isinstance(source, dict):
            source = {}
        if (runtime is None or not runtime.enabled or runtime.profile is None
                or source.get("session_id") != runtime.profile.session_id):
            return {"status": "ignored", "reason": "inactive_or_changed_vn_session"}
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"status": "ignored", "reason": "empty_text"}
        kind = str(source.get("kind") or "ask").strip().lower()
        if kind not in {"ask", "note", "pin", "choice"}:
            kind = "ask"
        return await runtime.player_intervention(kind, {
            "text": text, "source": "asr",
            "metadata": {"source": "vn_player_asr", "asr": {
                "is_final": bool(payload.get("is_final", True)), "source_payload": source,
            }},
        })
