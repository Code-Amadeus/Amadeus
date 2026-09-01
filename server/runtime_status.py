"""Read-only runtime status snapshot (runtime convergence plan, Phase 1).

Aggregates the live state of every runtime subsystem WITHOUT taking ownership
of any of it. This module never mutates state and never constructs lazy
singletons — uninitialized subsystems report as absent instead of being
instantiated as a side effect of observation.

Each section is best-effort: a failing subsystem yields {"error": "..."}
instead of breaking the whole snapshot.

Reads of private attributes (chat_handler._chat_epoch etc.) are intentional
and temporary: they document exactly which state the future TurnCoordinator
must take ownership of. When the coordinator lands, sections here should
switch to reading its ledger instead.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("runtime_status")


def _runtime_code_identity() -> dict[str, Any]:
    """Identify the exact checkout loaded by this long-lived process.

    Live acceptance must not compare current source files with an older
    backend that happened to keep the same port. A launcher may freeze the
    identity in environment variables; ordinary product starts derive it once
    from Git. Failure remains visible but never blocks runtime startup.
    """

    root = Path(__file__).resolve().parents[1]
    env_sha = str(os.environ.get("AMADEUS_CODE_SHA") or "").strip()
    env_fingerprint = str(
        os.environ.get("AMADEUS_WORKSPACE_FINGERPRINT") or ""
    ).strip()
    env_dirty = str(os.environ.get("AMADEUS_WORKSPACE_DIRTY") or "").strip().lower()
    if env_sha and env_fingerprint:
        return {
            "commit_sha": env_sha,
            "workspace_dirty": env_dirty in {"1", "true", "yes", "on"},
            "workspace_fingerprint": env_fingerprint,
            "source": "launcher",
        }

    def run_git(*args: str) -> bytes:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or f"git {' '.join(args)} failed")
        return result.stdout

    try:
        commit_sha = run_git("rev-parse", "HEAD").decode().strip()
        status = run_git("status", "--porcelain=v1", "-z")
        fingerprint = hashlib.sha256()
        fingerprint.update(b"amadeus-live-runtime-v1\0")
        fingerprint.update(commit_sha.encode("ascii", errors="replace"))
        fingerprint.update(b"\0status\0")
        fingerprint.update(status)
        if status:
            fingerprint.update(b"\0diff\0")
            fingerprint.update(run_git("diff", "--binary", "HEAD", "--", "."))
            untracked = run_git("ls-files", "--others", "--exclude-standard", "-z")
            for raw_name in sorted(value for value in untracked.split(b"\0") if value):
                fingerprint.update(b"\0untracked\0")
                fingerprint.update(raw_name)
                path = root / raw_name.decode("utf-8", errors="surrogateescape")
                if path.is_file():
                    fingerprint.update(b"\0")
                    fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
        return {
            "commit_sha": commit_sha,
            "workspace_dirty": bool(status),
            "workspace_fingerprint": fingerprint.hexdigest(),
            "source": "git",
        }
    except Exception as exc:
        return {
            "commit_sha": "",
            "workspace_dirty": None,
            "workspace_fingerprint": "",
            "source": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }


class RuntimeStatusCollector:
    def __init__(self) -> None:
        self._started_at_monotonic = time.monotonic()
        self._started_at_wall = time.time()
        self._port: int | None = None
        self._code_identity = _runtime_code_identity()
        self._chat_handler = None
        self._asr_handler = None
        self._playback_manager_getter: Callable[[], Any] | None = None
        self._player_getter: Callable[[], Any] | None = None
        self._pending_sentence_items_getter: Callable[[], Any] | None = None
        self._asr_manager_getter: Callable[[], Any] | None = None
        self._wake_service_getter: Callable[[], Any] | None = None
        self._wallpaper_handler = None
        self._provider_runtime_getter: Callable[[], Any] | None = None
        self._provider_availability_getter: Callable[[], Any] | None = None
        self._work_ledger_getter: Callable[[], Any] | None = None

    def configure(
        self,
        *,
        port: int | None = None,
        chat_handler=None,
        asr_handler=None,
        playback_manager_getter: Callable[[], Any] | None = None,
        player_getter: Callable[[], Any] | None = None,
        pending_sentence_items_getter: Callable[[], Any] | None = None,
        asr_manager_getter: Callable[[], Any] | None = None,
        wake_service_getter: Callable[[], Any] | None = None,
        wallpaper_handler=None,
        provider_runtime_getter: Callable[[], Any] | None = None,
        provider_availability_getter: Callable[[], Any] | None = None,
        work_ledger_getter: Callable[[], Any] | None = None,
    ) -> None:
        if port is not None:
            self._port = port
        if chat_handler is not None:
            self._chat_handler = chat_handler
        if asr_handler is not None:
            self._asr_handler = asr_handler
        if playback_manager_getter is not None:
            self._playback_manager_getter = playback_manager_getter
        if player_getter is not None:
            self._player_getter = player_getter
        if pending_sentence_items_getter is not None:
            self._pending_sentence_items_getter = pending_sentence_items_getter
        if asr_manager_getter is not None:
            self._asr_manager_getter = asr_manager_getter
        if wake_service_getter is not None:
            self._wake_service_getter = wake_service_getter
        if wallpaper_handler is not None:
            self._wallpaper_handler = wallpaper_handler
        if provider_runtime_getter is not None:
            self._provider_runtime_getter = provider_runtime_getter
        if provider_availability_getter is not None:
            self._provider_availability_getter = provider_availability_getter
        if work_ledger_getter is not None:
            self._work_ledger_getter = work_ledger_getter

    # ── 快照入口 ─────────────────────────────────────────────────────────────

    def collect(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "ts": time.time(),
            "server": self._section(self._server),
            "session": self._section(self._session),
            "chat": self._section(self._chat),
            "tts": self._section(self._tts),
            "playback": self._section(self._playback),
            "asr": self._section(self._asr),
            "wake": self._section(self._wake),
            "mic": self._section(self._mic),
            "aec": self._section(self._aec),
            "provider": self._section(self._provider),
            "coordinator": self._section(self._coordinator),
        }
        snapshot["ready"] = self._ready(snapshot)
        snapshot["derived"] = self._section(lambda: self._derived(snapshot))
        return snapshot

    @staticmethod
    def _coordinator() -> dict[str, Any]:
        from core.turn_coordinator import get_turn_coordinator

        return get_turn_coordinator().snapshot()

    @staticmethod
    def _section(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:
            logger.debug("status section failed: %s", exc, exc_info=True)
            return {"error": str(exc)}

    # ── 各子系统段 ────────────────────────────────────────────────────────────

    def _server(self) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "port": self._port,
            "started_at": self._started_at_wall,
            "uptime_s": round(time.monotonic() - self._started_at_monotonic, 1),
            "code_identity": dict(self._code_identity),
        }

    def _session(self) -> dict[str, Any]:
        from core import session_manager as sm
        from core.chat_runtime import get_chat_runtime

        sid = sm.get_current_session_id() or ""
        return {
            "current_session_id": sid,
            "title": sm.get_session_title(sid) if sid else "",
            "enable_conversation": bool(get_chat_runtime().enable_conversation),
            "history_len": len(getattr(sm.conversation_history, "dialog", []) or []),
        }

    def _chat(self) -> dict[str, Any]:
        from core.chat_runtime import get_chat_runtime

        rt = get_chat_runtime()
        out: dict[str, Any] = {
            "provider": rt.provider,
            "use_local_llm": bool(rt.use_local_llm),
            "local_llm_type": rt.local_llm_type,
            "control_decision_mode": (
                "authority"
                if bool(getattr(rt, "_control_proposal_authority", False))
                else "shadow"
                if getattr(rt, "_control_proposal_observer", None) is not None
                else "disabled"
            ),
        }
        h = self._chat_handler
        if h is not None:
            out.update({
                "busy": bool(h.is_busy()),
                "chat_epoch": int(getattr(h, "_chat_epoch", -1)),
                "active_turn_id": str(getattr(h, "_active_turn_id", "") or ""),
                "last_assistant_turn_id": str(getattr(h, "_last_assistant_turn_id", "") or ""),
            })
        return out

    def _tts(self) -> dict[str, Any]:
        import tts.pipeline as tts_pipeline

        out: dict[str, Any] = {
            "interrupt_epoch": int(getattr(tts_pipeline, "_tts_interrupt_epoch", -1)),
            "backend_loaded": getattr(tts_pipeline, "_tts_runtime", None) is not None,
            # Compatibility alias for existing diagnostics.
            "inferencer_loaded": getattr(tts_pipeline, "_tts_runtime", None) is not None,
        }
        getter = self._pending_sentence_items_getter
        queue = getter() if getter is not None else None
        if queue is not None:
            out["pending_sentences"] = int(queue.qsize())
        return out

    def _playback(self) -> dict[str, Any]:
        getter = self._playback_manager_getter
        pm = getter() if getter is not None else None
        if pm is None:
            return {"initialized": False}
        player = pm.player if hasattr(pm, "player") else None
        ready = getattr(pm, "player_is_ready", None)
        active = bool(ready is not None and not ready.is_set())
        return {
            "initialized": True,
            "playback_epoch": int(getattr(pm, "playback_epoch", -1)),
            # The PortAudio stream stays open between utterances.  ``is_playing``
            # is the user-visible activity state; expose stream ownership
            # separately so an initialized device is never mistaken for speech.
            "is_playing": active,
            "stream_open": bool(getattr(player, "is_playing", False)),
            "pending_audio": len(getattr(pm, "pending_audio", {}) or {}),
            "next_seq_to_play": int(getattr(pm, "next_seq_to_play", -1)),
        }

    def _asr(self) -> dict[str, Any]:
        from config.settings import (
            ASR_SPECULATIVE_END_MS,
            ASR_SPECULATIVE_TRANSCRIBE,
        )

        out: dict[str, Any] = {
            "speculative_transcribe": bool(ASR_SPECULATIVE_TRANSCRIBE),
            "speculative_end_ms": int(ASR_SPECULATIVE_END_MS),
        }
        h = self._asr_handler
        if h is not None:
            awake_until = float(getattr(h, "_awake_until", 0.0) or 0.0)
            out.update({
                "active": bool(getattr(h, "_active", False)),
                "source": str(getattr(h, "_source", "") or ""),
                "one_shot": bool(getattr(h, "_one_shot", False)),
                "awake_remaining_s": round(max(0.0, awake_until - time.monotonic()), 1),
                "waiting_turn_complete": bool(getattr(h, "_waiting_turn_complete", False)),
            })
        getter = self._asr_manager_getter
        mgr = getter() if getter is not None else None
        out["manager_loaded"] = mgr is not None
        if mgr is not None:
            out.update({
                "backend": str(getattr(mgr, "_backend_name", "") or ""),
                "backend_ready": bool(getattr(mgr, "is_ready", False)),
                "mic_index": getattr(mgr, "_mic_index", None),
            })
            # VAD state comes from the manager's public contract:
            # "ready" (silero) / "fallback" (tier absence) / "degraded"
            # (installed but broken — reason must stay observable).
            vad_status = getattr(mgr, "vad_status", None)
            if callable(vad_status):
                vad_state, vad_reason = vad_status()
                out["vad"] = str(vad_state)
                if vad_reason:
                    out["vad_degraded"] = str(vad_reason)
        return out

    def _wake(self) -> dict[str, Any]:
        getter = self._wake_service_getter
        svc = getter() if getter is not None else None
        if svc is None:
            return {"initialized": False}
        status = svc.status() if hasattr(svc, "status") else {}
        return {"initialized": True, **(status or {})}

    def _mic(self) -> dict[str, Any]:
        # 只 peek 单例，绝不触发构造
        import asr.mic_input_service as mic_mod

        svc = getattr(mic_mod, "_INSTANCE", None)
        if svc is None:
            return {"initialized": False}
        out: dict[str, Any] = {
            "initialized": True,
            "running": bool(getattr(svc, "running", False)),
            "mic_index": getattr(svc, "mic_index", None),
        }
        device = getattr(svc, "device", None)
        if device is not None:
            out.update({
                "mic_index": getattr(device, "index", out["mic_index"]),
                "device_name": str(getattr(device, "name", "") or ""),
                "host_api": str(getattr(device, "host_api", "") or ""),
                "device_class": str(getattr(device, "device_class", "unknown") or "unknown"),
                "max_input_channels": int(getattr(device, "max_input_channels", 0) or 0),
                "default_sample_rate": float(getattr(device, "default_sample_rate", 0.0) or 0.0),
            })
        return out

    def _aec(self) -> dict[str, Any]:
        from config.settings import (
            AEC_REALTIME_BARGE_IN,
            AEC_REALTIME_DELAY_MS,
            AEC_REALTIME_ENABLED,
        )
        import tts.aec_realtime as aec_mod

        out: dict[str, Any] = {
            "config_enabled": bool(AEC_REALTIME_ENABLED),
            "config_barge_in": bool(AEC_REALTIME_BARGE_IN),
            "config_delay_ms": float(AEC_REALTIME_DELAY_MS),
        }
        inst = getattr(aec_mod, "_INSTANCE", None)
        out["initialized"] = inst is not None
        if inst is not None:
            out.update({
                "enabled": bool(getattr(inst, "enabled", False)),
                "barge_in_enabled": bool(getattr(inst, "barge_in_enabled", False)),
                "delay_ms": float(getattr(inst, "_delay_ms", -1.0)),
                "device_class": str(getattr(inst, "_device_class", "unknown") or "unknown"),
            })
        return out

    def _provider(self) -> dict[str, Any]:
        provider_runtime = self._peek_provider_runtime()

        runs = provider_runtime.list_runs() or []
        active = [
            r for r in runs
            if str(r.get("status") or "").lower()
            not in {"completed", "failed", "cancelled", "canceled", "error", "done", "orphaned"}
        ]
        return {
            "providers": provider_runtime.list_providers(),
            "availability": (
                self._provider_availability_getter()
                if self._provider_availability_getter is not None
                else []
            ),
            "configured": callable(getattr(provider_runtime, "_request_preparer", None)),
            "active_runs": active,
            "recent_runs": runs[:5],
        }

    def _peek_provider_runtime(self):
        if self._provider_runtime_getter is not None:
            return self._provider_runtime_getter()
        from agent_host.provider_runtime import runtime as provider_runtime

        return provider_runtime

    def _peek_work_ledger(self):
        if self._work_ledger_getter is not None:
            return self._work_ledger_getter()
        from server.work_ledger_coordinator import get_work_ledger_coordinator

        return get_work_ledger_coordinator()

    @staticmethod
    def _ready_probe(fn: Callable[[], Any]) -> bool:
        try:
            return bool(fn())
        except Exception:
            logger.debug("readiness probe failed", exc_info=True)
            return False

    def _ready(self, snapshot: dict[str, Any]) -> dict[str, bool]:
        tts_ready = self._ready_probe(
            lambda: bool((snapshot.get("tts") or {}).get("inferencer_loaded"))
            and bool((snapshot.get("playback") or {}).get("initialized"))
        )
        asr_ready = self._ready_probe(
            lambda: bool((snapshot.get("asr") or {}).get("manager_loaded"))
            and bool((snapshot.get("asr") or {}).get("backend_ready"))
            # Installed-but-broken VAD silently loses barge-in, so ASR is not
            # fully ready; the L2 fallback tier is the designed shape and
            # stays ready.
            and (snapshot.get("asr") or {}).get("vad") != "degraded"
        )
        wake_ready = self._ready_probe(
            lambda: bool((snapshot.get("wake") or {}).get("initialized"))
            and bool((snapshot.get("wake") or {}).get("running"))
            and str((snapshot.get("wake") or {}).get("status") or "")
            in {"listening", "awake_bridge", "already_running"}
            and not bool((snapshot.get("wake") or {}).get("error"))
        )
        wallpaper_bridge_ready = self._ready_probe(
            lambda: bool(
                getattr(
                    getattr(self._wallpaper_handler, "_wallpaper_host", None),
                    "_ready",
                    False,
                )
            )
        )
        provider_runtime_ready = self._ready_probe(
            lambda: bool(self._peek_provider_runtime().list_providers())
            and callable(
                getattr(self._peek_provider_runtime(), "_request_preparer", None)
            )
        )
        work_ledger_ready = self._ready_probe(
            lambda: (
                (ledger := self._peek_work_ledger()) is not None
                and bool(getattr(ledger, "_subscribed", False))
                and getattr(ledger, "store", None) is not None
            )
        )
        ready = {
            "tts": tts_ready,
            "asr": asr_ready,
            "wake": wake_ready,
            "wallpaper_bridge": wallpaper_bridge_ready,
            "provider_runtime": provider_runtime_ready,
            "work_ledger": work_ledger_ready,
        }
        ready["overall"] = all(ready.values())
        return ready

    # ── 派生模式（启发式，只读汇总，不是所有权声明）───────────────────────────

    def _derived(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        # 协调器已观察到事件时，用它的账本作为权威分类
        coord = snapshot.get("coordinator") or {}
        if coord.get("initialized"):
            return {
                "output_mode": coord.get("output_mode"),
                "asr_mode": coord.get("asr_mode"),
                "source": "turn_coordinator",
            }

        chat = snapshot.get("chat") or {}
        playback = snapshot.get("playback") or {}
        asr = snapshot.get("asr") or {}
        wake = snapshot.get("wake") or {}

        if playback.get("is_playing"):
            output_mode = "playing"
        elif chat.get("busy"):
            output_mode = "llm_streaming"
        elif (playback.get("pending_audio") or 0) > 0:
            output_mode = "tts_synthesizing"
        else:
            output_mode = "idle"

        if asr.get("active"):
            asr_mode = "awake_hot" if (asr.get("awake_remaining_s") or 0) > 0 else "listening"
        elif wake.get("running"):
            asr_mode = "wake_listening"
        else:
            asr_mode = "sleeping"

        return {
            "output_mode": output_mode,
            "asr_mode": asr_mode,
            "source": "heuristic",
            "note": "no coordinator events observed yet; heuristic fallback",
        }


# 进程级单例
status_collector = RuntimeStatusCollector()
