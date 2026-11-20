"""VN runtime and session-owned player input controls."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from server.protocol import Method
from server.ws_handler import RequestHandler
from vn_player import VNPlayerRuntime


class VNPlayerHandler(RequestHandler):
    methods = [Method.VN_START, Method.VN_STOP, Method.VN_STATUS, Method.VN_LINE,
               Method.VN_PLAYER_NOTE, Method.VN_PLAYER_ASK, Method.VN_PLAYER_PIN,
               Method.VN_CHOICE_ASK, Method.VN_MODE_SET, Method.VN_INPUT_SET]

    def __init__(self) -> None:
        self._runtime: VNPlayerRuntime | None = None
        self._event_emit = None
        self._asr_control = None
        self._asr_state = None
        self._capture_game_view = None
        self._input_lock = asyncio.Lock()
        self._voice_id = ""
        self._voice_starting = False
        self._voice_error = ""
        self._vision_mode = "off"
        self._input_kind = "ask"

    def configure(self, project_root: Path, *, event_emit=None, speak_callback=None,
                  speech_epoch=None, speech_finished=None, asr_control=None, asr_state=None, capture_game_view=None) -> None:
        self._event_emit = event_emit
        self._asr_control = asr_control
        self._asr_state = asr_state
        self._capture_game_view = capture_game_view
        self._runtime = VNPlayerRuntime(project_root, event_emit=self._emit_runtime,
                                        speak_callback=speak_callback, speech_epoch=speech_epoch,
                                        speech_finished=speech_finished)

    def _session_id(self) -> str:
        runtime = self._runtime
        return str(runtime.profile.session_id) if runtime and runtime.enabled and runtime.profile else ""

    def set_overlay_url(self, url: str) -> None:
        if self._runtime and self._runtime.profile:
            self._runtime.profile.overlay_url = url

    def _inputs(self, status: dict[str, Any]) -> dict[str, Any]:
        session_id = self._session_id()
        interaction = status.get("capabilities", {}).get("interaction", {})
        interaction_ready = bool(session_id and interaction.get("enabled"))
        visual = status.get("visual", {})
        asr = self._asr_state() if self._asr_state else {}
        source = asr.get("source_payload") or {}
        listening = bool(asr.get("active") and asr.get("source") == "vn_player"
                         and source.get("session_id") == session_id
                         and source.get("input_id") == self._voice_id and self._voice_id)
        voice_available = bool(interaction_ready and self._asr_control)
        vision_available = bool(interaction_ready and visual.get("supported") and self._capture_game_view)
        unavailable = "inactive_vn_session" if not session_id else str(interaction.get("reason") or "interaction_unavailable")
        return {
            "session_id": session_id, "kind": self._input_kind,
            "voice": {"enabled": bool(voice_available and self._voice_id and (listening or self._voice_starting)),
                      "listening": listening, "starting": self._voice_starting,
                      "available": voice_available, "error": self._voice_error,
                      "reason": "ready" if voice_available else unavailable if not interaction_ready else "asr_unavailable"},
            "vision": {"mode": self._vision_mode, "enabled": self._vision_mode == "on_question" and vision_available,
                       "available": vision_available,
                       "reason": "ready" if vision_available else unavailable if not interaction_ready else str(visual.get("reason") or "game_view_unavailable")},
        }

    def status(self) -> dict[str, Any]:
        status = self._runtime.status() if self._runtime else {"status": "stopped"}
        return {**status, "inputs": self._inputs(status)}

    async def _emit_runtime(self, method: str, payload: dict[str, Any]) -> None:
        if method == Method.VN_STATUS:
            await self._reconcile_voice(payload)
            payload = {**payload, "inputs": self._inputs(payload)}
        if self._event_emit:
            await self._event_emit(method, payload)

    async def _publish_inputs(self) -> None:
        if self._event_emit:
            await self._event_emit(Method.VN_STATUS, self.status())

    async def _stop_voice(self, session_id: str, input_id: str) -> None:
        if input_id and self._asr_control:
            await self._asr_control(Method.ASR_STOP, {"source": "vn_player", "session_id": session_id, "input_id": input_id})

    async def _reconcile_voice(self, status: dict[str, Any]) -> None:
        if self._voice_id and (not self._session_id() or not status.get("capabilities", {}).get("interaction", {}).get("enabled")):
            old_id = self._voice_id
            self._voice_id = ""
            self._voice_starting = False
            await self._stop_voice(self._session_id(), old_id)

    async def _reset_inputs(self) -> None:
        old_id = self._voice_id
        self._voice_id = ""
        self._vision_mode = "off"
        self._voice_error = ""
        self._voice_starting = False
        self._input_kind = "ask"
        await self._stop_voice(self._session_id(), old_id)

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("VN Player handler is not configured")
        if method == Method.VN_START:
            mode = str(params.get("vision_mode") or "off")
            if mode not in {"off", "on_question"}:
                raise ValueError("Unsupported VN vision mode")
            await self._reset_inputs()
            await runtime.start(params)
            self._vision_mode = mode
            if params.get("voice_input"):
                try:
                    await self.set_inputs({"session_id": self._session_id(), "voice": True})
                except (ValueError, RuntimeError) as exc:
                    self._voice_error = str(exc)
            await self._publish_inputs()
            return self.status()
        if method == Method.VN_STOP:
            await self._reset_inputs()
            await runtime.stop(params)
            return self.status()
        if method == Method.VN_STATUS:
            await self._reconcile_voice(runtime.status())
            return {**self.status(), **({"activity": runtime.activity()} if params.get("include_history") else {})}
        if method == Method.VN_INPUT_SET:
            return await self.set_inputs(params)
        if method == Method.VN_LINE:
            return await runtime.ingest_line(params)
        kinds = {Method.VN_PLAYER_NOTE: "note", Method.VN_PLAYER_ASK: "ask",
                 Method.VN_PLAYER_PIN: "pin", Method.VN_CHOICE_ASK: "choice"}
        if method in kinds:
            return await self._intervene(kinds[method], params)
        if method == Method.VN_MODE_SET:
            await runtime.set_preferences(params)
            return self.status()
        return None

    async def submit_source_line(self, params: dict[str, Any]) -> dict[str, Any]:
        """Internal adapter handoff; vn.line keeps its completed-response contract."""
        if self._runtime is None:
            raise RuntimeError("VN Player handler is not configured")
        return await self._runtime.submit_line(params)

    async def set_inputs(self, params: dict[str, Any]) -> dict[str, Any]:
        async with self._input_lock:
            session_id = str(params.get("session_id") or "")
            if not session_id or session_id != self._session_id():
                raise ValueError("The VN session has ended or changed.")
            inputs = self.status()["inputs"]
            if "voice" in params and not isinstance(params["voice"], bool):
                raise ValueError("VN voice input must be a boolean.")
            if params.get("vision_mode", self._vision_mode) not in {"off", "on_question"}:
                raise ValueError("Unsupported VN vision mode")
            if params.get("kind", self._input_kind) not in {"ask", "choice", "note", "pin"}:
                raise ValueError("Unsupported VN input kind")
            voice = params.get("voice", inputs["voice"]["enabled"])
            if voice and not inputs["voice"]["available"]:
                raise RuntimeError(inputs["voice"]["reason"])
            if params.get("vision_mode") == "on_question" and not inputs["vision"]["available"]:
                raise RuntimeError(inputs["vision"]["reason"])
            next_kind = str(params.get("kind", self._input_kind))
            old_id = self._voice_id
            restart_voice = bool(voice and (not old_id or not inputs["voice"]["listening"] or next_kind != self._input_kind))
            if not voice or restart_voice:
                self._voice_id = ""  # revoke in-flight recognition before awaiting hardware
                self._voice_starting = False
                await self._stop_voice(session_id, old_id)
            if session_id != self._session_id():
                return self.status()
            self._vision_mode = str(params.get("vision_mode", self._vision_mode))
            self._input_kind = next_kind
            self._voice_error = ""
            if restart_voice:
                input_id = uuid.uuid4().hex
                self._voice_id = input_id
                self._voice_starting = True
                await self._publish_inputs()
                try:
                    result = await self._asr_control(Method.ASR_START, {
                        "source": "vn_player", "one_shot": False, "finish_after_turn_complete": False,
                        "source_payload": {"session_id": session_id, "input_id": input_id, "kind": next_kind},
                    })
                    if str((result or {}).get("status")) not in {"listening", "awake", "starting"}:
                        raise RuntimeError("Microphone is busy in another session." if (result or {}).get("status") == "already_listening"
                                           else str((result or {}).get("error") or "Microphone could not start."))
                    if session_id != self._session_id() or self._voice_id != input_id:
                        await self._stop_voice(session_id, input_id)
                        return self.status()
                except Exception as exc:
                    if self._voice_id == input_id:
                        self._voice_id = ""
                        self._voice_starting = False
                        self._voice_error = str(exc)
                    await self._publish_inputs()
                    raise
                finally:
                    if self._voice_id in {input_id, ""}:
                        self._voice_starting = False
            await self._publish_inputs()
            return self.status()

    async def asr_stopped(self, payload: dict[str, Any]) -> None:
        source = payload.get("source_payload") or {}
        if (payload.get("source") == "vn_player" and source.get("session_id") == self._session_id()
                and source.get("input_id") == self._voice_id):
            self._voice_id = ""
            self._voice_starting = False
            await self._publish_inputs()

    async def _intervene(self, kind: str, params: dict[str, Any], *, voice_id: str = "") -> dict[str, Any]:
        session_id = self._session_id()
        if not session_id or params.get("session_id", session_id) != session_id:
            return {"status": "ignored", "reason": "inactive_or_changed_vn_session"}
        runtime = self._runtime
        assert runtime is not None
        if kind in {"ask", "choice"} and self._vision_mode == "on_question" and not params.get("visual_context"):
            state = self.status()["inputs"]["vision"]
            if not state["available"]:
                return {"status": "unavailable", "reason": state["reason"]}
            try:
                result = await self._capture_game_view()
                visual = result.get("visual_context") if isinstance(result, dict) else None
                if not visual:
                    raise RuntimeError("Game view is unavailable.")
            except Exception as exc:
                if self._event_emit:
                    await self._event_emit(Method.VN_ERROR, {"error": str(exc), "source": "vn_input"})
                return {"status": "unavailable", "reason": "visual_capture_failed", "error": str(exc)}
            if session_id != self._session_id() or (voice_id and self._voice_id != voice_id):
                return {"status": "ignored", "reason": "inactive_or_changed_vn_input"}
            # A user may turn vision off while capture is pending.
            if self._vision_mode == "on_question":
                params = {**params, "visual_context": visual}
        if voice_id and self._voice_id != voice_id:
            return {"status": "ignored", "reason": "inactive_or_changed_vn_input"}
        return await runtime.player_intervention(kind, params)

    async def handle_asr(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = payload.get("source_payload") or {}
        if (not isinstance(source, dict) or source.get("session_id") != self._session_id()
                or not self._voice_id or source.get("input_id") != self._voice_id):
            return {"status": "ignored", "reason": "inactive_or_changed_vn_input"}
        if not self.status()["inputs"]["voice"]["enabled"]:
            return {"status": "ignored", "reason": "interaction_unavailable"}
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"status": "ignored", "reason": "empty_text"}
        return await self._intervene(self._input_kind, {
            "text": text, "source": "asr", "session_id": source["session_id"],
            "metadata": {"source": "vn_player_asr", "asr": {"is_final": bool(payload.get("is_final", True))}},
        }, voice_id=self._voice_id)
