"""
Backend service entry point - initializes all subsystems and starts the
WebSocket server. Mirrors the initialization in main.py's main() but
without any GUI (PyQt5 / Tkinter) dependencies.

Usage:
    python -m server.app              # default port 17777
    python -m server.app --port 9077  # custom port
"""

import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import mimetypes
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

from tts.pre_translation_runtime import (
    configured_default_enabled as _configured_pre_translation_default,
)
from tts.pre_translation_runtime import runtime as pre_translation_runtime


def _force_utf8_console_io() -> None:
    """Best-effort UTF-8 stdout/stderr setup for Windows consoles."""
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_console_io()


_BASE_PRE_TRANSLATION_ENABLED = _configured_pre_translation_default()


def _pre_translation_enabled() -> bool:
    env_enabled = _BASE_PRE_TRANSLATION_ENABLED
    if env_enabled:
        return True
    try:
        from server import wallpaper_subtitle_runtime

        return wallpaper_subtitle_runtime.needs_translation()
    except Exception:
        return False


def _observer_display_language() -> str:
    """Primary language for host-authored Chat and work reports."""

    from server.assistant_language import current_assistant_language

    return current_assistant_language()


# force the project root onto sys.path.
ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.log_encoding import install_mojibake_repair_filter, install_stdio_mojibake_repair
from server.local_auth import LocalAuthPolicy, clear_inherited_auth_environment
from config import settings
from config.log_privacy import protected_text
from server.work_steer_control import route_active_amendment as _route_active_amendment

install_stdio_mojibake_repair()

# Run main.py in headless mode - skip PyQt5/chatGui/floating_subtitle imports
os.environ["AMADEUS_HEADLESS"] = "1"

# CUDA DLL preload.
for _cuda_ver in ("v12.1", "v12.2", "v12.4", "v12.6"):
    _cuda_bin = f"C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/{_cuda_ver}/bin"
    if os.path.isdir(_cuda_bin):
        os.add_dll_directory(_cuda_bin)
        break
# Eager-load onnxruntime before the torchmetrics chain on local-model installs
# (CUDA DLL ordering). It is a T2b (local-cu124) dependency, so skip silently on
# headless/voice-less installs where neither onnxruntime nor torchmetrics exist.
import importlib.util as _importlib_util

try:
    if _importlib_util.find_spec("onnxruntime") is not None:
        import onnxruntime  # noqa: F401  # eager before torchmetrics chain
except ImportError:  # local-model stack absent in T1 installs
    pass

def _session_log_path() -> str:
    """One log file per process start, under runtime/logs.

    The old relative "server.log" meant every process that imported this module
    with the repo root as cwd appended to the same file -- including the test
    suite and the probes. On 2026-08-02 that made a live session's log contain
    `consumer=test` and `run_id=r1` lines from a regression run happening at the
    same time, and reading them as the session's own behaviour sent one
    diagnosis down the wrong path before the timestamps gave it away.

    Keeps the newest few and prunes the rest: these are diagnostic scratch, and
    an unbounded pile of them is its own kind of mess.
    """

    # An explicit path wins, and callers that need to find the log afterwards
    # should use it rather than relying on the process's cwd -- that reliance
    # is exactly what let two processes share one file.
    explicit = str(os.environ.get("AMADEUS_SERVER_LOG") or "").strip()
    if explicit:
        parent = os.path.dirname(os.path.abspath(explicit))
        if parent:
            os.makedirs(parent, exist_ok=True)
        return explicit

    log_dir = os.path.join(ROOT, "runtime", "logs")
    os.makedirs(log_dir, exist_ok=True)
    try:
        existing = sorted(
            (name for name in os.listdir(log_dir) if name.startswith("server_")),
            reverse=True,
        )
        for stale in existing[19:]:
            try:
                os.remove(os.path.join(log_dir, stale))
            except OSError:
                pass
    except OSError:
        pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(log_dir, f"server_{stamp}_{os.getpid()}.log")


_SESSION_LOG_PATH = _session_log_path()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            _SESSION_LOG_PATH,
            maxBytes=8 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        ),
    ],
)
install_mojibake_repair_filter()
logger = logging.getLogger("server")
logger.info("session log: %s", _SESSION_LOG_PATH)

# silence noisy libraries
for lib in ("httpx", "websocket", "faiss", "sentence_transformers",
            "numexpr", "PIL", "boto3", "urllib3", "asyncio"):
    logging.getLogger(lib).setLevel(logging.WARNING)


# runtime singletons (populated by bootstrap).
vts_manager = None
player = None
playback_manager = None
tts_runtime = None
asr_manager = None
wake_service = None
_asr_manager_lock = threading.Lock()
_wake_service_lock = threading.Lock()
tts_executor = None
translation_executor = None
pending_actions = None
pending_sentence_items = None
exp_tts_semaphore = None
exp_play_condition = None
# True when neither chat streaming nor TTS playback is active. Populated by
# bootstrap from the same signals the WorkObserver trusts for narration
# timing; None (headless/smoke) means there is nothing to wait for.
output_idle_probe = None
# Host-authored read-only replies use the same character voice sink as branch
# replies, but never invoke the main conversation model.
host_readonly_voice_sink = None
# The Work Observer remains the single character-expression owner for
# resolved WorkItem status questions. Populated by bootstrap; lookup itself
# continues to own only identity and ledger facts.
work_status_narrator = None
# Focus is applied synchronously at the dispatcher boundary, while its spoken
# post-condition waits for the shared character lane in a tracked background
# task.  This keeps a compound "switch and edit" from delaying Provider start.
_focus_confirmation_tasks: set[asyncio.Task] = set()
_auip_preparation_tasks: set[asyncio.Task] = set()


def _websocket_origin_allowed(
    origin: str | None,
    *,
    backend_port: int,
    user_agent: str | None = None,
) -> bool:
    """Allow only the browser origins that Amadeus actually owns.

    Native clients normally omit ``Origin``. Packaged Electron uses a file or
    opaque origin, while development Electron is served by the fixed Vite
    origin. Opaque origins are accepted only with Chromium's Electron UA.
    """

    value = str(origin or "").strip()
    if not value:
        return True
    if value == "null":
        # Chromium serializes packaged file:// Electron pages as an opaque
        # null origin.  A normal website can also manufacture null with a
        # sandboxed iframe, but it cannot choose the WebSocket User-Agent.
        # This is a compatibility boundary until the app adopts a signed
        # custom scheme or per-launch WebSocket token.
        return "Electron/" in str(user_agent or "")
    return value in {
        "file://",
        "http://localhost:5173",
        f"http://127.0.0.1:{int(backend_port)}",
    }


async def _handle_websocket_connection(
    ws,
    manager,
    *,
    backend_port: int,
    ready=True,
    auth_policy: LocalAuthPolicy | None = None,
) -> bool:
    """Accept an owned socket only after origin, identity, and readiness checks."""

    origin = ws.headers.get("origin")
    if not _websocket_origin_allowed(
        origin,
        backend_port=backend_port,
        user_agent=ws.headers.get("user-agent"),
    ):
        logger.warning("rejected websocket origin: %s", origin)
        await ws.close(code=1008, reason="untrusted websocket origin")
        return False
    policy = auth_policy or LocalAuthPolicy.disabled()
    if policy.authenticate(ws.headers, allow_websocket_protocol=True) is None:
        logger.warning("rejected unauthenticated desktop websocket")
        await ws.close(code=1008, reason="authentication required")
        return False
    is_ready = ready() if callable(ready) else ready
    if not bool(is_ready):
        await ws.close(code=1013, reason="backend starting")
        return False
    selected_protocol = policy.selected_websocket_subprotocol(ws.headers)
    if selected_protocol:
        await manager.handle_connection(ws, subprotocol=selected_protocol)
    else:
        await manager.handle_connection(ws)
    return True


def _http_request_origin_allowed(headers, *, backend_port: int) -> bool:
    """Reject browser cross-site mutations while retaining native clients."""

    origin = headers.get("origin")
    if origin:
        return _websocket_origin_allowed(
            origin,
            backend_port=backend_port,
            user_agent=headers.get("user-agent"),
        )
    # Native sidecars do not send browser fetch metadata. A browser that
    # suppresses Origin still declares a cross-site fetch here.
    return str(headers.get("sec-fetch-site") or "").lower() != "cross-site"


def _http_request_authenticated(headers, auth_policy: LocalAuthPolicy) -> bool:
    """Authenticate a local mutation without assigning product authority."""

    return auth_policy.authenticate(headers) is not None


# bootstrap.

async def bootstrap(port: int = 17777) -> None:
    """Mirrors main.py's main() init sequence, minus GUI."""
    global vts_manager, player, playback_manager, tts_runtime, asr_manager, wake_service
    global tts_executor, translation_executor, pending_actions, pending_sentence_items
    global exp_tts_semaphore, exp_play_condition, output_idle_probe, host_readonly_voice_sink
    global work_status_narrator

    auth_policy = LocalAuthPolicy.from_environment(os.environ)
    clear_inherited_auth_environment(os.environ)
    if auth_policy.required:
        logger.info("local desktop authentication enabled")
    else:
        logger.warning(
            "local desktop authentication disabled; direct loopback development mode"
        )

    e2e_no_tts = str(os.environ.get("AMADEUS_E2E_NO_TTS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # config.
    from config.settings import (
        VTS_WS_URL, VTS_TOKEN_FILE,
        VTS_ENABLED, VTS_HEARTBEAT_ENABLED,
        AEC_REALTIME_BARGE_IN,
        AEC_REALTIME_ENABLED,
        ASR_IDLE_UNLOAD_SECONDS,
        ASR_ECHO_TAIL_GUARD_MS,
        LOCAL_LLM_LAUNCH_MODE, LOCAL_LLM_TYPE, LLM_PROVIDER,
        EXP_TTS_MAX_CONCURRENCY, TTS_BACKEND,
        WAKE_AWAKE_SECONDS,
        WAKE_AUTO_SEND_TO_CHAT,
        WAKE_BRIDGE_AUTO_SEND,
        WAKE_ENABLED,
    )

    # wire handler registration.
    from server.ws_handler import manager as _mgr
    from server.protocol import Method
    from server.event_bus import bus
    from server.handlers.chat_handler import ChatHandler
    from server.handlers.session_handler import SessionHandler
    from server.handlers.tts_handler import TtsHandler
    from server.handlers.asr_handler import AsrHandler
    from server.handlers.wake_handler import WakeHandler
    from server.handlers.vts_handler import VtsHandler
    from server.handlers.expression_handler import ExpressionHandler
    from server.handlers.system_handler import SystemHandler
    from server.handlers.render_handler import RenderHandler
    from server.handlers.wallpaper_handler import WallpaperHandler
    from server.handlers.provider_handler import ProviderHandler
    from server.handlers.capability_handler import CapabilityHandler
    from server.handlers.mcp_connection_handler import McpConnectionHandler
    from server.handlers.provider_activity_handler import ProviderActivityHandler
    from server.handlers.work_activity_handler import WorkActivityCoordinator
    from server.handlers.work_ledger_handler import WorkLedgerHandler
    from server.work_preview import WorkPreviewHandler, WorkPreviewManager
    from server.handlers.auip_handler import AuipHandler
    from server.auip_launch import (
        AuipLaunchCoordinator,
        set_auip_launch_coordinator,
    )
    from server.capability_catalog import CapabilityCatalog
    from server.capability_composition import (
        auip_app_capability_packages,
        builtin_auip_authoring_package,
    )
    from server.auip_app_connection import manager as auip_app_manager
    from server.auip_runtime import runtime as auip_runtime
    from server.auip_self_attach import AuipSelfAttachCoordinator
    from server.handlers.vn_player_handler import VNPlayerHandler
    from server.handlers.vn_launch_handler import VNLaunchHandler
    from server.work_observer import WorkObserverCoordinator
    from server.canvas_action_router import CanvasActionRouter
    from server.interaction_branch import InteractionBranchCoordinator
    from server.work_ledger_coordinator import WorkLedgerCoordinator
    from agent_host.work_ledger_store import WorkLedgerStore
    from agent_host.provider_runtime import runtime as provider_runtime
    from agent_host.provider_activity_journal import ProviderActivityJournal

    # Bind the serving loop before anything can emit: sync code dispatched to a
    # worker thread (control-plane intake) reaches subscribers only through it.
    bus.bind_loop()

    # create handlers & register methods FIRST.
    # Handlers are registered before uvicorn starts so that methods are
    # recognized immediately. Runtime deps are injected later via configure().
    chat_h = ChatHandler()
    session_h = SessionHandler()
    tts_h = TtsHandler()
    asr_h = AsrHandler()
    wake_h = WakeHandler()
    vts_h = VtsHandler()
    expr_h = ExpressionHandler()
    sys_h = SystemHandler()
    render_h = RenderHandler()
    wallpaper_h = WallpaperHandler()
    capability_catalog = CapabilityCatalog()
    capability_catalog.register_package(builtin_auip_authoring_package())
    provider_h = ProviderHandler(capability_catalog=capability_catalog)
    mcp_connection_h = McpConnectionHandler(provider_h.mcp_connections)
    configured_activity_path = str(
        os.environ.get("AMADEUS_PROVIDER_ACTIVITY_PATH") or ""
    ).strip()
    provider_activity_h = ProviderActivityHandler(
        ProviderActivityJournal(
            Path(configured_activity_path).expanduser()
            if configured_activity_path
            else Path(ROOT) / "runtime" / "provider_activity.jsonl"
        )
    )
    configured_ledger_path = str(os.environ.get("AMADEUS_WORK_LEDGER_PATH") or "").strip()
    work_ledger_store = WorkLedgerStore(
        Path(configured_ledger_path).expanduser()
        if configured_ledger_path
        else Path(ROOT) / "runtime" / "work_ledger.sqlite3"
    )
    from core import session_manager as _work_session_manager

    work_ledger = WorkLedgerCoordinator(
        work_ledger_store,
        provider_start=provider_runtime.start,
        provider_cancel=provider_runtime.cancel,
        current_session_id=_work_session_manager.get_current_session_id,
    )
    session_h.configure(
        work_coordinator=work_ledger,
        is_chat_busy=chat_h.is_busy,
    )
    provider_h.configure_work_control(work_ledger)
    work_preview = WorkPreviewManager(work_ledger_store)
    work_preview_h = WorkPreviewHandler(work_ledger, work_preview)
    work_h = WorkLedgerHandler(
        work_ledger,
        provider_run=provider_h.run_provider,
        provider_permission=provider_runtime.resolve_permission,
        preview_open=work_preview_h.open_from_work_action,
    )
    from core import session_manager as _attention_session_manager
    from server.attention_request import attention_requests
    from server.handlers.attention_handler import AttentionRequestHandler

    attention_h = AttentionRequestHandler(
        attention_requests,
        current_session_id=lambda: _attention_session_manager.get_current_session_id() or "",
    )
    auip_launch = AuipLaunchCoordinator(
        artifacts=work_ledger_store,
        work_roster=work_ledger,
        attention=attention_requests,
    )
    capability_h = CapabilityHandler(
        capability_catalog,
        extra_packages=lambda: auip_app_capability_packages(
            auip_launch.candidates(
                _attention_session_manager.get_current_session_id() or ""
            )
        ),
    )
    set_auip_launch_coordinator(auip_launch)
    auip_h = AuipHandler(
        artifacts=work_ledger_store,
        current_session_id=lambda: _attention_session_manager.get_current_session_id() or "",
        app_websocket_url=f"ws://127.0.0.1:{port}/auip/ws",
        launch=auip_launch,
        preview_handoff=work_preview.begin_auip_handoff,
    )
    auip_app_manager.configure_self_attach(
        AuipSelfAttachCoordinator(
            runtime=auip_runtime,
            artifacts=work_ledger_store,
            attention=attention_requests,
            current_session_id=(
                lambda: _attention_session_manager.get_current_session_id() or ""
            ),
        )
    )
    auip_narration_callback = None
    auip_narration = None
    auip_presentation_callback = None
    auip_engagement = None
    auip_engagement_callback = None
    auip_launch_callback = auip_launch.on_work_updated
    bus.on(Method.WORK_UPDATED, auip_launch_callback)
    work_preview_auip_callback = work_preview.on_auip_updated
    bus.on(Method.AUIP_UPDATED, work_preview_auip_callback)
    provider_runtime.set_request_preparer(work_ledger.prepare_request)
    work_ledger.adopt_runtime_records(provider_runtime.list_runs())
    work_ledger.configure()
    work_activity = WorkActivityCoordinator()
    work_observer = WorkObserverCoordinator()
    canvas_action_router = CanvasActionRouter(
        provider_run=provider_h.run_provider,
        work_action=work_h.route_action,
        provider_inspect=work_ledger.route_provider_inspection,
        context_action=session_h.route_context_action,
        attention_action=attention_h.route_canvas_action,
    )
    interaction_branch = InteractionBranchCoordinator(
        provider_run=provider_h.run_provider,
        provider_steer=provider_h.steer_provider,
        display_language=_observer_display_language,
    )
    vn_h = VNPlayerHandler()
    vn_launch_h = VNLaunchHandler()

    for h in (chat_h, session_h, tts_h, asr_h, wake_h, vts_h, expr_h, sys_h, render_h, wallpaper_h, provider_h, capability_h, mcp_connection_h, provider_activity_h, work_h, work_preview_h, attention_h, auip_h, vn_h, vn_launch_h):
        _mgr.register_handler(h)

    # create FastAPI app.
    from fastapi import FastAPI, HTTPException, Request, WebSocket
    from fastapi.responses import FileResponse, JSONResponse
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    import uvicorn

    app = FastAPI(title="amadeus-backend", version="0.1.0")

    # Uvicorn binds before the rest of the product runtime is configured.  A
    # 200 response is useful for process discovery during that window, but it
    # must not tell desktop/E2E clients that chat routing is ready yet.
    backend_ready = False
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost"],
    )

    def _project_file_response(base: Path, rel_path: str):
        root = base.resolve()
        target = (root / rel_path).resolve()
        if root != target and root not in target.parents:
            raise HTTPException(status_code=404, detail="Not Found")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        media_type = mimetypes.guess_type(str(target))[0]
        return FileResponse(target, media_type=media_type)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await _handle_websocket_connection(
            ws,
            _mgr,
            backend_port=port,
            ready=lambda: backend_ready,
            auth_policy=auth_policy,
        )

    @app.websocket("/auip/ws")
    async def auip_ws_endpoint(ws: WebSocket):
        # This endpoint deliberately does not share the main connection
        # manager or its event subscriptions.  A one-time attach ticket is
        # the connection's authority; the per-connection handler exposes only
        # the cooperative-app method allowlist.
        # AUIP applications authenticate with a one-time attach ticket after
        # connection and never receive the desktop instance credential. They
        # still share the exact browser-origin and readiness boundary.
        await _handle_websocket_connection(
            ws,
            auip_app_manager,
            backend_port=port,
            ready=lambda: backend_ready,
            auth_policy=LocalAuthPolicy.disabled(),
        )

    @app.get("/health")
    async def health():
        from core.chat_runtime import get_chat_runtime

        chat_runtime = get_chat_runtime()
        return {
            "status": "ok" if backend_ready else "starting",
            **auth_policy.health_fields(),
            "vts_connected": vts_manager.connected if vts_manager else False,
            "control_decision_mode": (
                "authority"
                if bool(getattr(chat_runtime, "_control_proposal_authority", False))
                else "shadow"
                if getattr(chat_runtime, "_control_proposal_observer", None) is not None
                else "disabled"
            ),
        }

    @app.get("/runtime/status")
    async def runtime_status():
        from server.runtime_status import status_collector

        snapshot = await asyncio.to_thread(status_collector.collect)
        return JSONResponse(snapshot)

    @app.post("/shutdown")
    async def shutdown(request: Request):
        if not _http_request_authenticated(request.headers, auth_policy):
            raise HTTPException(status_code=401, detail="Authentication required")
        if not _http_request_origin_allowed(request.headers, backend_port=port):
            raise HTTPException(status_code=403, detail="Untrusted request origin")
        logger.info("backend shutdown requested")

        async def _request_exit() -> None:
            await asyncio.sleep(0.05)
            server.should_exit = True

        asyncio.create_task(_request_exit())
        return {"ok": True}

    @app.post("/vn/speak")
    async def vn_speak(payload: dict, request: Request):
        if not _http_request_authenticated(request.headers, auth_policy):
            raise HTTPException(status_code=401, detail="Authentication required")
        if not _http_request_origin_allowed(request.headers, backend_port=port):
            raise HTTPException(status_code=403, detail="Untrusted request origin")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload must be an object")
        result = await _speak_vn_reaction(payload)
        return {"ok": True, **(result or {})}

    @app.get("/wallpaper/bridge-info")
    async def wallpaper_bridge_info():
        # This is a discovery/health endpoint, not the bridge transport.
        # Manual-off is a healthy state and is represented by
        # ``running: false``; returning 503 made Lively's harmless watchdog
        # polling look like a backend failure in every access log.
        return wallpaper_h.bridge_info()

    @app.get("/render/web/{rel_path:path}")
    async def render_web_asset(rel_path: str):
        return _project_file_response(Path(ROOT) / "render" / "web", rel_path)

    @app.get("/assets/{rel_path:path}")
    async def project_asset(rel_path: str):
        return _project_file_response(Path(ROOT) / "assets", rel_path)

    @app.get("/wallpaper/lively/{rel_path:path}")
    async def lively_asset(rel_path: str):
        return _project_file_response(Path(ROOT) / "wallpaper" / "lively", rel_path)

    # start uvicorn in background.
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.1)  # let server bind

    # TTS.
    if e2e_no_tts or TTS_BACKEND == "disabled":
        tts_runtime = None
        logger.info(
            "TTS disabled (%s)",
            "isolated E2E conversation test" if e2e_no_tts else "TTS_BACKEND=disabled",
        )
    else:
        try:
            from tts.registry import create_tts_runtime

            tts_runtime = create_tts_runtime(TTS_BACKEND)
            logger.info("tts backend ready: %s", TTS_BACKEND)
        except Exception as e:
            logger.warning("tts init failed (expected if model files missing): %s", e)
            tts_runtime = None

    # VTS.
    from vts.connection_manager import VTSConnectionManager as _VTSMgr
    vts_manager = _VTSMgr(VTS_WS_URL, token_file=VTS_TOKEN_FILE)
    from tts.mouth_signal import MouthSignalRouter

    mouth_signal_router = MouthSignalRouter(
        compatibility_sink=vts_manager.send_mouth_data,
    )

    # audio player.
    from tts.playback import StreamPlayerWithBuffer as _SPB, PlaybackManager as _PM, SubtitleHooks as _SH
    from server import presentation_runtime, wallpaper_subtitle_runtime

    def _update_wallpaper_subtitle(japanese_text: str, chinese_text: str = "") -> None:
        # Wallpaper subtitles are a display layer. Chat bubbles and chat memory
        # keep the original assistant text; only this surface can switch
        # between Japanese, Chinese, bilingual, or hidden captions.
        wallpaper_subtitle_runtime.update(japanese_text, chinese_text)

    wallpaper_subtitle_runtime.set_renderer(lambda text: wallpaper_h.set_subtitle(text))
    presentation_runtime.set_renderer(
        lambda profile: wallpaper_h.set_canvas_presentation(profile)
    )

    def _is_vn_playback_sentence(sentence_id: str) -> bool:
        try:
            from server.vn_tts_bridge import is_vn_sentence

            return bool(is_vn_sentence(sentence_id))
        except Exception:
            return False

    async def _server_display_chinese_subtitle_with_text(
        sentence_id: str,
        japanese_text: str,
        chinese_text: str,
    ) -> None:
        is_vn_sentence = _is_vn_playback_sentence(sentence_id)
        try:
            current_id = getattr(playback_manager, "current_playing_id", None)
            is_current_sentence = current_id == sentence_id
            if not is_current_sentence and playback_manager is not None:
                checker = getattr(playback_manager, "is_current_playback_sentence", None)
                if callable(checker):
                    is_current_sentence = bool(checker(sentence_id))
            if (is_vn_sentence and not is_current_sentence) or (
                not is_vn_sentence and current_id and not is_current_sentence
            ):
                logger.info(
                    "skip subtitle update for stale sentence: playing=%s incoming=%s",
                    current_id,
                    sentence_id,
                )
                return
        except Exception:
            logger.debug("subtitle current sentence guard failed", exc_info=True)
        if is_vn_sentence:
            _update_wallpaper_subtitle(japanese_text, chinese_text)
            try:
                from server.vn_tts_bridge import publish_overlay_subtitle

                await publish_overlay_subtitle(sentence_id, japanese_text, chinese_text)
            except Exception:
                logger.exception("vn overlay subtitle publish failed")
            try:
                await bus.emit(Method.RENDER_SUBTITLE, {"text": wallpaper_subtitle_runtime.current_text(), "source": "vn_player"})
            except Exception:
                logger.exception("server subtitle emit failed")
            return
        _update_wallpaper_subtitle(japanese_text, chinese_text)
        try:
            from server.vn_tts_bridge import publish_overlay_subtitle

            await publish_overlay_subtitle(sentence_id, japanese_text, chinese_text)
        except Exception:
            logger.exception("vn overlay subtitle publish failed")
        try:
            await bus.emit(Method.RENDER_SUBTITLE, {"text": wallpaper_subtitle_runtime.current_text(), "source": "vn_player"})
        except Exception:
            logger.exception("server subtitle emit failed")

    async def _server_check_and_display_pre_translation(sentence_id: str, japanese_text: str) -> None:
        try:
            try:
                from server.vn_tts_bridge import get_vn_subtitle, is_vn_sentence

                if is_vn_sentence(sentence_id):
                    cached = await get_vn_subtitle(sentence_id, japanese_text)
                    if cached and cached.get("status") == "completed" and cached.get("chinese"):
                        await _server_display_chinese_subtitle_with_text(
                            sentence_id,
                            japanese_text,
                            str(cached.get("chinese") or ""),
                        )
                        return
                    await _server_display_chinese_subtitle_with_text(
                        sentence_id,
                        japanese_text,
                        "",
                    )

                    async def _wait_for_vn_subtitle() -> None:
                        for _ in range(120):
                            await asyncio.sleep(0.1)
                            data = await get_vn_subtitle(sentence_id, japanese_text)
                            if data and data.get("status") == "completed" and data.get("chinese"):
                                await _server_display_chinese_subtitle_with_text(
                                    sentence_id,
                                    japanese_text,
                                    str(data.get("chinese") or ""),
                                )
                                return

                    asyncio.create_task(_wait_for_vn_subtitle())
                    return
            except Exception:
                logger.debug("vn subtitle probe failed", exc_info=True)

            if not _pre_translation_enabled():
                return

            cached = await pre_translation_cache.get_translation(japanese_text)
            if cached and cached.get("status") == "completed" and cached.get("chinese"):
                await _server_display_chinese_subtitle_with_text(
                    sentence_id,
                    japanese_text,
                    str(cached.get("chinese") or ""),
                )
            else:
                _update_wallpaper_subtitle(japanese_text, "")
                async def _wait_for_translation() -> None:
                    for _ in range(120):
                        await asyncio.sleep(0.1)
                        data = await pre_translation_cache.get_translation(japanese_text)
                        if data and data.get("status") == "completed" and data.get("chinese"):
                            await _server_display_chinese_subtitle_with_text(
                                sentence_id,
                                japanese_text,
                                str(data.get("chinese") or ""),
                            )
                            return

                asyncio.create_task(_wait_for_translation())
        except Exception:
            logger.exception("server pre-translation display failed")

    from tts.sentence_state import pre_translation_cache

    player = _SPB(
        mouth_signal_router,
        hooks=_SH(
            check_and_display_pre_translation=_server_check_and_display_pre_translation,
            display_chinese_subtitle_with_text=_server_display_chinese_subtitle_with_text,
            get_translation=None,
            cache_lock=None,
            cache_ref=None,
            update_subtitle_display=_update_wallpaper_subtitle,
            subtitle_available=True,
        ),
    )
    playback_manager = _PM(player)

    _last_raw_tts_active_at = 0.0

    def _tts_is_raw_playing() -> bool:
        try:
            return bool(
                playback_manager
                and getattr(playback_manager, "player_is_ready", None)
                and not playback_manager.player_is_ready.is_set()
            )
        except Exception:
            return False

    def _tts_should_block_mic() -> bool:
        nonlocal _last_raw_tts_active_at
        now = time.monotonic()
        if _tts_is_raw_playing():
            _last_raw_tts_active_at = now
            return True
        guard_s = max(0.0, float(ASR_ECHO_TAIL_GUARD_MS) / 1000.0)
        return guard_s > 0 and (now - _last_raw_tts_active_at) < guard_s

    def _tts_is_observer_output_busy() -> bool:
        try:
            try:
                from server.vn_tts_bridge import is_vn_tts_busy

                if is_vn_tts_busy():
                    return True
            except Exception:
                logger.exception("VN TTS busy probe failed")
                return True
            if _tts_is_raw_playing():
                return True
            if pending_sentence_items is not None and not pending_sentence_items.empty():
                return True
            if playback_manager and getattr(playback_manager, "player_is_ready", None):
                return not playback_manager.player_is_ready.is_set()
        except Exception:
            logger.exception("observer TTS busy probe failed")
            return True
        return False

    def _barge_in_enabled() -> bool:
        return bool(AEC_REALTIME_ENABLED and AEC_REALTIME_BARGE_IN)

    logger.info(
        "aec realtime enabled=%s barge_in=%s delay_ms=%s tail_guard_ms=%s",
        AEC_REALTIME_ENABLED,
        AEC_REALTIME_BARGE_IN,
        os.environ.get("AEC_REALTIME_DELAY_MS", ""),
        ASR_ECHO_TAIL_GUARD_MS,
    )

    # expression controller.
    from vts.expression_controller import get_controller as _get_expr
    _expr_ctrl = _get_expr()
    _expr_ctrl.configure(vts_manager=vts_manager)
    server_loop = asyncio.get_running_loop()
    _last_barge_in_interrupt_at = 0.0

    def _env_truthy(name: str, default: bool = False) -> bool:
        value = os.environ.get(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    async def _vn_runtime_active() -> bool:
        try:
            status = await vn_h.handle(Method.VN_STATUS, {})
            return str((status or {}).get("status") or "").strip().lower() == "active"
        except Exception:
            logger.exception("failed to check VN runtime status")
            return False

    async def _main_voice_allowed_now(source: str) -> bool:
        if _env_truthy("AMADEUS_ALLOW_MAIN_VOICE_DURING_VN", False):
            return True
        if await _vn_runtime_active():
            logger.info("%s ignored by main voice route while VN runtime is active", source)
            return False
        return True

    async def _interrupt_for_barge_in() -> None:
        nonlocal _last_barge_in_interrupt_at
        if not await _main_voice_allowed_now("barge-in"):
            return
        now = time.monotonic()
        if now - _last_barge_in_interrupt_at < 0.75:
            return
        _last_barge_in_interrupt_at = now
        logger.info("barge-in speech detected; aborting current chat/TTS turn")
        try:
            barge_in_detector.stop()
        except Exception:
            pass
        # 复合打断（chat abort → TTS interrupt）走统一编排器，
        # 序列语义与原内联实现逐行一致（见 server/interrupt_flow.py）。
        from server.interrupt_flow import get_interrupt_flow

        await get_interrupt_flow().interrupt(source="barge_in", annotate_history=True)
        try:
            await asr_h.notify_turn_complete("barge_in")
        except Exception:
            logger.exception("barge-in ASR release failed")
        try:
            qwen_hot_window = max(float(WAKE_AWAKE_SECONDS), float(ASR_IDLE_UNLOAD_SECONDS))
            await asr_h.start_listening(
                {
                    "source": "wake",
                    "wake": {"source": "barge_in"},
                    "awake_seconds": qwen_hot_window,
                    "finish_after_turn_complete": False,
                }
            )
        except Exception:
            logger.exception("barge-in ASR rearm failed")

    from asr.barge_in_detector import BargeInDetector

    async def _emit_barge_in_status(payload: dict) -> None:
        await bus.emit(Method.TTS_STATUS, payload)

    barge_in_detector = BargeInDetector(
        tts_playing_fn=_tts_is_raw_playing,
        on_barge_in=_interrupt_for_barge_in,
        on_debug=_emit_barge_in_status,
    )

    def _start_barge_in_detector() -> None:
        if not _barge_in_enabled():
            return
        if barge_in_detector.running:
            return
        try:
            barge_in_detector.start(server_loop)
        except Exception:
            logger.exception("failed to start barge-in detector")

    def _on_sentence_start(sentence_id: str) -> None:
        _expr_ctrl.on_sentence_start(sentence_id)
        try:
            from server.character_presentation import playback_bridge
            from server.vn_tts_bridge import get_vn_sentence_metadata

            playback_bridge.on_sentence_start(
                sentence_id,
                get_vn_sentence_metadata(sentence_id),
            )
        except Exception:
            logger.exception("narration presentation start failed")
        _start_barge_in_detector()

    playback_manager.on_sentence_start = _on_sentence_start

    def _on_sentence_complete(sentence_id: str, _text: str) -> None:
        try:
            from server.character_presentation import playback_bridge

            playback_bridge.on_sentence_end(sentence_id)
        except Exception:
            logger.exception("narration presentation release failed")

    playback_manager.on_sentence_complete = _on_sentence_complete

    def _on_turn_playback_complete() -> None:
        logger.info("turn playback complete; notifying ASR")
        try:
            barge_in_detector.stop()
        except Exception:
            pass
        try:
            _expr_ctrl.on_turn_end()
        except Exception:
            logger.exception("expression turn-complete callback failed")
        try:
            from server.character_presentation import playback_bridge

            playback_bridge.release_all(handoff="after_speech")
        except Exception:
            logger.exception("narration presentation turn release failed")
        try:
            server_loop.call_soon_threadsafe(
                lambda: asyncio.create_task(asr_h.notify_turn_complete())
            )
        except Exception:
            logger.exception("asr turn-complete callback failed")

    playback_manager.on_turn_playback_complete = _on_turn_playback_complete

    # Render signal bridge.
    #
    # Keep the global speech/mouth/intent signal path lightweight at backend
    # boot. The full GUI SpriteForge runtime is expensive and is created only
    # by render.start below. Wallpaper owns a separate scene host/runtime.
    from render.headless_bridge import HeadlessRenderBridge
    _render_signal_bridge = HeadlessRenderBridge(project_root=Path(ROOT))

    class _RenderSignalAnimator:
        """ExpressionController-compatible signal shim.

        It forwards semantic graph signals without registering SpriteForge
        frames. GUI render can therefore stay fully lazy, while wallpaper still
        receives speaking/mouth/intent events through WallpaperHandler.
        """

        def __init__(self, engine) -> None:
            self.engine = engine

        def trigger_expression(self, expression_label: str) -> None:
            from server.character_presentation import coordinator as character_presentation

            character_presentation.claim_now(
                source_kind="main_chat",
                source_id="active-expression",
                label=str(expression_label or ""),
                tier="utterance",
            )

        def on_speaking(self, speaking: bool) -> None:
            self.engine.set_speaking(bool(speaking))
            if not speaking:
                from server.character_presentation import coordinator as character_presentation

                character_presentation.release_now(
                    source_kind="main_chat",
                    source_id="active-expression",
                    tier="utterance",
                    handoff="after_speech",
                )

        def set_mouth_value(self, value: float) -> None:
            self.engine.set_mouth_value(value)

        def stop(self) -> None:
            from server.character_presentation import coordinator as character_presentation

            character_presentation.release_now(
                source_kind="main_chat",
                source_id="active-expression",
                tier="utterance",
            )
            self.engine.set_speaking(False)
            self.engine.set_mouth_value(0.0)

    _expr_ctrl.set_animator(_RenderSignalAnimator(_render_signal_bridge), backend="graph")
    logger.info("render signal bridge ready (GUI SpriteForge runtime is lazy)")

    _gui_render_bridge = None
    _gui_sf_animator = None
    _gui_render_lock = threading.Lock()

    def _ensure_gui_render_runtime():
        nonlocal _gui_render_bridge, _gui_sf_animator
        with _gui_render_lock:
            if _gui_render_bridge is not None:
                return _gui_render_bridge
            bridge = HeadlessRenderBridge(project_root=Path(ROOT))
            try:
                from render.spriteforge_animator import SpriteForgeAnimator
                animator = SpriteForgeAnimator(bridge)
                if animator.start():
                    _gui_sf_animator = animator
                    logger.info("GUI SpriteForge animator started lazily")
                else:
                    _gui_sf_animator = None
                    logger.info("GUI render started without the optional character pack")
            except Exception as e:
                logger.warning("GUI SpriteForge animator init failed: %s", e)
                _gui_sf_animator = None
            _gui_render_bridge = bridge
            return _gui_render_bridge

    def _stop_gui_render_runtime() -> None:
        nonlocal _gui_render_bridge, _gui_sf_animator
        with _gui_render_lock:
            animator = _gui_sf_animator
            _gui_sf_animator = None
            _gui_render_bridge = None
        if animator is None:
            return
        try:
            animator.stop()
        except Exception:
            logger.exception("GUI SpriteForge animator stop failed")

    # Local character rendering is the primary mouth sink. VTube Studio stays
    # attached to the router only as an optional compatibility output.
    mouth_signal_router.set_primary_sink(_render_signal_bridge.set_mouth_value)

    # thread pools & queues.
    tts_max_workers = max(1, EXP_TTS_MAX_CONCURRENCY)
    tts_executor = ThreadPoolExecutor(max_workers=tts_max_workers)
    translation_executor = ThreadPoolExecutor(max_workers=4)
    try:
        from server.wallpaper_subtitle_translator import (
            get_translation_runtime_info,
            translate_wallpaper_subtitle,
        )

        pre_translation_cache.set_translate_fn(translate_wallpaper_subtitle)
        subtitle_translation_info = get_translation_runtime_info()
        logger.info(
            "Wallpaper subtitle translation configured; active=%s mode=%s provider=%s model=%s",
            _pre_translation_enabled(),
            wallpaper_subtitle_runtime.get_mode(),
            subtitle_translation_info.get("provider"),
            subtitle_translation_info.get("model"),
        )
    except Exception:
        logger.exception("failed to configure server pre-translation cache")
    pending_actions = Queue()
    pending_sentence_items = asyncio.Queue(maxsize=3)
    exp_tts_semaphore = asyncio.Semaphore(EXP_TTS_MAX_CONCURRENCY)
    exp_play_condition = asyncio.Condition()

    # OpenClaw gateway.
    try:
        from openclaw.gateway import start_openclaw_gateway as _start_oc
        asyncio.create_task(_start_oc())
    except Exception as e:
        logger.warning("openclaw gateway init failed: %s", e)

    # VTS connect.
    loop = asyncio.get_running_loop()

    # local llama server
    if (
        LLM_PROVIDER == "local"
        and LOCAL_LLM_TYPE == "llama_server"
        and LOCAL_LLM_LAUNCH_MODE == "managed"
    ):
        try:
            from llm.llama_server import start_llama_server as _start_ls, warmup_local_llm_cache as _warmup
            await _start_ls()
            asyncio.create_task(_warmup())
        except Exception as e:
            logger.warning("llama server init failed: %s", e)

    if VTS_ENABLED:
        try:
            await loop.run_in_executor(None, vts_manager.connect)
        except Exception as e:
            logger.warning("vts connect error: %s", e)
    else:
        logger.info("vts connection disabled by VTS_ENABLED=0")

    if vts_manager.connected and VTS_HEARTBEAT_ENABLED:
        logger.info("vts connected")
        from vts.action import heartbeat_worker as _hb
        loop.run_in_executor(None, _hb)
    elif vts_manager.connected:
        logger.info("vts connected; heartbeat disabled")
    elif VTS_ENABLED:
        logger.warning("vts connection failed, will retry automatically")
    else:
        logger.info("vts connection skipped")

    # background workers.
    if e2e_no_tts or TTS_BACKEND == "disabled":
        async def _discard_disabled_tts_requests() -> None:
            while True:
                await pending_sentence_items.get()
                pending_sentence_items.task_done()

        asyncio.create_task(_discard_disabled_tts_requests(), name="tts-disabled-noop")
    else:
        from tts.pipeline import play_sentence_worker as _psw
        asyncio.create_task(playback_manager.run())
        asyncio.create_task(_psw())
    from vts.action import action_worker as _aw
    loop.run_in_executor(None, _aw)

    def _on_speculative_asr_text(text: str) -> None:
        """ASR 工作线程 → 事件循环：投机文本就绪，尝试发起投机 LLM 轮。"""
        try:
            from server.speculative_turn import get_speculative_launcher

            asyncio.run_coroutine_threadsafe(
                get_speculative_launcher().launch(text), server_loop
            )
        except Exception:
            logger.debug("speculative text dispatch failed", exc_info=True)

    def _get_or_create_asr_manager():
        global asr_manager
        if asr_manager is not None:
            return asr_manager
        with _asr_manager_lock:
            if asr_manager is not None:
                return asr_manager
            from asr.manager import ASRManager as _ASR
            mgr = _ASR(backend=asr_h.backend_name)
            try:
                mgr._tts_playing_fn = _tts_is_raw_playing
                mgr._tts_block_mic_fn = _tts_should_block_mic
            except Exception:
                pass
            try:
                # 投机 LLM 启动（切片 D2）：投机转写文本就绪时从 ASR 线程
                # 跳回事件循环发起 pending 轮（策略门在 launcher 内部）
                mgr.set_speculative_text_callback(_on_speculative_asr_text)
            except Exception:
                logger.exception("failed to wire speculative text callback")
            asr_manager = mgr
            logger.info("asr manager ready (lazy)")
            return asr_manager

    def _clear_asr_manager(manager) -> None:
        global asr_manager
        with _asr_manager_lock:
            if asr_manager is manager:
                asr_manager = None

    async def _start_asr_from_wake(payload=None):
        payload = payload or {}
        qwen_hot_window = max(float(WAKE_AWAKE_SECONDS), float(ASR_IDLE_UNLOAD_SECONDS))
        if not await _main_voice_allowed_now("wake detected"):
            return
        logger.info("wake detected; entering Qwen ASR hot window for %.1fs", qwen_hot_window)
        try:
            from core.turn_coordinator import get_turn_coordinator

            get_turn_coordinator().on_wake_detected()
        except Exception:
            logger.debug("turn coordinator notify failed", exc_info=True)
        command_text = str(payload.get("command_text") or "").strip()
        if command_text and WAKE_AUTO_SEND_TO_CHAT:
            try:
                await bus.emit(Method.ASR_RECOGNIZED, {"text": command_text, "is_final": True, "source": "wake", "wake": payload})
                logger.info(
                    "wake inline command; auto-sending to chat: %s",
                    protected_text(command_text),
                )
                await _send_wake_text(command_text, source="wake")
            except Exception:
                logger.exception("failed to send wake inline command")
        await asr_h.start_listening(
            {
                "source": "wake",
                "wake": payload,
                "awake_seconds": qwen_hot_window,
                "finish_after_turn_complete": False,
            }
        )

    def _current_or_create_session_id() -> str:
        try:
            from core import session_manager as sm
            sid = sm.get_current_session_id()
            if sid:
                return sid
            sid = time.strftime("%Y%m%d-%H%M%S")
            sm.create_session(sid)
            sm.save_session(sid, enable_conversation=True)
            return sid
        except Exception:
            logger.exception("failed to prepare wake chat session")
            return ""

    async def _send_wake_text(text: str, *, source: str = "wake") -> None:
        if not WAKE_AUTO_SEND_TO_CHAT:
            return
        text = str(text or "").strip()
        if not text:
            return
        if not await _main_voice_allowed_now(source):
            return
        try:
            from asr.text_filter import is_asr_prompt_leak
            from config.settings import ASR_CONTEXT

            if is_asr_prompt_leak(text, context=ASR_CONTEXT):
                logger.warning(
                    "%s text ignored as ASR prompt leak: %s",
                    source,
                    protected_text(text),
                )
                return
        except Exception:
            logger.exception("failed to check ASR prompt leak")
        # 投机决议（切片 D2）：正式文本与投机轮一致则确认放行，不再重发；
        # 不一致 / 投机轮已被作废则按原路径正常发送
        try:
            from server.speculative_turn import get_speculative_launcher

            if await get_speculative_launcher().resolve(text):
                logger.info("%s text matched speculative turn; confirmed, skip resend", source)
                return
        except Exception:
            logger.exception("speculative resolve failed; sending normally")
        import llm.client as _lcm
        provider = str(getattr(_lcm, "LLM_PROVIDER", "") or LLM_PROVIDER)
        session_id = _current_or_create_session_id()
        logger.info(
            "%s text; auto-sending to chat: %s",
            source,
            protected_text(text),
        )
        await chat_h.send_text(
            text,
            provider=provider,
            session_id=session_id,
            source="wake",
        )

    async def _handle_asr_recognized(payload: dict) -> None:
        source = str(payload.get("source") or "")
        if source == "vn_player":
            await _handle_vn_player_asr_recognized(payload)
            return
        if source != "wake":
            return
        await _send_wake_text(str(payload.get("text") or ""), source="wake ASR")

    async def _handle_vn_player_asr_recognized(payload: dict) -> None:
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        source_payload = payload.get("source_payload")
        if not isinstance(source_payload, dict):
            source_payload = {}
        kind = str(source_payload.get("kind") or "ask").strip().lower()
        params = {
            "text": text,
            "source": "asr",
            "metadata": {
                "source": "vn_player_asr",
                "asr": {
                    "is_final": bool(payload.get("is_final", True)),
                    "source_payload": source_payload,
                },
            },
        }
        if kind == "note":
            result = await vn_h.handle(Method.VN_PLAYER_NOTE, params)
        elif kind == "pin":
            result = await vn_h.handle(Method.VN_PLAYER_PIN, params)
        elif kind == "choice":
            result = await vn_h.handle(Method.VN_CHOICE_ASK, params)
        else:
            result = await vn_h.handle(Method.VN_PLAYER_ASK, params)
        try:
            await bus.emit(
                Method.ASR_STATUS,
                {
                    "status": "routed",
                    "source": "vn_player",
                    "kind": kind if kind in {"ask", "note", "pin", "choice"} else "ask",
                    "text_len": len(text),
                    "result_status": (result or {}).get("status") if isinstance(result, dict) else "",
                },
            )
        except Exception:
            logger.exception("failed to emit VN player ASR route status")

    async def _handle_wake_chat_finished(payload: dict) -> None:
        status = str(payload.get("status") or "")
        if status in {"error", "empty"}:
            logger.info("wake chat finished without playable turn (%s); resuming ASR loop", status)
            await asr_h.notify_turn_complete(f"chat_{status}")
            return
        if status == "complete":
            asyncio.create_task(_release_asr_after_playback_idle())

    async def _release_asr_after_playback_idle() -> None:
        # Fallback for cases where LLM streaming completes but PlaybackManager's
        # last-sentence watcher misses the turn-complete callback.
        await asyncio.sleep(0.5)
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            if not asr_h.is_waiting_turn_complete():
                return
            player_busy = _tts_is_raw_playing()
            playback_ready = bool(
                playback_manager
                and getattr(playback_manager, "player_is_ready", None)
                and playback_manager.player_is_ready.is_set()
            )
            pending_empty = True
            try:
                pending_empty = pending_sentence_items.empty()
            except Exception:
                pass
            if (not player_busy) and playback_ready and pending_empty:
                logger.info("wake chat complete and playback idle; releasing ASR turn wait fallback")
                await asr_h.notify_turn_complete("chat_complete_idle")
                return
            await asyncio.sleep(0.2)
        if asr_h.is_waiting_turn_complete():
            logger.warning("wake chat complete fallback timed out; releasing ASR turn wait")
            await asr_h.notify_turn_complete("chat_complete_timeout")

    async def _handle_wake_bridge_text(payload: dict) -> None:
        await _send_wake_text(str(payload.get("text") or ""), source="wake bridge")

    async def _handle_tts_interrupt(payload: dict) -> None:
        try:
            from server.character_presentation import playback_bridge

            playback_bridge.release_all(handoff="immediate")
        except Exception:
            logger.exception("narration presentation interrupt release failed")
        try:
            _expr_ctrl.on_turn_end()
        except Exception:
            logger.exception("expression interrupt reset failed")
        try:
            _render_signal_bridge.set_mouth_value(0.0)
            _render_signal_bridge.set_speaking(False)
        except Exception:
            logger.exception("render interrupt reset failed")

        try:
            from core import session_manager as sm

            completed_text = str(payload.get("completed_text") or "").strip()
            accumulated_text = str(payload.get("accumulated_text") or "").strip()
            interrupted_prefix = completed_text or accumulated_text
            marker = "[interrupted by user]"
            interrupted_text = f"{interrupted_prefix} {marker}".strip() if interrupted_prefix else marker
            dialog = getattr(sm.conversation_history, "dialog", [])
            had_assistant = any(
                message.get("role") == "assistant"
                for message in dialog
                if isinstance(message, dict)
            )
            turn_id = str(payload.get("turn_id") or "")
            changed = sm.conversation_history.mark_last_assistant_interrupted(
                interrupted_prefix,
                marker=marker,
                turn_id=turn_id or None,
            )
            if not had_assistant:
                sm.conversation_history.add_assistant(
                    interrupted_text,
                    turn_id=turn_id or None,
                )
                changed = True
            sid = sm.get_current_session_id()
            if sid and changed:
                sm.save_session(sid, enable_conversation=True)
            logger.info(
                "emitting chat.interrupted turn=%s text_len=%s completed_len=%s subscribers=%s source=%s",
                payload.get("turn_id") or "",
                len(interrupted_text),
                len(interrupted_prefix),
                bus.subscriber_count(Method.CHAT_INTERRUPTED),
                payload.get("source") or "",
            )
            await bus.emit(
                Method.CHAT_INTERRUPTED,
                {
                    "text": interrupted_text,
                    "completed_text": interrupted_prefix,
                    "marker": marker,
                    "source": payload.get("source") or "",
                    "session_id": sid or "",
                    "turn_id": payload.get("turn_id") or "",
                },
            )
            if not changed:
                return
            logger.info(
                "annotated interrupted assistant turn in session=%s source=%s",
                sid or "",
                payload.get("source") or "",
            )
        except Exception:
            logger.exception("failed to annotate interrupted assistant turn")

    async def _handle_asr_ready_to_listen(payload: dict) -> None:
        if str(payload.get("source") or "") != "wake":
            return
        logger.info("awake ASR backend ready; pausing SenseVoice bridge")
        try:
            await wake_h.stop({"close_shared_mic": False})
        except Exception:
            logger.exception("failed to pause wake service after awake ASR became ready")

    async def _handle_asr_listening_stopped(payload: dict) -> None:
        try:
            from server.speculative_turn import get_speculative_launcher

            await get_speculative_launcher().abandon(
                f"listen_stopped:{payload.get('reason') or ''}"
            )
        except Exception:
            logger.debug("speculative abandon failed", exc_info=True)
        if str(payload.get("source") or "") != "wake":
            return
        logger.info("awake ASR ended (%s); restoring wake service", payload.get("reason"))
        try:
            from asr.mic_input_service import close_mic_input_service
            close_mic_input_service()
        except Exception:
            logger.exception("failed to close shared mic before wake restore")
        if WAKE_ENABLED and not await _vn_runtime_active():
            await wake_h.start({})

    def _get_or_create_wake_service():
        global wake_service
        if wake_service is not None:
            return wake_service
        with _wake_service_lock:
            if wake_service is not None:
                return wake_service
            from asr.wake_service import WakeService as _WakeService
            svc = _WakeService(
                on_wake=_start_asr_from_wake,
                on_awake_text=_handle_wake_bridge_text if WAKE_BRIDGE_AUTO_SEND else None,
                tts_playing_fn=_tts_should_block_mic,
            )
            wake_service = svc
            logger.info("wake service ready (lazy)")
            return wake_service

    async def _prepare_for_external_vn_launch(params: dict) -> dict:
        try:
            await wallpaper_h.handle(Method.WALLPAPER_STOP, params or {})
        except Exception:
            logger.exception("failed to stop wallpaper before VN launch")
        try:
            await asr_h.stop_listening()
        except Exception:
            logger.exception("failed to stop ASR before VN launch")
        try:
            await wake_h.stop({})
        except Exception:
            logger.exception("failed to stop wake service before VN launch")
        return {"status": "prepared"}

    # inject runtime deps.
    import tts.pipeline as _tts_pipeline
    _tts_pipeline.configure(
        tts_runtime=tts_runtime,
        tts_executor=tts_executor,
        playback_manager=playback_manager,
        player=player,
        pending_sentence_items=pending_sentence_items,
        llm_warmup_fn=_noop_warmup,
        exp_tts_semaphore=exp_tts_semaphore,
    )
    import vts.action as _vts_action_mod
    _vts_action_mod.configure(
        vts_manager=vts_manager,
        pending_actions=pending_actions,
    )
    from server import host_action_dispatcher

    host_action_dispatcher.configure(
        delegate_handler=_handle_delegate,
        expression_sink=_vts_action_mod.record_expression_actions,
    )
    import llm.client as _llm_client_mod
    _llm_client_mod.configure(llm_provider=LLM_PROVIDER)
    from core.chat_runtime import get_chat_runtime

    control_authority_enabled = bool(
        getattr(settings, "CONTROL_DECISION_AUTHORITY_ENABLED", False)
    )
    control_shadow_enabled = bool(
        getattr(settings, "CONTROL_DECISION_SHADOW_ENABLED", False)
    )
    compound_control_shadow_enabled = bool(
        getattr(settings, "COMPOUND_CONTROL_SHADOW_ENABLED", False)
    )
    compound_control_authority_enabled = bool(
        getattr(settings, "COMPOUND_CONTROL_AUTHORITY_ENABLED", False)
    )
    if compound_control_authority_enabled and not control_authority_enabled:
        raise RuntimeError(
            "compound control authority requires ControlDecision authority"
        )

    async def _query_structured_control(
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
    ) -> str:
        return await asyncio.to_thread(
            _llm_client_mod.remote_llm_messages_query,
            messages,
            temperature=0.0,
            max_tokens=max_tokens,
            timeout=float(getattr(settings, "CONTROL_DECISION_TIMEOUT_S", 45)),
        )

    if control_shadow_enabled or control_authority_enabled:
        from server.control_adjudication import RuntimeControlDecisionResolver

        async def _query_control_decision(messages: list[dict[str, str]]) -> str:
            return await _query_structured_control(
                messages,
                max_tokens=int(
                    getattr(settings, "CONTROL_DECISION_MAX_TOKENS", 900)
                ),
            )

        control_resolver = RuntimeControlDecisionResolver(
            coordinator=work_ledger,
            query=_query_control_decision,
            compound_shadow=(
                compound_control_shadow_enabled
                or compound_control_authority_enabled
            ),
            project_limit=int(
                getattr(settings, "CONTROL_DECISION_PROJECT_LIMIT", 200)
            ),
            work_item_limit=int(
                getattr(settings, "CONTROL_DECISION_WORK_ITEM_LIMIT", 200)
            ),
            exhaustive_candidate_limit=int(
                getattr(
                    settings,
                    "CONTROL_DECISION_EXHAUSTIVE_CANDIDATE_LIMIT",
                    64,
                )
            ),
        )
        get_chat_runtime().configure(
            control_proposal_observer=control_resolver,
            control_proposal_authority=control_authority_enabled,
            compound_control_authority=compound_control_authority_enabled,
            control_proposal_authority_timeout_s=float(
                getattr(settings, "CONTROL_DECISION_AUTHORITY_TIMEOUT_S", 30.0)
            ),
            control_authority_block_callback=(
                _announce_control_authority_block
                if control_authority_enabled
                else None
            ),
        )
        logger.info(
            (
                "[CONTROL-DECISION] runtime enabled mode=%s "
                "project_limit=%d work_item_limit=%d exhaustive_limit=%d "
                "compound_authority=%s compound_shadow=%s"
            ),
            "authority-canary" if control_authority_enabled else "shadow",
            int(getattr(settings, "CONTROL_DECISION_PROJECT_LIMIT", 200)),
            int(getattr(settings, "CONTROL_DECISION_WORK_ITEM_LIMIT", 200)),
            int(
                getattr(
                    settings,
                    "CONTROL_DECISION_EXHAUSTIVE_CANDIDATE_LIMIT",
                    64,
                )
            ),
            compound_control_authority_enabled,
            compound_control_shadow_enabled,
        )
    else:
        get_chat_runtime().configure(
            control_proposal_observer=None,
            control_proposal_authority=False,
            compound_control_authority=False,
            control_authority_block_callback=None,
        )

    # expression presets.

    async def _speak_vn_reaction(payload: dict) -> dict:
        voice_text = str(
            payload.get("voice_text_ja")
            or payload.get("voice_text")
            or payload.get("tts_text")
            or payload.get("text")
            or ""
        ).strip()
        display_text = str(
            payload.get("display_text")
            or payload.get("subtitle_text")
            or ""
        ).strip()
        if not voice_text and not display_text:
            return {"status": "skipped", "reason": "empty_text"}
        if e2e_no_tts:
            return {
                "status": "skipped",
                "reason": "e2e_no_tts",
                "captured_text": display_text or voice_text,
            }
        delivery = (
            payload.get("_narration_delivery")
            if isinstance(payload.get("_narration_delivery"), dict)
            else None
        )
        if delivery is None:
            line_id = str(payload.get("line_id") or payload.get("turn_id") or "").strip()
            delivery = {
                "source_kind": "host",
                "source_id": line_id or f"host-voice-{time.time_ns()}",
                "session_id": "",
                "request_id": line_id or f"host-narration-{time.time_ns()}",
            }

        try:
            from server.vn_tts_bridge import submit_vn_tts_confirmed

            result = await submit_vn_tts_confirmed(
                {
                    **payload,
                    "_narration_delivery": delivery,
                    "display_text": display_text,
                    "voice_text_ja": voice_text,
                },
                pending_sentence_items=pending_sentence_items,
            )
            if payload.get("complete_turn") is True and result.get("status") == "queued":
                last_sentence_id = str(result.get("last_sentence_id") or "").strip()
                if last_sentence_id and playback_manager is not None:
                    playback_manager.mark_turn_last_sentence(
                        last_sentence_id,
                        str(payload.get("turn_id") or payload.get("line_id") or "") or None,
                    )
            if result.get("status") in {"dropped", "skipped"}:
                logger.warning("vn tts not queued: %s", result)
            return result
        except Exception:
            logger.exception("vn tts enqueue failed")
            return {"status": "error", "reason": "enqueue_failed"}

    async def _deliver_work_narration(payload: dict) -> dict:
        """Attach Work identity without moving Work policy into delivery."""

        from server.narration_delivery import NarrationRequest, deliver_narration

        line_id = str(payload.get("line_id") or payload.get("turn_id") or "").strip()
        source_id = str(
            payload.get("attempt_id")
            or payload.get("work_item_id")
            or line_id
            or f"work-{time.time_ns()}"
        ).strip()
        receipt = await deliver_narration(
            NarrationRequest(
                request_id=line_id or f"work-narration-{time.time_ns()}",
                source_kind="work",
                source_id=source_id,
                payload=payload,
            ),
            _speak_vn_reaction,
        )
        return receipt.to_dict()

    async def _deliver_vn_narration(payload: dict) -> dict:
        """Attach VN identity while leaving Director policy in VNPlayerRuntime."""

        from server.narration_delivery import NarrationRequest, deliver_narration

        line = payload.get("line") if isinstance(payload.get("line"), dict) else {}
        session_id = str(payload.get("session_id") or "").strip()
        line_id = str(line.get("line_id") or "").strip()
        script_id = str(line.get("script_id") or "").strip()
        source_id = session_id or script_id or line_id or f"vn-{time.time_ns()}"
        identity = "-".join(part for part in (session_id, script_id, line_id) if part)
        receipt = await deliver_narration(
            NarrationRequest(
                request_id=f"vn-narration-{identity or time.time_ns()}",
                source_kind="vn",
                source_id=source_id,
                session_id=session_id,
                payload=payload,
            ),
            _speak_vn_reaction,
        )
        return receipt.to_dict()

    host_readonly_voice_sink = _speak_vn_reaction

    def _append_work_observer_to_history(decision: dict) -> None:
        entry = str(decision.get("main_chat_entry") or "").strip()
        if not entry:
            return
        try:
            from core import session_manager as sm

            session_id = str(decision.get("session_id") or "")
            if session_id and sm.get_current_session_id() != session_id:
                return
            sm.conversation_history.add_assistant(f"[WORK_OBSERVER]\n{entry}")
            sid = sm.get_current_session_id()
            if sid:
                sm.save_session(sid, enable_conversation=True)
        except Exception:
            logger.exception("failed to append work observer decision to history")

    def _recent_parent_chat(session_id: str) -> list[dict[str, str]]:
        try:
            from core import session_manager as sm

            if session_id and sm.get_current_session_id() != session_id:
                return []
            items = []
            for message in list(sm.conversation_history.dialog)[-6:]:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                content = str(message.get("content") or "")
                if role and content:
                    items.append({"role": role, "content": content[:700]})
            return items
        except Exception:
            logger.exception("failed to collect recent parent chat context")
            return []

    def _recent_chat_for_auip(session_id: str) -> list[dict[str, str]]:
        try:
            branch_messages = auip_h.runtime.recent_role_branch_messages(
                session_id,
                limit=6,
            )
            if branch_messages is not None:
                return branch_messages
        except Exception:
            logger.exception("failed to collect AUIP AppSession branch context")
        return _recent_parent_chat(session_id)

    from server.auip_engagement import AuipEngagementCoordinator
    from server.auip_participant_llm import (
        decide_with_auip_participant,
        has_auip_participant_llm_config,
    )
    from server.auip_role_authorizer_llm import (
        authorize_with_main_role,
        has_auip_role_authorizer_config,
    )

    auip_engagement = AuipEngagementCoordinator(
        app_runtime=auip_h.runtime,
        controller=(
            decide_with_auip_participant
            if has_auip_participant_llm_config()
            else None
        ),
        role_authorizer=(
            authorize_with_main_role
            if has_auip_role_authorizer_config()
            else None
        ),
        recent_chat=_recent_chat_for_auip,
        is_chat_busy=chat_h.is_busy,
    )
    auip_h.engagement = auip_engagement
    auip_engagement_callback = auip_engagement.on_update
    bus.on(Method.AUIP_UPDATED, auip_engagement_callback)

    from server.auip_control_decision import AuipControlDecisionResolver

    async def _route_auip_control(
        attrs: dict,
        *,
        session_id: str,
        user_text: str,
        turn_id: str,
    ) -> None:
        """Resolve role-level AUIP control against host-owned focus.

        The model selects only the source-local control verb.  AppSession
        identity comes from the current Session, so an app cannot smuggle a
        target id through prompt data and a stale model id cannot redirect it.
        """

        async def prepare_existing_work(candidate, _mode: str) -> None:
            """Queue the AUIP prerequisite through the ordinary Work path.

            The source-local decision authorizes only preparation of this
            Host-resolved WorkItem. It neither chooses a Provider nor creates
            an AUIP-shaped run. The Provider adapter receives the user's exact
            request plus the existing host-authoring capability contract.
            """

            attrs = {
                "intent": "amend",
                "subject": "work_item",
                "work_placement": "not_applicable",
                "workspace_ref": str(candidate.work_item_id),
                "_host_reference_resolved": True,
                "_host_dispatch_source": "auip_prepare",
                "_host_source_user_text": str(user_text or "")[:4000],
                "_host_turn_id": str(turn_id or "")[:200],
            }

            async def run() -> None:
                try:
                    await _handle_delegate(str(user_text or ""), attrs)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "[AUIP-CONTROL] preparation Work failed turn_id=%s",
                        turn_id,
                    )

            task = asyncio.create_task(
                run(),
                name=f"auip-prepare:{turn_id or 'turn'}",
            )
            _auip_preparation_tasks.add(task)
            task.add_done_callback(_auip_preparation_tasks.discard)

        action = str(attrs.get("action") or "").strip().lower()
        try:
            result = await auip_h.route_control(
                attrs,
                session_id=session_id,
                user_text=user_text,
                turn_id=turn_id,
                prepare_work=prepare_existing_work,
            )
        except Exception:
            logger.exception(
                "[AUIP-CONTROL] dispatch failed action=%s turn_id=%s",
                action,
                turn_id,
            )
            return
        if not isinstance(result, dict) or result.get("ok") is not True:
            logger.warning(
                "[AUIP-CONTROL] rejected action=%s turn_id=%s result=%s",
                action,
                turn_id,
                result,
            )

    async def _query_auip_control(messages: list[dict[str, str]]) -> str:
        return await _query_structured_control(messages, max_tokens=300)

    def _session_active_provider_work_attempt_ids(session_id: str) -> tuple[str, ...]:
        """Freeze host identity while exposing only liveness to the model."""

        roster = work_ledger.conversation_work_items_for_resolution(
            session_id,
            limit=200,
        )
        items = roster.get("items") if isinstance(roster, dict) else []
        return tuple(
            dict.fromkeys(
                str(item.get("attempt_id") or "").strip()
                for item in items or []
                if isinstance(item, dict)
                and str(item.get("execution") or "") in {"queued", "running"}
                and str(item.get("attempt_id") or "").strip()
            )
        )

    auip_control_decider = (
        AuipControlDecisionResolver(
            query=_query_auip_control,
            app_runtime=auip_h.runtime,
            launch_catalog=auip_launch,
            has_active_work=_session_active_provider_work_attempt_ids,
        )
        if bool(getattr(settings, "AUIP_CONTROL_DECISION_ENABLED", False))
        else None
    )
    get_chat_runtime().configure(
        auip_control_callback=_route_auip_control,
        auip_control_decider=auip_control_decider,
    )

    auip_b2 = None
    auip_b2_unavailable_reason = ""
    if str(settings.AUIP_APPSESSION_ROLE_BRANCH_MODE or "") == "b2":
        from server.auip_b2 import (
            AuipB2Coordinator,
            b2_runtime_unavailable_reason,
        )
        from server.auip_b2_role_llm import (
            choose_b2_open_role_action,
            choose_b2_role_action,
            has_b2_role_model_config,
        )

        auip_b2_unavailable_reason = b2_runtime_unavailable_reason(
            role_branch_mode=settings.AUIP_APPSESSION_ROLE_BRANCH_MODE,
            control_decision_available=auip_control_decider is not None,
            role_model_available=has_b2_role_model_config(),
        )
        if auip_b2_unavailable_reason:
            logger.warning(
                "[AUIP-B2] action capability unavailable reason=%s; "
                "backend startup continues and application actions remain blocked",
                auip_b2_unavailable_reason,
            )
        else:
            auip_b2 = AuipB2Coordinator(
                runtime=auip_h.runtime,
                control_decider=auip_control_decider,
                role_chooser=choose_b2_role_action,
                open_role_chooser=(
                    choose_b2_open_role_action
                    if settings.AUIP_B2_OPEN_PAYLOAD_MODE == "candidate"
                    else None
                ),
                open_payload_mode=settings.AUIP_B2_OPEN_PAYLOAD_MODE,
                stage_decision=get_chat_runtime().stage_auip_decision,
            )
            logger.info(
                "[AUIP-B2] foreground route enabled provider=%s model=%s "
                "effort=%s service_tier=%s open_payload=%s",
                settings.AUIP_ACTION_PROVIDER,
                settings.AUIP_ACTION_MODEL,
                settings.AUIP_ACTION_REASONING_EFFORT,
                settings.AUIP_ACTION_SERVICE_TIER,
                settings.AUIP_B2_OPEN_PAYLOAD_MODE,
            )
        auip_engagement.set_b2_coordinator(
            auip_b2,
            unavailable_reason=auip_b2_unavailable_reason,
        )

    # configure handlers with runtime deps.
    async def _route_interaction_branch(
        *,
        text: str,
        session_id: str,
        turn_id: str = "",
    ) -> dict | None:
        # Free-form language first acquires a canonical operation from the
        # model-owned DELEGATE/ControlDecision path. The Host must not infer
        # ``report`` from words such as "game" and "state": the same words can
        # describe an amend request. Canonical reports still read the Ledger
        # deterministically in ``_answer_report_from_ledger`` and never start
        # a Provider.
        routed = await interaction_branch.try_route_user_message(
            text=text,
            session_id=session_id,
            turn_id=turn_id,
        )
        if routed is not None:
            return routed
        if auip_b2 is not None:
            return await auip_b2.try_route_user_message(
                text=text,
                session_id=session_id,
                turn_id=turn_id,
            )
        return None

    async def _interrupt_presentation_before_chat() -> None:
        await tts_h.handle(
            Method.TTS_INTERRUPT,
            {
                "annotate_history": False,
                "source": "new_chat_turn_presentation",
            },
        )

    async def _interrupt_background_interaction_before_chat() -> None:
        if auip_engagement is None:
            return
        await auip_engagement.interrupt_for_user_turn(
            _attention_session_manager.get_current_session_id() or ""
        )

    chat_h.configure(
        stream_llm_query=_stream_llm_query_adapter,
        pending_sentence_items=pending_sentence_items,
        on_turn_finished=_handle_wake_chat_finished,
        interaction_branch_router=_route_interaction_branch,
        assistant_voice_sink=_speak_vn_reaction,
        presentation_interrupt=_interrupt_presentation_before_chat,
        background_interaction_interrupt=(
            _interrupt_background_interaction_before_chat
        ),
    )
    from server.interrupt_flow import get_interrupt_flow
    get_interrupt_flow().configure(chat_handler=chat_h, tts_handler=tts_h)

    def _current_llm_provider() -> str:
        import llm.client as _lcm

        return str(getattr(_lcm, "LLM_PROVIDER", "") or LLM_PROVIDER)

    async def _send_speculative_pending(text: str, **kw) -> dict:
        return await chat_h.send_text(
            text, provider=_current_llm_provider(), pending=True, **kw
        )

    from server.speculative_turn import get_speculative_launcher
    get_speculative_launcher().configure(
        send_pending=_send_speculative_pending,
        confirm=chat_h.confirm_pending_turn,
        discard=chat_h.discard_pending_turn,
        provider_getter=_current_llm_provider,
        asr_source_getter=lambda: str(getattr(asr_h, "_source", "") or ""),
        chat_busy_fn=chat_h.is_busy,
        voice_allowed_fn=lambda: _main_voice_allowed_now("speculative_llm"),
        session_id_factory=_current_or_create_session_id,
    )
    from server.runtime_status import status_collector
    status_collector.configure(
        port=port,
        chat_handler=chat_h,
        asr_handler=asr_h,
        playback_manager_getter=lambda: playback_manager,
        player_getter=lambda: player,
        pending_sentence_items_getter=lambda: pending_sentence_items,
        asr_manager_getter=lambda: asr_manager,
        wake_service_getter=lambda: wake_service,
        wallpaper_handler=wallpaper_h,
        provider_runtime_getter=lambda: provider_runtime,
        provider_availability_getter=provider_h.provider_availability,
        work_ledger_getter=lambda: work_ledger,
    )
    tts_h.configure(
        playback_manager=playback_manager,
        player=player,
        on_interrupt=_handle_tts_interrupt,
    )
    asr_h.configure(
        asr_manager_factory=_get_or_create_asr_manager,
        on_unload=_clear_asr_manager,
        on_recognized=_handle_asr_recognized,
        on_listening_stopped=_handle_asr_listening_stopped,
        on_ready_to_listen=_handle_asr_ready_to_listen,
        tts_playing_fn=_tts_should_block_mic,
    )
    wake_h.configure(wake_service_factory=_get_or_create_wake_service)
    vts_h.configure(vts_manager=vts_manager)
    expr_h.configure(expression_controller=_expr_ctrl)
    sys_h.configure(
        vts_manager=vts_manager,
        asr_handler=asr_h,
        asr_manager_getter=lambda: asr_manager,
        playback_manager=playback_manager,
        is_chat_busy=chat_h.is_busy,
        project_root=Path(ROOT),
    )
    render_h.configure(
        project_root=Path(ROOT),
        render_bridge=None,
        backend_port=port,
        ensure_runtime=_ensure_gui_render_runtime,
        stop_runtime=_stop_gui_render_runtime,
        state_bridge=_render_signal_bridge,
    )
    wallpaper_h.configure(
        project_root=Path(ROOT),
        render_bridge=_render_signal_bridge,
        wake_start_fn=lambda: wake_h.start({}),
        wake_stop_fn=lambda: wake_h.stop({}),
        canvas_action_fn=canvas_action_router.route,
        canvas_projector=work_ledger.project_canvas,
        attention_snapshot=lambda: attention_requests.list_pending(
            _attention_session_manager.get_current_session_id() or ""
        ),
    )
    work_activity.configure()
    interaction_branch.configure()
    from server.character_presentation import project_auip_update

    auip_presentation_callback = project_auip_update
    bus.on(Method.AUIP_UPDATED, auip_presentation_callback)
    output_idle_probe = lambda: not (chat_h.is_busy() or _tts_is_observer_output_busy())
    work_observer.configure(
        is_chat_busy=chat_h.is_busy,
        is_tts_busy=_tts_is_observer_output_busy,
        append_to_main_chat=_append_work_observer_to_history,
        narrate=_deliver_work_narration,
        get_recent_chat=_recent_parent_chat,
        display_language=_observer_display_language,
        observer_llm=_run_work_observer_llm,
        release_work=work_activity.release_work_presentation,
    )
    work_status_narrator = work_observer
    if bool(settings.AUIP_NARRATION_ENABLED):
        from server.auip_narration import AuipNarrationAdapter, AuipNarrationProfile
        from server.auip_narration_llm import (
            decide_with_auip_observer,
            has_auip_narration_llm_config,
            narrate_with_auip_llm,
            present_with_auip_llm,
        )

        if has_auip_narration_llm_config():
            auip_narration = AuipNarrationAdapter(
                runtime=auip_h.runtime,
                observer=decide_with_auip_observer,
                narrator=narrate_with_auip_llm,
                presenter=present_with_auip_llm,
                presentation_mode=settings.AUIP_PRESENTATION_MODE,
                sink=_speak_vn_reaction,
                profile=AuipNarrationProfile(
                    normal_beat_stride=max(
                        1, int(settings.AUIP_NARRATION_NORMAL_BEAT_STRIDE)
                    )
                ),
                recent_chat=_recent_chat_for_auip,
                display_language=_observer_display_language,
            )
            auip_narration_callback = auip_narration.enqueue_update
            bus.on(Method.AUIP_UPDATED, auip_narration_callback)
            logger.info(
                "AUIP narration enabled stride=%s presentation_mode=%s",
                max(1, int(settings.AUIP_NARRATION_NORMAL_BEAT_STRIDE)),
                settings.AUIP_PRESENTATION_MODE,
            )
        else:
            logger.warning(
                "AUIP narration requested but no configured narration model is available"
            )
    # The ledger may have persisted a terminal note just before a previous
    # process stopped or while the voice lane was wedged.  Replay only after
    # the Observer has subscribed; its delivery receipt will retire the entry.
    await work_ledger.replay_pending_terminal_notices()
    vn_h.configure(project_root=Path(ROOT), event_emit=bus.emit, speak_callback=_deliver_vn_narration)
    vn_launch_h.configure(
        project_root=Path(ROOT),
        runtime_start=lambda params: vn_h.handle(Method.VN_START, params),
        runtime_stop=lambda params: vn_h.handle(Method.VN_STOP, params),
        runtime_status=lambda: vn_h.handle(Method.VN_STATUS, {}),
        runtime_line=lambda params: vn_h.handle(Method.VN_LINE, params),
        before_external_launch=_prepare_for_external_vn_launch,
    )

    backend_ready = True
    logger.info(f"backend server ready on ws://127.0.0.1:{port}/ws")
    try:
        await server_task  # wait for server to finish
    finally:
        work_status_narrator = None
        bus.off(Method.WORK_UPDATED, auip_launch_callback)
        bus.off(Method.AUIP_UPDATED, work_preview_auip_callback)
        set_auip_launch_coordinator(None)
        if auip_presentation_callback is not None:
            bus.off(Method.AUIP_UPDATED, auip_presentation_callback)
        if auip_engagement_callback is not None:
            bus.off(Method.AUIP_UPDATED, auip_engagement_callback)
        if auip_engagement is not None:
            await auip_engagement.close()
        if auip_narration_callback is not None:
            bus.off(Method.AUIP_UPDATED, auip_narration_callback)
        if auip_narration is not None:
            await auip_narration.close()
        provider_runtime.set_request_preparer(None)
        await provider_runtime.close()
        await provider_activity_h.close()
        await work_ledger.drain_provider_facts()
        await work_observer.close()
        await work_preview.close_all()
        work_ledger.close()
        _stop_gui_render_runtime()
        if wake_service is not None:
            close = getattr(wake_service, "close", None)
            if callable(close):
                close()
        if asr_manager is not None:
            close = getattr(asr_manager, "close", None)
            if callable(close):
                close()
        try:
            from asr.mic_input_service import close_mic_input_service
            close_mic_input_service()
        except Exception:
            pass
        try:
            from llm.llama_server import stop_llama_server

            await asyncio.to_thread(stop_llama_server)
        except Exception:
            logger.exception("failed to stop managed llama-server")


# adapter: drives core.chat_runtime directly (no main.py attribute injection).

async def _stream_llm_query_adapter(
    text: str,
    gui_callback=None,
    provider: str | None = None,
    preserve_emotion: bool = False,
    visual_context: dict | None = None,
    turn_id: str = "",
    prompt_variant: str = "",
) -> str:
    """Thin adapter around ChatRuntime.stream_llm_query."""
    os.environ.setdefault("AMADEUS_HEADLESS", "1")
    from core.chat_runtime import get_chat_runtime
    import llm.client as _lcm

    rt = get_chat_runtime()

    # Sync the Electron-selected provider into the runtime and llm.client.
    selected_provider = str(provider or "").strip() or _lcm.LLM_PROVIDER or rt.provider
    if selected_provider:
        rt.set_provider(selected_provider)
        _lcm.configure(llm_provider=selected_provider)

    # Server runtime singletons (module globals populated by bootstrap).
    rt.configure(
        playback_manager=playback_manager,
        pending_sentence_items=pending_sentence_items,
    )
    e2e_no_tts = str(os.environ.get("AMADEUS_E2E_NO_TTS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    pre_translation_runtime.configure(
        False if e2e_no_tts else _pre_translation_enabled()
    )

    return await rt.stream_llm_query(
        text,
        gui_callback=gui_callback,
        preserve_emotion=preserve_emotion,
        visual_context=visual_context,
        turn_id=turn_id,
        prompt_variant=prompt_variant,
    )


async def _run_work_observer_llm(
    *,
    note: dict,
    notes: list,
    recent_chat: list | None = None,
    recent_spoken_updates: list | None = None,
) -> dict | None:
    from server.work_observer_llm import decide_with_observer_llm

    return await decide_with_observer_llm(
        note=note,
        notes=notes,
        recent_chat=recent_chat or [],
        recent_spoken_updates=recent_spoken_updates or [],
        display_language=_observer_display_language(),
    )


def _build_openclaw_follow_up(result_type: str) -> str:
    if result_type == "error":
        return (
            "[SYSTEM] OpenClaw 执行时遇到了问题，上方 [RESULT] 是错误或失败信息。"
            "请以 Kurisu 的口吻简短说明发生了什么，并提示用户下一步可以重试、换一种问法或检查设置。"
            "不要逐字朗读技术日志，不要提 provider、observer 或路由细节。"
        )
    if result_type == "partial":
        return (
            "[SYSTEM] OpenClaw 因 API 限制或工具限制只返回了部分结果，上方 [RESULT] 可能仍包含有用信息。"
            "请先总结其中确实有用的内容，再用一句话说明结果不完整。"
            "如果信息不足，请直接说明需要重新查询或补充条件。"
        )
    if result_type == "question":
        return (
            "[SYSTEM] OpenClaw 需要更多信息才能继续，上方 [RESULT] 包含它的问题。"
            "请以 Kurisu 的口吻把问题转述给用户，语气自然简短，不要逐字复述系统文本。"
        )
    return (
        "[SYSTEM] OpenClaw 已完成任务，上方 [RESULT] 是执行结果。"
        "请以 Kurisu 的口吻向用户做一次简短自然的汇报。"
        "总结结果内容，不要逐字朗读日志，不要提 provider、observer 或后台路由细节。"
    )


async def _speak_openclaw_delegate_result(result: str, result_type: str) -> None:
    """Run the legacy second-pass Kurisu summary after provider completion."""
    result = str(result or "").strip()
    if not result:
        return
    try:
        from core.session_manager import conversation_history

        conversation_history.add_assistant(f"[RESULT] OpenClaw 执行结果\n{result}")
    except Exception:
        logger.exception("failed to inject OpenClaw result into conversation history")

    try:
        from core.chat_runtime import get_chat_runtime

        gui_callback = get_chat_runtime().current_gui_callback
    except Exception:
        gui_callback = None

    follow_up = _build_openclaw_follow_up(result_type)
    await _stream_llm_query_adapter(
        follow_up,
        gui_callback=gui_callback,
        preserve_emotion=True,
    )


def _delegate_workspace_route(provider: str, attrs: dict, *, manifest=None) -> dict:
    """Resolve a host-owned delegate workspace without guessing a destination."""

    values = attrs if isinstance(attrs, dict) else {}
    if manifest is None:
        try:
            from agent_host.provider_runtime import runtime as provider_runtime

            manifest = provider_runtime.get_manifest(provider)
        except Exception:
            manifest = None
    if manifest is None:
        return {
            "status": "invalid",
            "reason": "provider_manifest_unavailable",
            "cwd": "",
            "projectId": "",
            "source": "delegate_guard",
        }
    from agent_host.provider_workspace import workspace_route_authority

    route_authority = workspace_route_authority(
        manifest.capabilities.workspace_ownership
    )
    if route_authority != "host":
        explicit = next(
            (
                str(values.get(key)).strip()
                for key in ("cwd", "workspace", "workspace_path", "project", "project_dir")
                if values.get(key) not in (None, "")
            ),
            "",
        )
        return {
            "status": "resolved",
            "cwd": explicit or None,
            "projectId": "",
            "source": "delegate_attribute" if explicit else "not_applicable",
        }

    try:
        from server.work_ledger_coordinator import get_work_ledger_coordinator
    except Exception:
        logger.exception("failed to load provider workspace control plane")
        return {
            "status": "invalid",
            "reason": "workspace_control_plane_unavailable",
            "cwd": "",
            "projectId": "",
            "source": "delegate_guard",
        }

    coordinator = get_work_ledger_coordinator()
    if coordinator is not None:
        try:
            if not values.get("session_id"):
                # An unnamed instruction falls back to the project this
                # conversation chose, so the route cannot be resolved without
                # knowing which conversation is asking.
                from core import session_manager as _sm

                values = {**values, "session_id": _sm.get_current_session_id() or ""}
            return coordinator.resolve_workspace_route(values)
        except Exception:
            logger.exception("failed to resolve provider workspace through work ledger")
            return {
                "status": "invalid",
                "reason": "workspace_resolution_failed",
                "cwd": "",
                "projectId": "",
                "source": "delegate_guard",
            }

    # Startup/test fallback never invents a destination. It can only preserve
    # an explicit existing directory already trusted by the host Project
    # Registry. Provider adapters still enforce their own execution contracts
    # after this host-owned routing decision.
    explicit = next(
        (
            str(values.get(key)).strip()
            for key in ("cwd", "workspace", "workspace_path", "project", "project_dir")
            if values.get(key) not in (None, "")
        ),
        "",
    )
    if explicit:
        try:
            resolved = str(Path(explicit).resolve())
        except (OSError, RuntimeError, ValueError):
            resolved = ""
        from server.project_registry import cwd_in_project_registry

        allowed = bool(
            resolved
            and Path(resolved).is_dir()
            and cwd_in_project_registry(resolved)
        )
        if allowed:
            return {
                "status": "resolved",
                "cwd": resolved,
                "projectId": "",
                "source": "explicit_cwd_without_ledger",
            }
    # Without a ledger there is no scratch destination either, so this refuses
    # rather than guessing. It is not the old "which project did you mean?" --
    # nothing asks that any more; unnamed work simply goes to scratch.
    return {
        "status": "missing",
        "reason": "no_work_ledger",
        "cwd": "",
        "projectId": "",
        "source": "delegate_guard",
    }


async def _announce_provider_workspace_block(provider: str, route: dict) -> None:
    """Make a failed host routing decision visible and audible once."""

    try:
        from core import session_manager as sm
        from server.ai_os_schema import work_note_payload, work_signal
        from server.event_bus import bus
        from server.protocol import Method
        from server.work_context import add_work_note

        reason = str(route.get("reason") or "workspace_unresolved")
        candidates = route.get("candidates") if isinstance(route.get("candidates"), list) else []
        # Asking the user to name a project only helps when naming one would
        # have changed the outcome. When the scratch destination itself is
        # unusable, saying that would send them after the wrong problem.
        summary = (
            "I could not prepare a workspace for new work, so I did not start this. "
            "The scratch area is unavailable; that needs fixing before I can run it."
            if reason == "scratch_unavailable"
            else (
                "I could not safely determine which project directory this instruction belongs to. "
                "Please name the project, or choose a historical task and lock its workspace."
            )
        )
        note = work_note_payload(
            source="workspace_router",
            provider=provider,
            run_id=f"{provider}_route_{time.time_ns()}",
            session_id=sm.get_current_session_id() or "",
            # This is a correction that no execution started, not a Provider
            # terminal result.  Marking it Result made WorkObserver replace the
            # structured failure with its generic "task finished" closure.
            phase="Checkpoint",
            title="Project context required",
            summary=summary,
            signals=[
                work_signal(
                    label="routing",
                    text=f"{provider} execution was not started",
                    detail=f"{reason}; {len(candidates)} candidate workspace(s)",
                    kind="status",
                    importance="blocking",
                )
            ],
            importance="blocking",
            metadata={
                "routing_blocked": True,
                "reason": reason,
                "candidate_count": len(candidates),
                "execution_started": False,
                "narration_keypoint": "execution_blocked",
            },
            speak=True,
        )
        add_work_note(note)
        await bus.emit(Method.CHAT_WORK_NOTE, note)
    except Exception:
        logger.exception("failed to announce blocked provider workspace routing")


async def _announce_provider_start_failure(provider: str, error: Exception) -> None:
    """Correct a pre-execution promise when Provider intake never starts.

    The conversational line necessarily precedes the delegate result.  If the
    Runtime rejects intake before it can mint a run, there is no Provider event
    for WorkActivity or WorkObserver to narrate, so the host must close that
    promise explicitly and without claiming that any external action happened.
    """

    try:
        from core import session_manager as sm
        from server.ai_os_schema import work_note_payload, work_signal
        from server.event_bus import bus
        from server.protocol import Method
        from server.work_context import add_work_note

        clean_provider = str(provider or "provider").strip().lower() or "provider"
        label = "Browser" if clean_provider == "browser" else clean_provider.capitalize()
        summary = (
            f"I could not start the {label} action, so nothing was opened or changed."
            if clean_provider == "browser"
            else f"I could not start the {label} task, so no new work was executed."
        )
        note = work_note_payload(
            source="provider_runtime",
            provider=clean_provider,
            run_id=f"{clean_provider}_start_failed_{time.time_ns()}",
            session_id=sm.get_current_session_id() or "",
            phase="Checkpoint",
            title=f"{label} did not start",
            summary=summary,
            signals=[
                work_signal(
                    label="start",
                    text=f"{label} execution was not started",
                    detail=error.__class__.__name__,
                    kind="status",
                    importance="blocking",
                )
            ],
            importance="blocking",
            metadata={
                "provider_start_failed": True,
                "failure_kind": error.__class__.__name__,
                "execution_started": False,
                "narration_keypoint": "execution_blocked",
            },
            speak=True,
        )
        add_work_note(note)
        await bus.emit(Method.CHAT_WORK_NOTE, note)
    except Exception:
        logger.exception("failed to announce provider start failure: %s", provider)


async def _announce_control_authority_block(resolution, session_id: str) -> None:
    """Correct a spoken commitment when the control plane starts nothing.

    This is intentionally a provider-neutral Work Note. WorkObserver already
    owns when and how blocking facts enter voice, so the decision callback does
    not wait for the current TTS floor or create a second chat turn.
    """

    try:
        from server.ai_os_schema import work_note_payload, work_signal
        from server.event_bus import bus
        from server.protocol import Method
        from server.work_context import add_work_note

        disposition = str(getattr(resolution, "disposition", "") or "failed_closed")
        status = str(getattr(resolution, "decision_status", "") or "unavailable")
        summary = (
            "The control check found no verified action to execute, so no Provider work was started."
            if disposition == "suppressed"
            else (
                "The control check did not finish with a complete safe result, "
                "so no Provider work or persistent project switch was started."
            )
        )
        note = work_note_payload(
            source="control_authority",
            provider="host",
            run_id=f"control_{time.time_ns()}",
            session_id=str(session_id or ""),
            phase="Checkpoint",
            title="Request was not started",
            summary=summary,
            signals=[
                work_signal(
                    label="control",
                    text="Nothing was started",
                    detail=f"{disposition}; {status}",
                    kind="status",
                    importance="blocking",
                )
            ],
            importance="blocking",
            metadata={
                "control_authority_blocked": True,
                "disposition": disposition,
                "decision_status": status,
                "execution_started": False,
                "narration_keypoint": "execution_blocked",
            },
            speak=True,
        )
        add_work_note(note)
        await bus.emit(Method.CHAT_WORK_NOTE, note)
    except Exception:
        logger.exception("failed to announce a blocked control decision")


_RETRACT_TERMINAL_STATUSES = {"done", "error", "cancelled"}


async def _announce_retract_outcome(summary: str, *, reason: str, count: int) -> None:
    """Let the character correct itself when a withdrawal could not be honoured.

    The model speaks before the host sees the tag, so by now it has already said
    it is stopping something. If nothing was running, or several things were,
    staying silent would leave that claim standing as fact.
    """

    try:
        from core import session_manager as sm
        from server.ai_os_schema import work_note_payload, work_signal
        from server.event_bus import bus
        from server.protocol import Method
        from server.work_context import add_work_note

        note = work_note_payload(
            source="workspace_router",
            provider="host",
            run_id=f"retract_{time.time_ns()}",
            session_id=sm.get_current_session_id() or "",
            phase="Checkpoint",
            title="Nothing was stopped",
            summary=summary,
            signals=[
                work_signal(
                    label="retract",
                    text="No attempt was cancelled",
                    detail=f"{reason}; {count} active run(s)",
                    kind="status",
                    importance="blocking",
                )
            ],
            importance="blocking",
            metadata={
                "retract_unresolved": True,
                "reason": reason,
                "active_runs": count,
                "narration_keypoint": "execution_blocked",
            },
            speak=True,
        )
        add_work_note(note)
        await bus.emit(Method.CHAT_WORK_NOTE, note)
    except Exception:
        logger.exception("failed to announce an unresolved retraction")


async def _announce_amend_ambiguous(titles: str) -> None:
    """Ask which task, in the nouns the user can see.

    Never the work_item_id: the model demonstrably will not quote one (0 of 18,
    then 0 of 10 more on 2026-08-01), and an identifier is not something this
    character would say out loud either.
    """

    try:
        from core import session_manager as sm
        from server.ai_os_schema import work_note_payload, work_signal
        from server.event_bus import bus
        from server.protocol import Method
        from server.work_context import add_work_note

        note = work_note_payload(
            source="workspace_router",
            provider="host",
            run_id=f"amend_{time.time_ns()}",
            session_id=sm.get_current_session_id() or "",
            phase="Checkpoint",
            title="Which task should I change?",
            summary=(
                "More than one task matches that file, so I did not guess: "
                f"{titles}. Tell me which one and I will change it."
            ),
            signals=[
                work_signal(
                    label="amend",
                    text="Nothing was started",
                    detail="ambiguous_amend_target",
                    kind="status",
                    importance="blocking",
                )
            ],
            importance="blocking",
            metadata={
                "amend_ambiguous": True,
                "narration_keypoint": "execution_blocked",
            },
            speak=True,
        )
        add_work_note(note)
        await bus.emit(Method.CHAT_WORK_NOTE, note)
    except Exception:
        logger.exception("failed to ask which task an amendment meant")


async def _announce_amend_missing(filename: str) -> None:
    """Say that an explicit amendment was blocked instead of inventing work."""

    try:
        from core import session_manager as sm
        from server.ai_os_schema import work_note_payload, work_signal
        from server.event_bus import bus
        from server.protocol import Method
        from server.work_context import add_work_note

        clean_name = str(filename or "").strip() or "that file"
        note = work_note_payload(
            source="workspace_router",
            provider="host",
            run_id=f"amend_{time.time_ns()}",
            session_id=sm.get_current_session_id() or "",
            phase="Checkpoint",
            title="Existing file not found",
            summary=(
                f"I could not find a tracked task that produced {clean_name}, "
                "so I did not create a replacement in a new workspace."
            ),
            signals=[
                work_signal(
                    label="amend",
                    text="Nothing was started",
                    detail="missing_amend_target",
                    kind="status",
                    importance="blocking",
                )
            ],
            importance="blocking",
            metadata={
                "amend_missing": True,
                "filename": clean_name,
                "narration_keypoint": "execution_blocked",
            },
            speak=True,
        )
        add_work_note(note)
        await bus.emit(Method.CHAT_WORK_NOTE, note)
    except Exception:
        logger.exception("failed to announce a missing amendment target")


# How long the answer may wait for the floor. It is waiting for one turn to
# finish streaming and playing, and a rich completion narration ran about 20
# seconds on a real machine, so this clears a long turn with room to spare.
# The old 120 was not a considered number: past roughly half a minute the
# conversation has moved on and a status answer is no longer an answer, it is
# an interruption about something the user stopped asking.
_ANSWER_IDLE_TIMEOUT_S = 30.0


async def _wait_for_output_idle(
    timeout_s: float | None = None,
    poll_s: float = 0.25,
) -> bool:
    """Wait until chat streaming and TTS playback are both idle.

    The answering pass is its own conversational turn, and starting one clears
    the sentence queue — doing that while the tag-bearing turn is still being
    spoken would cut its playback off mid-sentence. These are the same busy
    signals the WorkObserver trusts before narrating.

    The budget is read at call time rather than bound as a default, so it stays
    one number that tests and operators can move.
    """

    probe = output_idle_probe
    if probe is None:
        return True
    budget = _ANSWER_IDLE_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + max(1.0, float(budget))
    while time.monotonic() < deadline:
        try:
            if probe():
                return True
        except Exception:
            # An unreadable busy signal is not a reason to withhold the
            # answer; the worst case is speaking a moment early.
            return True
        await asyncio.sleep(max(0.05, float(poll_s)))
    return False


async def _announce_report_unanswered(
    title: str,
    summary: str,
    *,
    reason: str,
    count: int,
) -> None:
    """Ask or admit rather than leave "let me check" standing unmet.

    By the time resolution fails the character has already promised to look
    something up. Saying which tasks could be meant — in titles, never ids —
    or that none exists is the only honest way to close that promise.
    """

    try:
        from core import session_manager as sm
        from server.ai_os_schema import work_note_payload, work_signal
        from server.event_bus import bus
        from server.protocol import Method
        from server.work_context import add_work_note

        note = work_note_payload(
            source="workspace_router",
            provider="host",
            run_id=f"lookup_{time.time_ns()}",
            session_id=sm.get_current_session_id() or "",
            phase="Result",
            title=title,
            summary=summary,
            signals=[
                work_signal(
                    label="lookup",
                    text="The question was not answered from the ledger",
                    detail=reason,
                    kind="status",
                    importance="blocking",
                )
            ],
            importance="blocking",
            metadata={
                "lookup_unanswered": True,
                "reason": reason,
                "candidate_count": count,
            },
            speak=True,
        )
        add_work_note(note)
        await bus.emit(Method.CHAT_WORK_NOTE, note)
    except Exception:
        logger.exception("failed to announce an unanswered task lookup")


async def _speak_task_lookup_answer(
    answer: str,
    *,
    voice_text_ja: str | None = "",
    history_marker: str = "TASK_STATUS",
    source: str = "work_ledger_status",
    log_label: str = "TASK-LOOKUP",
    session_id: str = "",
    message_id: str = "",
    delivery_observer=None,
) -> bool:
    """Publish an already-authored read-only answer on the character lane.

    This boundary owns delivery identity, history and TTS timing. It does not
    decide how WorkItem status is worded; the normal status path arrives here
    after the Work Narrator has expressed the Host-resolved facts.
    """

    answer = str(answer or "").strip()
    if not answer:
        return False
    # Capture delivery identity before waiting for the shared speech floor.
    # A later Session switch must not make this answer appear in whichever
    # conversation happens to be current at emit time.
    target_session_id = str(session_id or "").strip()
    session_manager_module = None
    try:
        from core import session_manager as session_manager_module

        if not target_session_id:
            target_session_id = str(
                session_manager_module.get_current_session_id() or ""
            ).strip()
    except Exception:
        session_manager_module = None
    target_message_id = str(message_id or "").strip() or (
        f"host-answer:{str(source or 'answer')}:{time.time_ns()}"
    )
    from server.event_bus import bus
    from server.protocol import Method

    floor_available = await _wait_for_output_idle()
    if not floor_available:
        logger.warning(
            "[%s] publishing text-only ledger answer after %.0fs busy floor",
            log_label,
            _ANSWER_IDLE_TIMEOUT_S,
        )
    speech_status = "text_only_busy"
    if floor_available and host_readonly_voice_sink is not None:
        try:
            direct_voice = (
                {}
                if voice_text_ja is None
                else {"voice_text_ja": str(voice_text_ja or answer)}
            )
            receipt = host_readonly_voice_sink(
                {
                    "display_text": answer,
                    **direct_voice,
                    "emotion": "thinking",
                    "source": source,
                    "action": "assistant_reply",
                    "terminal": False,
                    "turn_id": target_message_id,
                    "complete_turn": True,
                }
            )
            if hasattr(receipt, "__await__"):
                receipt = await receipt
            speech_status = str(
                receipt.get("status") if isinstance(receipt, dict) else "queued"
            )
        except Exception:
            speech_status = "enqueue_error"
            logger.exception("[%s] deterministic answer TTS failed", log_label)
    try:
        marker = str(history_marker or "TASK_STATUS").strip().strip("[]")
        current_session_id = str(
            session_manager_module.get_current_session_id()
            if session_manager_module is not None
            else ""
        ).strip()
        if not target_session_id or current_session_id == target_session_id:
            from core.session_manager import conversation_history

            conversation_history.add_assistant(f"[{marker}]\n{answer}")
    except Exception:
        logger.exception("failed to append ledger answer to conversation history")
    await bus.emit(
        Method.CHAT_OBSERVER_DECISION,
        {
            "source": source,
            "session_id": target_session_id,
            "message_id": target_message_id,
            "action": "assistant_reply",
            "terminal": False,
            "append_to_main_chat": True,
            "speak": speech_status in {"queued", "queued_legacy_sink"},
            "speech_status": speech_status,
            "display_text": answer,
            "main_chat_entry": answer,
        },
    )
    if callable(delivery_observer):
        try:
            observed = delivery_observer(
                {
                    "session_id": target_session_id,
                    "message_id": target_message_id,
                    "speech_status": speech_status,
                    "speak": speech_status in {"queued", "queued_legacy_sink"},
                }
            )
            if hasattr(observed, "__await__"):
                await observed
        except Exception:
            logger.exception("[%s] answer delivery observer failed", log_label)
    logger.info(
        "[%s] report published source=%s chars=%d speech=%s",
        log_label,
        source,
        len(answer),
        speech_status,
    )
    return True


def _schedule_focus_confirmation(
    display_text: str,
    voice_text_ja: str,
    *,
    status: str,
) -> None:
    """Narrate the host-confirmed focus result after the role turn yields."""

    async def publish() -> None:
        await _speak_task_lookup_answer(
            display_text,
            voice_text_ja=voice_text_ja,
            history_marker="FOCUS_RESULT",
            source="session_focus_result",
            log_label="FOCUS-RESULT",
        )

    try:
        task = asyncio.create_task(
            publish(),
            name=f"focus-result-{str(status or 'result')}",
        )
    except RuntimeError:
        logger.warning("[FOCUS-RESULT] no event loop available for confirmation")
        return
    _focus_confirmation_tasks.add(task)
    task.add_done_callback(_focus_confirmation_tasks.discard)


async def _answer_work_item_status(
    row: dict,
    *,
    session_id: str,
) -> bool:
    """Resolve facts in the Host, then let the existing Narrator say them."""

    from server import task_lookup

    runtime = work_status_narrator
    if runtime is not None:
        note = task_lookup.status_query_narration_note(
            row,
            session_id=session_id,
        )
        try:
            runtime.supersede_for_status_query(str(row.get("work_item_id") or ""))
            decision = await runtime.compose_status_query_reply(note)
        except Exception:
            decision = None
            logger.exception("[TASK-LOOKUP] Work Narrator status reply failed")
        if isinstance(decision, dict):
            answer = str(
                decision.get("display_text")
                or decision.get("main_chat_entry")
                or ""
            ).strip()
            if answer:
                language = str(
                    decision.get("display_language") or _observer_display_language()
                ).strip().lower().replace("-", "_")
                direct_voice = (
                    answer
                    if language in {"ja", "ja_jp", "japanese", "日本語"}
                    else None
                )

                def record_delivery(delivery: dict) -> None:
                    runtime.record_status_query_delivery(
                        note,
                        decision,
                        delivery,
                    )

                return await _speak_task_lookup_answer(
                    answer,
                    voice_text_ja=direct_voice,
                    history_marker="TASK_STATUS",
                    source="work_status_narrator",
                    log_label="TASK-LOOKUP",
                    session_id=session_id,
                    delivery_observer=record_delivery,
                )

    # A missing or failed Narrator is an observable outage fallback, not the
    # ordinary wording path. Ledger facts still deserve a truthful text answer.
    logger.warning(
        "[TASK-LOOKUP] Work Narrator unavailable; using emergency status wording"
    )
    answer, voice_text_ja = task_lookup.render_current_status_answer(
        row,
        display_language=_observer_display_language(),
    )
    return await _speak_task_lookup_answer(
        answer,
        voice_text_ja=voice_text_ja,
        history_marker="TASK_STATUS",
        source="work_ledger_status_fallback",
        log_label="TASK-LOOKUP-FALLBACK",
        session_id=session_id,
    )


async def _answer_report_from_ledger(task_text: str, attrs: dict) -> str:
    """The answering half intent="report" never had.

    Refusing to start work was implemented on day one; the log line about
    answering from the ledger was aspiration. This resolves which task the
    user meant (reusing the pre-turn resolution when it already ran on this
    utterance), fetches that task's state, and lets the character say it.
    """

    from server.project_report import answer_project_report, normalize_report_subject

    subject = normalize_report_subject(attrs.get("subject"))
    if subject is None:
        display = "这次状态查询的对象无法识别；我没有启动任何工作。"
        voice_ja = "今回の状態照会の対象を識別できませんでした。作業は開始していません。"
        if _observer_display_language() == "japanese":
            display = voice_ja
        await _speak_task_lookup_answer(
            display,
            voice_text_ja=voice_ja,
            history_marker="LEDGER_STATUS",
            source="work_ledger_status",
            log_label="REPORT-QUERY",
        )
        logger.warning("[REPORT-QUERY] rejected unknown report subject=%r", attrs.get("subject"))
        return "[report] invalid subject"

    if subject == "project":
        from server.work_ledger_coordinator import get_work_ledger_coordinator

        answer = answer_project_report(
            get_work_ledger_coordinator(),
            project_id=str(attrs.get("project_id") or attrs.get("projectId") or ""),
            display_language=_observer_display_language(),
        )
        published = await _speak_task_lookup_answer(
            answer.display_text,
            voice_text_ja=answer.voice_text_ja,
            history_marker="PROJECT_STATUS",
            source="project_ledger_status",
            log_label="PROJECT-LOOKUP",
        )
        if not published:
            return "[report] project answer pass unavailable"
        if answer.status == "not_found":
            return "[report] no such project"
        if answer.status == "unavailable":
            return "[report] project ledger unavailable"
        if answer.status == "empty":
            return "[report] no projects"
        return "[report] answered project from the ledger"

    from core import session_manager as sm
    from server import task_lookup

    session_id = str(attrs.get("lookup_session_id") or "").strip() or str(
        sm.get_current_session_id() or ""
    )
    canonical_work_item_id = str(
        attrs.get("workspace_ref") or attrs.get("workspaceRef") or ""
    ).strip()
    if canonical_work_item_id:
        from server.work_ledger_coordinator import get_work_ledger_coordinator

        coordinator = get_work_ledger_coordinator()
        row = (
            coordinator.bound_work_item_status_row(
                session_id,
                canonical_work_item_id,
            )
            if coordinator is not None and session_id
            else None
        )
        if not isinstance(row, dict):
            await _announce_report_unanswered(
                "I could not read that task",
                (
                    "The selected WorkItem is no longer available in this "
                    "conversation, so I did not substitute a different task."
                ),
                reason="canonical_work_item_unavailable",
                count=0,
            )
            return "[report] canonical task unavailable"
        try:
            row = await coordinator.enrich_report_row(row)
        except Exception:
            logger.exception("[TASK-LOOKUP] canonical activity refresh failed")
        if await _answer_work_item_status(row, session_id=session_id):
            logger.info(
                "[TASK-LOOKUP] canonical report work_item=%s",
                canonical_work_item_id,
            )
            return "[report] answered from canonical ledger identity"
        return "[report] answer pass unavailable"

    question = str(attrs.get("lookup_question") or "").strip() or str(task_text or "").strip()
    if not session_id or not question:
        logger.info("[TASK-LOOKUP] report lacks lookup context; keeping today's behaviour")
        return "[report] no lookup context"
    resolution = task_lookup.peek_turn_resolution(session_id)
    if not (
        isinstance(resolution, dict)
        and str(resolution.get("utterance") or "") == question
        and isinstance(resolution.get("row"), dict)
    ):
        # The pre-turn pass either did not run on these words or found no
        # referent. A status question with no literal overlap ("刚才那个好了
        # 吗") still deserves the second rung over the recent rows — picking
        # stays classification; recency alone never answers.
        resolution = await task_lookup.resolve(
            session_id, question, consumer="report", recency_fallback=True
        )
    row = resolution.get("row")
    if isinstance(row, dict):
        try:
            from server.work_ledger_coordinator import get_work_ledger_coordinator

            coordinator = get_work_ledger_coordinator()
            if coordinator is not None:
                row = await coordinator.enrich_report_row(row)
        except Exception:
            # A stale/missing workspace observation must not erase the durable
            # task facts that were already resolved correctly.
            logger.exception("[TASK-LOOKUP] bounded activity refresh failed")
        if await _answer_work_item_status(row, session_id=session_id):
            return "[report] answered from the ledger"
        return "[report] answer pass unavailable"
    reason = str(resolution.get("reason") or "")
    candidates = [
        candidate
        for candidate in (resolution.get("candidates") or [])
        if isinstance(candidate, dict)
    ]
    if reason == "ambiguous" and candidates:
        from core.chat_runtime import _amend_candidate_label

        titles = ", ".join(
            label
            for label in (_amend_candidate_label(candidate) for candidate in candidates[:4])
            if label
        )
        logger.info("[TASK-LOOKUP] level=3 ask consumer=report n=%d", len(candidates))
        await _announce_report_unanswered(
            "Which task do you mean?",
            (
                "More than one task could be the one you are asking about, so I "
                f"did not guess: {titles}. Tell me which one and I will check."
            ),
            reason="ambiguous_lookup",
            count=len(candidates),
        )
        return "[report] asked which task"
    if reason == "empty":
        drafts = _drafts_in_other_conversations(question)
        if drafts:
            # It exists, it is visible in the task list, and it is only out of
            # reach because it was never kept. Saying "no such task" here would
            # contradict the user's own screen.
            titles = ", ".join(str(draft.get("title") or "") for draft in drafts[:2])
            logger.info(
                "[TASK-LOOKUP] level=3 ask consumer=report n=0 (draft elsewhere n=%d)",
                len(drafts),
            )
            await _announce_report_unanswered(
                "That one is a scratch task",
                (
                    f"I found it outside this conversation ({titles}), but it was left "
                    "as a scratch task, so I cannot pick it up here. Keep it as a "
                    "project from the task list and I can work on it again."
                ),
                reason="lookup_scratch_elsewhere",
                count=len(drafts),
            )
            return "[report] draft outside this conversation"
        logger.info("[TASK-LOOKUP] level=3 ask consumer=report n=0 (no such task)")
        await _announce_report_unanswered(
            "I could not find that task",
            "No task in this conversation produced that file, so there is nothing to report on.",
            reason="lookup_empty",
            count=0,
        )
        return "[report] no such task"
    logger.info("[TASK-LOOKUP] report unresolved reason=%s", reason or "unknown")
    await _announce_report_unanswered(
        "Which task do you mean?",
        "I could not tell which task you are asking about, and I did not guess. "
        "Name the file or the task and I will check.",
        reason=reason or "unresolved_lookup",
        count=len(candidates),
    )
    return "[report] could not resolve the task"


def _drafts_in_other_conversations(question: str) -> list[dict]:
    """Drafts elsewhere matching what was asked. Never routes; only phrases."""

    try:
        from core.chat_runtime import _explicit_file_references
        from server.work_ledger_coordinator import get_work_ledger_coordinator

        coordinator = get_work_ledger_coordinator()
        if coordinator is None:
            return []
        for reference in _explicit_file_references(str(question or "")):
            found = coordinator.drafts_in_other_conversations(reference)
            if found:
                return found
    except Exception:
        # Wording help only: a failure here must not change the answer path.
        logger.debug("draft lookup outside the conversation failed", exc_info=True)
    return []


async def _handle_declared_retract(task_text: str) -> str:
    """Cancel what the user took back, or say plainly that nothing was.

    Never starts work: a withdrawal that spawns a task is the worst variant of
    the chat invariant, and it is exactly what happened before the model had a
    verb for this (2026-07-31, B1: 3 of 5 runs created a third WorkItem).
    """

    from agent_host.provider_runtime import runtime

    active = [
        run
        for run in runtime.list_runs()
        if str(run.get("status") or "").strip().lower() not in _RETRACT_TERMINAL_STATUSES
    ]
    try:
        from server.work_ledger_coordinator import get_work_ledger_coordinator

        recovery_coordinator = get_work_ledger_coordinator()
        pending_recoveries = (
            recovery_coordinator.pending_provider_recoveries()
            if recovery_coordinator is not None
            else []
        )
    except Exception:
        recovery_coordinator = None
        pending_recoveries = []
    active_run_ids = {
        str(run.get("run_id") or "").strip() for run in active
    }
    active_recovery_predecessors = {
        str(recovery.get("predecessor_attempt_id") or "").strip()
        for run in active
        for metadata in [
            run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        ]
        for recovery in [
            metadata.get("provider_recovery")
            if isinstance(metadata.get("provider_recovery"), dict)
            else {}
        ]
        if str(recovery.get("predecessor_attempt_id") or "").strip()
    }
    pending_only = [
        recovery
        for recovery in pending_recoveries
        if str(recovery.get("successor_run_id") or "").strip()
        not in active_run_ids
        and str(recovery.get("attempt_id") or "").strip()
        not in active_recovery_predecessors
    ]
    target_count = len(active) + len(pending_only)
    logger.info(
        "[DELEGATE-RETRACT] declared; active=%d pending_recovery=%d task=%r",
        len(active),
        len(pending_only),
        str(task_text or "")[:120],
    )
    if target_count == 0:
        await _announce_retract_outcome(
            "There was nothing running to stop, so nothing was cancelled.",
            reason="no_active_run",
            count=0,
        )
        return "[retract] nothing was running"
    if target_count > 1:
        # Cancelling the wrong task is not recoverable by asking afterwards, so
        # ask first — the same fail-closed rule the workspace router follows.
        await _announce_retract_outcome(
            "More than one task is running, so I did not guess which to stop. "
            "Tell me which one and I will.",
            reason="ambiguous_active_runs",
            count=target_count,
        )
        return "[retract] several tasks are running"
    if not active:
        pending = pending_only[0]
        attempt_id = str(pending.get("attempt_id") or "").strip()
        cancelled = bool(
            recovery_coordinator is not None
            and recovery_coordinator.cancel_pending_provider_recovery(attempt_id)
        )
        if cancelled:
            logger.info(
                "[DELEGATE-RETRACT] cancelled pending recovery attempt_id=%s",
                attempt_id,
            )
            return "[retract] cancelled"
        # Intake may have made the successor visible between the inventory and
        # compare-and-set. Fall through to the ordinary runtime cancellation if
        # exactly one live run now exists.
        active = [
            run
            for run in runtime.list_runs()
            if str(run.get("status") or "").strip().lower()
            not in _RETRACT_TERMINAL_STATUSES
        ]
        if len(active) != 1:
            await _announce_retract_outcome(
                "That task had already finished before cancellation could take effect.",
                reason="recovery_transition_finished",
                count=len(active),
            )
            return "[retract] task had already finished"
    run_id = str(active[0].get("run_id") or "")
    active_metadata = (
        active[0].get("metadata")
        if isinstance(active[0].get("metadata"), dict)
        else {}
    )
    active_recovery = (
        active_metadata.get("provider_recovery")
        if isinstance(active_metadata.get("provider_recovery"), dict)
        else {}
    )
    active_predecessor = str(
        active_recovery.get("predecessor_attempt_id") or ""
    ).strip()
    if recovery_coordinator is not None:
        for pending in pending_recoveries:
            if (
                str(pending.get("successor_run_id") or "").strip() == run_id
                or active_predecessor
                and str(pending.get("attempt_id") or "").strip()
                == active_predecessor
            ):
                recovery_coordinator.cancel_pending_provider_recovery(
                    str(pending.get("attempt_id") or "")
                )
    result = await runtime.cancel(run_id)
    cancelled = bool(result.get("cancelled"))
    reason = str(result.get("reason") or "").strip()
    logger.info(
        "[DELEGATE-RETRACT] cancel run_id=%s cancelled=%s reason=%s",
        run_id,
        cancelled,
        reason,
    )
    if not cancelled:
        refreshed = next(
            (
                run
                for run in runtime.list_runs()
                if str(run.get("run_id") or "") == run_id
            ),
            {},
        )
        refreshed_status = str(refreshed.get("status") or "").strip().lower()
        if refreshed_status == "cancelled":
            return "[retract] cancelled"
        if refreshed_status in {"done", "error"}:
            await _announce_retract_outcome(
                "That task had already finished before cancellation could take effect.",
                reason=reason or f"already_{refreshed_status}",
                count=1,
            )
            return "[retract] task had already finished"
        # A provider can accept the signal while confirmation is still racing
        # the terminal event.  The ledger owns the eventual cancelled/done
        # narration; claiming either outcome here produced two contradictory
        # voice reports in the 2026-08-07 real run.
        logger.info(
            "[DELEGATE-RETRACT] awaiting terminal confirmation run_id=%s status=%s reason=%s",
            run_id,
            refreshed_status or "unknown",
            reason or "cancel_unconfirmed",
        )
        return "[retract] cancellation requested; awaiting terminal confirmation"
    return "[retract] cancelled"


def _delegate_mode_for_provider(
    provider: str,
    attrs: dict,
    action: str,
    task_text: str = "",
    *,
    workspace_access: str = "",
) -> str:
    explicit_mode = str(attrs.get("mode") or "").strip().lower()
    action_mode = str(action or "").strip().lower()
    if explicit_mode:
        return explicit_mode
    if action_mode:
        return action_mode
    return "delegate"


def _english_mutation_is_negated(text: str, start: int) -> bool:
    """Return whether one mutation verb belongs to a local negative constraint.

    The workspace guard must not turn "do not write files" into positive write
    authority.  This is deliberately clause-local: punctuation and explicit
    contrast/sequence words end the negative scope, so a later positive action
    in the same utterance still requires a writable Provider.
    """

    prefix = text[max(0, start - 120) : start]
    clause = re.split(r"[.;!?]", prefix)[-1]
    clause = re.split(
        r"\b(?:but|however|instead|then|rather)\b",
        clause,
    )[-1]
    return bool(
        re.search(
            r"\b(?:do\s+not|don't|must\s+not|need\s+not|never|no\s+need\s+to)\b"
            r"(?:(?!\b(?:but|however|instead|then|rather)\b).)*$",
            clause,
        )
    )


_ENGLISH_FILE_MUTATION_PATTERN = re.compile(
    r"\b(?:add|build|change|copy|create|delete|develop|edit|export|fix|generate|"
    r"implement|make|modify|move|patch|refactor|remove|rename|replace|save|update|write)\b",
)


def _cjk_mutation_is_negated(text: str, start: int, end: int) -> bool:
    """Apply the same clause-local rule to Chinese/Japanese mutation words."""

    prefix = re.split(r"[;；,，、。！？.!?]", text[max(0, start - 48) : start])[-1]
    if re.search(
        r"(?:不要|不得|不能|禁止|无需|無需|不需要|别|別)"
        r"[^;；,，、。！？.!?]{0,24}$",
        prefix,
    ):
        return True
    return bool(
        re.match(
            r"[^;；。！？.!?]{0,20}(?:しない|しません|せず|ないで|禁止)",
            text[end : end + 28],
        )
    )


def _has_positive_cjk_mutation(text: str, markers: tuple[str, ...]) -> bool:
    return any(
        not _cjk_mutation_is_negated(text, match.start(), match.end())
        for marker in markers
        for match in re.finditer(re.escape(marker), text)
    )


def _task_requests_file_mutation(task_text: str) -> bool:
    """Return whether a task asks to create or mutate code/files.

    This intentionally requires both a mutation verb and a code/file marker so
    ordinary actions such as "create a user profile" keep their existing
    provider routing.
    """
    normalized = " ".join(str(task_text or "").lower().split())
    english_strong_code = re.search(
        r"\b(?:api|cli|code|codebase|css|go|html|java|javascript|module|node|php|"
        r"program|python|react|repository|repo|ruby|rust|script|source\s+code|sql|"
        r"typescript|vue)\b",
        normalized,
    )
    strong_code_markers = (
        "代码",
        "程式",
        "程序",
        "脚本",
        "代码库",
        "程式碼",
        "源码",
        "源代码",
        "仓库",
        "倉庫",
        "模块",
        "模組",
        "接口",
        "コード",
        "プログラム",
        "スクリプト",
    )
    english_weak_software = re.search(
        r"\b(?:app|application|button|chess|component|game|gui|tool|ui)\b",
        normalized,
    )
    cjk_weak_software_markers = (
        "国际象棋",
        "國際象棋",
        "游戏",
        "遊戲",
        "应用",
        "應用",
        "工具",
        "界面",
        "チェス",
        "ゲーム",
    )
    english_mutation = next(
        (
            match
            for match in _ENGLISH_FILE_MUTATION_PATTERN.finditer(normalized)
            if not _english_mutation_is_negated(normalized, match.start())
        ),
        None,
    )
    mutation_markers = (
        "生成",
        "创建",
        "創建",
        "寫",
        "写",
        "添加",
        "新增",
        "新建",
        "增加",
        "删除",
        "刪除",
        "移除",
        "修改",
        "更改",
        "改一下",
        "改下",
        "重命名",
        "重新命名",
        "移动",
        "移動",
        "复制",
        "複製",
        "导出",
        "導出",
        "输出",
        "輸出",
        "替换",
        "替換",
        "存到",
        "存入",
        "存为",
        "存為",
        "开发",
        "開發",
        "制作",
        "製作",
        "构建",
        "構建",
        "实现",
        "實現",
        "修复",
        "修復",
        "更新",
        "保存",
        "儲存",
        "重构",
        "重構",
        "编辑",
        "編輯",
        "给我做",
        "帮我做",
        "幫我做",
        "做个",
        "做個",
        "做一个",
        "做一個",
        "作成",
        "実装",
        "編集",
    )
    file_markers = (
        "文件",
        "文件夹",
        "檔案",
        "資料夾",
        "ファイル",
        "フォルダ",
    )
    named_file = re.search(
        r"(?<![\w.])[\w@()+-][\w@().+-]*\."
        r"(?:c|cc|conf|cpp|cs|css|csv|cfg|go|h|hpp|html|ini|ipynb|java|js|jsx|"
        r"json|md|markdown|php|ps1|py|rb|rs|sh|sql|svg|toml|ts|tsx|tsv|txt|xml|"
        r"yaml|yml)\b",
        normalized,
    )
    named_file_kind = re.search(
        r"\b(?:config|directory|file|files|folder|readme)\b",
        normalized,
    )
    read_only_change_context = re.search(
        r"(?:解释|說明|说明|查看|浏览|瀏覽|审查|審查|分析|总结|總結|展示|列出)"
        r".{0,40}(?:修改|更改|变更|變更).{0,12}(?:历史|歷史|记录|紀錄|日志|日誌)",
        normalized,
    )
    usage_question = re.search(
        r"(?:python|代码|程式|程序|脚本).{0,16}做什么(?:用|用途|的)?",
        normalized,
    )
    if read_only_change_context is not None or usage_question is not None:
        return False
    mutates_files = english_mutation is not None or _has_positive_cjk_mutation(
        normalized,
        mutation_markers,
    )
    strong_file_context = (
        english_strong_code is not None
        or any(marker in normalized for marker in strong_code_markers)
        or named_file_kind is not None
        or any(marker in normalized for marker in file_markers)
        or named_file is not None
    )
    if mutates_files and strong_file_context:
        return True

    # A precise Desktop output request is file work even when the prompt calls
    # the deliverable only "the result".  The shared export detector rejects
    # Desktop as an input, reference, runtime platform, or negated target.
    if any(marker in normalized for marker in ("desktop", "桌面", "デスクトップ")):
        from server.work_export_service import WorkExportService

        if WorkExportService._has_desktop_destination(normalized):
            return True

    # app/game/UI are weak evidence by themselves: the same words occur in
    # account management, Steam updates, and window movement.  Require an
    # explicit creation/development verb and reject nearby external objects.
    weak_software_context = english_weak_software is not None or any(
        marker in normalized for marker in cjk_weak_software_markers
    )
    english_development = re.search(
        r"\b(?:build|create|develop|generate|implement|make|refactor|write)\b",
        normalized,
    )
    english_external_object = re.search(
        r"\b(?:app|application|game|tool|ui)\b.{0,24}"
        r"\b(?:account|profile|setting|shortcut|steam|user|window)\b|"
        r"\b(?:account|profile|setting|shortcut|steam|user|window)\b.{0,24}"
        r"\b(?:app|application|game|tool|ui)\b",
        normalized,
    )
    cjk_development_markers = (
            "创建",
            "創建",
            "开发",
            "開發",
            "制作",
            "製作",
            "构建",
            "構建",
            "实现",
            "實現",
            "重构",
            "重構",
            "给我做",
            "帮我做",
            "幫我做",
            "做个",
            "做個",
            "做一个",
            "做一個",
            "作成",
            "実装",
        )
    cjk_development = _has_positive_cjk_mutation(
        normalized,
        cjk_development_markers,
    )
    cjk_external_object = re.search(
        r"(?:应用|應用|游戏|遊戲|界面).{0,12}(?:账户|帳戶|账号|帳號|用户|用戶|窗口|視窗|设置|設定)|"
        r"(?:账户|帳戶|账号|帳號|用户|用戶|窗口|視窗|设置|設定).{0,12}(?:应用|應用|游戏|遊戲|界面)",
        normalized,
    )
    return bool(
        weak_software_context
        and english_external_object is None
        and cjk_external_object is None
        and (english_development is not None or cjk_development)
    )


def _delegate_provider_selection(
    task_text: str,
    attrs: dict,
    *,
    manifests=None,
):
    """Translate host-owned task facts into requirements, then select.

    Workspace mutation is a capability requirement, not a provider identity.
    Any registered conforming provider can compete without changing this
    function or the model prompt.
    """

    from agent_host.provider_contract import ProviderSelectionError, select_provider
    from agent_host.provider_runtime import runtime as provider_runtime
    from server.provider_requirements import (
        DelegateRequirementFacts,
        compile_delegate_requirements,
    )

    source_user_text = str(attrs.get("_host_source_user_text") or "").strip()
    requested_provider = str(attrs.get("provider") or "").strip()
    continuation_facts: dict = {}
    workspace_ref = str(
        attrs.get("workspace_ref")
        or attrs.get("workspaceRef")
        or attrs.get("work_item_id")
        or attrs.get("workItemId")
        or ""
    ).strip()
    if workspace_ref:
        try:
            from server.work_ledger_coordinator import get_work_ledger_coordinator

            coordinator = get_work_ledger_coordinator()
            if coordinator is not None:
                continuation_facts = coordinator.continuation_routing_facts(
                    workspace_ref
                )
        except Exception:
            logger.exception("failed to read bounded WorkItem routing facts")
    from agent_host.browser_request_contract import web_addresses

    facts = DelegateRequirementFacts.from_delegate(
        attrs,
        # Once ControlDecision has named a Provider, its model-authored task is
        # an execution payload, not a second capability authority. The exact
        # user turn and durable target facts own requirements; task prose stays
        # as the compatibility fallback for legacy provider-less delegates.
        task_requests_workspace_mutation=(
            _task_requests_file_mutation(task_text)
            if not (source_user_text and requested_provider)
            else False
        ),
        source_requests_workspace_mutation=_task_requests_file_mutation(
            source_user_text
        ),
        target_workspace_mode=str(
            continuation_facts.get("workspace_mode") or ""
        ),
        continuation_provider=str(
            continuation_facts.get("provider") or ""
        ),
        source_has_browser_address=bool(
            web_addresses(source_user_text, allow_bare_domain=True)
        ),
        required_workspace_access=str(
            attrs.get("_host_workspace_access") or ""
        ),
    )
    requirements = compile_delegate_requirements(facts)
    default_provider = str(
        getattr(settings, "PROVIDER_DELEGATE_DEFAULT_PROVIDER", "openclaw")
        or "openclaw"
    ).strip().lower()
    available_manifests = (
        tuple(manifests)
        if manifests is not None
        else provider_runtime.provider_manifests()
    )
    if not available_manifests:
        raise ProviderSelectionError("provider registry is not initialized")
    selection = select_provider(
        requirements,
        available_manifests,
        default_provider=default_provider,
    )
    return requirements, selection


def _delegate_provider_for_task(task_text: str, attrs: dict, *, manifests=None) -> str:
    _requirements, selection = _delegate_provider_selection(
        task_text,
        attrs,
        manifests=manifests,
    )
    return selection.provider_id


def _delegate_declared_report_only(attrs: dict) -> bool:
    """True when the model declared this turn is about reporting, not doing.

    Only honoured while the attribute is part of the contract; otherwise an
    absent declaration would be indistinguishable from a model that never
    learned to make one.
    """

    from config import settings as _settings

    if not bool(getattr(_settings, "DELEGATE_INTENT_ATTRIBUTE", False)):
        return False
    return str(attrs.get("intent") or "").strip().lower() == "report"


def _delegate_declared_retract(attrs: dict) -> bool:
    """True when the model declared this turn takes running work back.

    Gated on both flags: the value is meaningless unless the attribute is part
    of the contract, and the verb is only offered while the host is wired to
    act on it.
    """

    from config import settings as _settings

    if not bool(getattr(_settings, "DELEGATE_INTENT_ATTRIBUTE", False)):
        return False
    if not bool(getattr(_settings, "DELEGATE_RETRACT_INTENT", False)):
        return False
    return str(attrs.get("intent") or "").strip().lower() == "retract"


def _delegate_declared_focus(attrs: dict) -> bool:
    """True when the model was told which project this conversation is on."""

    from config import settings as _settings

    if not bool(getattr(_settings, "DELEGATE_INTENT_ATTRIBUTE", False)):
        return False
    if not bool(getattr(_settings, "DELEGATE_FOCUS_INTENT", False)):
        return False
    return str(attrs.get("intent") or "").strip().lower() == "focus"


def _delegate_focus_modifier(attrs: dict) -> str:
    """Return an explicit persistent-destination modifier, when enabled.

    ``intent`` describes the operation performed now. ``focus`` is an
    orthogonal side effect on later turns, so compound requests do not have to
    mislabel execute/amend/report work as a control operation. The existing
    feature gates own both spellings during the compatibility migration.
    """

    from config import settings as _settings

    if not bool(getattr(_settings, "DELEGATE_INTENT_ATTRIBUTE", False)):
        return ""
    if not bool(getattr(_settings, "DELEGATE_FOCUS_INTENT", False)):
        return ""
    modifier = str(attrs.get("focus") or "").strip().lower()
    return modifier if modifier in {"set", "clear"} else ""


async def _handle_declared_focus(
    attrs: dict,
    *,
    announce_result: bool = True,
) -> dict[str, object]:
    """Set or clear the conversation's project and publish the result.

    Asking which project on every instruction lands 2-4 times in 12 and no
    wording moves it, because the references that fail -- "this project", a bare
    filename -- point at things the prompt does not contain. Said once it lands
    6 times in 6, and the working turns after it never repeat the project, so
    the host is necessarily the one that has to remember.
    """

    from core import session_manager as sm
    from server.work_ledger_coordinator import get_work_ledger_coordinator

    project_id = str(attrs.get("project_id") or attrs.get("projectId") or "").strip()
    coordinator = get_work_ledger_coordinator()
    session_id = sm.get_current_session_id() or ""
    if coordinator is None or not session_id:
        logger.info(
            "[WORK-DESTINATION] focus declared but unusable: project=%r session=%r",
            project_id,
            session_id,
        )
        return {"ok": False, "message": "[focus] no active session to update"}
    if not project_id:
        coordinator.clear_session_project(session_id)
        await coordinator.publish_snapshot(reason="session_project.cleared")
        if announce_result:
            display = "已回到本会话的 Draft；后续未指定项目的工作会留在这里。"
            voice_ja = "この会話の Draft に戻したわ。次の指定なしの作業は、ここに残る。"
            if _observer_display_language() == "japanese":
                display = voice_ja
            _schedule_focus_confirmation(display, voice_ja, status="cleared")
        return {"ok": True, "message": "[focus] future work will use Drafts"}
    try:
        chosen = coordinator.set_session_project(session_id, project_id)
    except Exception as exc:
        logger.warning("[WORK-DESTINATION] focus refused: %s", exc)
        reason = str(exc).lower()
        if "unknown project" in reason:
            message = "That project is no longer registered, so the destination was not changed."
        elif "no longer exists" in reason:
            message = "That project folder is no longer available, so the destination was not changed."
        else:
            message = "The project switch was refused; the previous destination is still active."
        coordinator.set_session_project_feedback(
            session_id,
            status="rejected",
            message=message,
        )
        await coordinator.publish_snapshot(reason="session_project.rejected")
        if announce_result:
            display = "项目没有切换成功；我保留了原来的工作位置。"
            voice_ja = "プロジェクトの切り替えは失敗したわ。元の作業先はそのままにしてある。"
            if _observer_display_language() == "japanese":
                display = voice_ja
            _schedule_focus_confirmation(display, voice_ja, status="rejected")
        return {"ok": False, "message": f"[focus] {message}"}
    await coordinator.publish_snapshot(reason="session_project.changed")
    if announce_result:
        project_name = str(chosen.get("projectName") or "").strip() or "项目"
        display = f"已经确认切换到“{project_name}”项目，接下来的项目工作会从这里继续。"
        voice_ja = f"「{project_name}」プロジェクトへの切り替えを確認したわ。次の作業はここから続ける。"
        if _observer_display_language() == "japanese":
            display = voice_ja
        _schedule_focus_confirmation(display, voice_ja, status="changed")
    return {
        "ok": True,
        "message": f"[focus] now working in {chosen['projectName']}",
    }


def _recent_dialog_for_reference() -> list[dict[str, str]]:
    try:
        from core.session_manager import conversation_history

        return [
            {
                "role": str(message.get("role") or ""),
                "content": str(message.get("content") or "")[:1200],
            }
            for message in list(conversation_history.dialog)[-8:]
            if isinstance(message, dict)
            and str(message.get("role") or "") in {"user", "assistant"}
            and str(message.get("content") or "").strip()
        ]
    except Exception:
        logger.exception("failed to collect chat context for reference lookup")
        return []


async def _resume_reference_selection(plan) -> dict[str, object]:
    """Apply the single host continuation claimed by an attention option."""

    from core import session_manager as sm
    from server.work_ledger_coordinator import get_work_ledger_coordinator

    session_id = sm.get_current_session_id() or ""
    if not session_id or session_id != str(plan.session_id or ""):
        raise RuntimeError("the selected reference no longer belongs to the active Session")
    if plan.kind == "delegate":
        resume_attrs = dict(plan.attrs)
        # The explicit Slice choice is acknowledged below. Mark only this
        # continuation so a pure focus does not also emit the ordinary
        # automatic focus confirmation. Immediate unique resolution never
        # carries this flag and therefore still reports its verified result.
        resume_attrs["_host_reference_selection_resumed"] = True
        result = await _handle_delegate(plan.task_text, resume_attrs)
    elif plan.kind == "bind_work_item":
        coordinator = get_work_ledger_coordinator()
        candidate = plan.candidate
        if coordinator is None or not session_id or not candidate.parent_project_id:
            raise RuntimeError("the selected WorkItem cannot be bound to this Session")
        result = coordinator.bind_session_context(
            session_id,
            candidate.parent_project_id,
            work_item_id=candidate.entity_id,
            source="reference_selection",
        )
        await coordinator.publish_snapshot(reason="reference_selection.bound")
    else:
        result = {"status": "acknowledged"}

    await _speak_task_lookup_answer(
        plan.display_text,
        voice_text_ja=plan.voice_text_ja,
        history_marker="REFERENCE_SELECTION",
        source="reference_selection",
        log_label="REFERENCE-SELECTION",
    )
    return {
        "status": "resumed" if plan.kind == "delegate" else plan.kind,
        "result": result,
    }


async def _adjudicate_delegate_reference(
    task_text: str,
    attrs: dict,
) -> tuple[str, str, dict]:
    """Resolve one frozen entity decision before any destination side effect."""

    from server.control_decision import CONTROL_REFERENCE_CANDIDATES_ATTR
    from server.reference_catalog import TypedReferenceCandidate

    missing = object()
    frozen_references = attrs.pop(CONTROL_REFERENCE_CANDIDATES_ATTR, missing)
    has_frozen_references = frozen_references is None or (
        isinstance(frozen_references, tuple)
        and all(
            isinstance(candidate, TypedReferenceCandidate)
            for candidate in frozen_references
        )
    )
    if frozen_references is not missing and not has_frozen_references:
        logger.warning("discarded an invalid host control-reference handoff")

    if not bool(getattr(settings, "REFERENCE_CLARIFICATION_ENABLED", False)):
        return "bypass", task_text, attrs
    if attrs.get("_host_reference_resolved") is True:
        return "bypass", task_text, attrs

    from core import session_manager as sm
    from server.reference_clarification import (
        adjudicate_focus_reference,
        clarification_announcement,
        create_reference_selection,
        default_message_query,
        plan_resume,
    )
    from server.work_ledger_coordinator import get_work_ledger_coordinator

    session_id = sm.get_current_session_id() or ""
    coordinator = get_work_ledger_coordinator()
    if not session_id or coordinator is None:
        return "bypass", task_text, attrs
    clears_or_replaces_pending = (
        (
            str(attrs.get("focus") or "").strip().lower() == "clear"
            and (not has_frozen_references or frozen_references is None)
        )
        or (
            str(attrs.get("intent") or "").strip().lower() == "focus"
            and not str(
                attrs.get("project_id") or attrs.get("projectId") or ""
            ).strip()
            # Legacy taskless focus used the absence of project_id as clear.
            # A ControlDecision handoff has an explicit three-state reference
            # result: null is "no existing target", not permission to clear a
            # durable Session binding. Canonical clear must say focus=clear.
            and not has_frozen_references
        )
    )
    if clears_or_replaces_pending:
        from server.attention_request import attention_requests

        await attention_requests.cancel_matching(
            session_id=session_id,
            dedupe_key="reference_clarification",
        )
        return "bypass", task_text, attrs

    if has_frozen_references:
        if frozen_references is None:
            needs_existing_entity = (
                str(attrs.get("intent") or "").strip().lower() == "focus"
                or str(attrs.get("focus") or "").strip().lower() == "set"
            )
            if not needs_existing_entity:
                return "bypass", task_text, attrs
            adjudication_status = "blocked"
            adjudication_reason = "control decision provided no existing entity target"
            adjudication_request = None
        elif not frozen_references:
            adjudication_status = "blocked"
            adjudication_reason = "no typed reference candidate matched the request"
            adjudication_request = None
        elif len(frozen_references) > 1:
            adjudication_request = await create_reference_selection(
                session_id=session_id,
                task_text=task_text,
                attrs=attrs,
                candidates=frozen_references,
                resume=_resume_reference_selection,
            )
            adjudication_status = "deferred"
            adjudication_reason = ""
        else:
            plan = plan_resume(
                session_id=session_id,
                task_text=task_text,
                attrs=attrs,
                candidate=frozen_references[0],
            )
            if plan.kind == "delegate":
                return "resolved", plan.task_text, dict(plan.attrs)
            await _resume_reference_selection(plan)
            return "handled", "", attrs
    else:
        source_text = " ".join(
            str(attrs.get("_host_source_user_text") or "").split()
        )
        if not source_text:
            # Internal callers and legacy tests without the originating
            # utterance cannot be re-linked safely. Existing host validation
            # still applies.
            return "bypass", task_text, attrs
        adjudication = await adjudicate_focus_reference(
            coordinator=coordinator,
            session_id=session_id,
            utterance=source_text,
            task_text=task_text,
            attrs=attrs,
            query=default_message_query,
            resume=_resume_reference_selection,
            history=_recent_dialog_for_reference(),
        )
        if adjudication.status in {"bypass", "resolved"}:
            resolved_task = "" if dict(adjudication.attrs).pop(
                "_host_reference_taskless", False
            ) else task_text
            resolved_attrs = dict(adjudication.attrs)
            resolved_attrs.pop("_host_reference_taskless", None)
            return adjudication.status, resolved_task, resolved_attrs
        adjudication_status = adjudication.status
        adjudication_reason = adjudication.reason
        adjudication_request = adjudication.request
    if adjudication_status == "deferred":
        display, voice_ja = clarification_announcement()
        await _speak_task_lookup_answer(
            display,
            voice_text_ja=voice_ja,
            history_marker="REFERENCE_CLARIFICATION",
            source="reference_clarification",
            log_label="REFERENCE-CLARIFICATION",
        )
        logger.info(
            "[REFERENCE-CLARIFICATION] deferred session=%s request=%s candidates=%d",
            session_id,
            str((adjudication_request or {}).get("id") or ""),
            len((adjudication_request or {}).get("options") or []),
        )
        return "deferred", task_text, attrs

    logger.warning(
        "[REFERENCE-CLARIFICATION] blocked before focus side effect: %s",
        adjudication_reason,
    )
    await _speak_task_lookup_answer(
        "这个操作没有可靠地对应到现有 Project 或当前会话的 WorkItem，所以我没有执行这个操作，也没有更改它的会话目标。",
        voice_text_ja=(
            "この操作の対象を既存の Project または現在の会話の WorkItem に安全に対応できなかったため、"
            "この操作は実行せず、会話の対象も変更していません。"
        ),
        history_marker="REFERENCE_BLOCKED",
        source="reference_clarification",
        log_label="REFERENCE-CLARIFICATION",
    )
    return "blocked", task_text, attrs


async def _defer_ambiguous_amend(task_text: str, attrs: dict) -> bool:
    """Turn grounded WorkItem ambiguity into the same structured choice UI."""

    if not bool(getattr(settings, "REFERENCE_CLARIFICATION_ENABLED", False)):
        return False
    rows = attrs.get("_host_amend_candidates")
    if not isinstance(rows, list) or len(rows) < 2:
        return False
    from core import session_manager as sm
    from server.reference_clarification import (
        amend_candidates_from_host_rows,
        clarification_announcement,
        create_reference_selection,
    )
    from server.work_ledger_coordinator import get_work_ledger_coordinator

    coordinator = get_work_ledger_coordinator()
    session_id = sm.get_current_session_id() or ""
    if coordinator is None or not session_id:
        return False
    candidates = amend_candidates_from_host_rows(coordinator, rows)
    if len(candidates) < 2:
        return False
    await create_reference_selection(
        session_id=session_id,
        task_text=task_text,
        attrs=attrs,
        candidates=candidates,
        resume=_resume_reference_selection,
    )
    display, voice_ja = clarification_announcement()
    await _speak_task_lookup_answer(
        display,
        voice_text_ja=voice_ja,
        history_marker="REFERENCE_CLARIFICATION",
        source="reference_clarification",
        log_label="REFERENCE-CLARIFICATION",
    )
    return True


async def _handle_delegate(task_text: str, attrs: dict | None = None) -> str | None:
    """Forward delegate tags through ProviderRuntime.

    ProviderRuntime owns canvas updates and WorkObserver owns terminal narration.
    The legacy second-pass summary is only kept for the direct OpenClaw fallback
    path, where no provider observer session exists.
    """
    attrs = dict(attrs) if isinstance(attrs, dict) else {}
    focus_modifier = _delegate_focus_modifier(attrs)
    if focus_modifier:
        from server.focus_policy import (
            apply_focus_modifier_audit,
            audit_focus_modifier,
        )

        focus_audit = await audit_focus_modifier(attrs)
        apply_focus_modifier_audit(attrs, focus_audit)
        if not focus_audit.allowed:
            logger.warning(
                "[WORK-DESTINATION] persistent focus modifier denied: "
                "requested=%s decision=%s outcome=%s",
                focus_audit.requested,
                focus_audit.decision or "unavailable",
                focus_audit.outcome,
            )
        focus_modifier = _delegate_focus_modifier(attrs)
    # Audit whether the user actually requested a persistent modifier before
    # resolving its entity. Otherwise an invented focus=set on ordinary work
    # can manufacture a selection card even though the modifier is removed a
    # few lines later.
    reference_status, task_text, attrs = await _adjudicate_delegate_reference(
        task_text, attrs
    )
    if reference_status == "deferred":
        return "[focus] awaiting reference selection"
    if reference_status == "blocked":
        return "[focus] reference resolution blocked"
    if reference_status == "handled":
        return "[focus] reference selection applied"
    reference_selection_resumed = (
        attrs.pop("_host_reference_selection_resumed", False) is True
    )
    focus_modifier = _delegate_focus_modifier(attrs)
    if focus_modifier:
        # Apply the persistent destination before resolving the operation so
        # the same tag has deterministic ordering without creating two racing
        # DELEGATE actions. Work intent remains untouched and is what the
        # Provider request and ledger record.
        focus_attrs = dict(attrs)
        if focus_modifier == "set":
            project_id = str(
                focus_attrs.get("project_id") or focus_attrs.get("projectId") or ""
            ).strip()
            if not project_id:
                return "[focus] a project is required when setting the destination"
        else:
            # project_id may still route the current operation; clearing only
            # changes the destination inherited by future turns.
            focus_attrs.pop("project_id", None)
            focus_attrs.pop("projectId", None)
        # A focus modifier belongs to a work operation. The role's work
        # acknowledgement and later WorkObserver narration own that UX; a
        # second standalone "switched" message would be duplicate noise.
        focus_result = await _handle_declared_focus(
            focus_attrs,
            announce_result=False,
        )
        if focus_result.get("ok") is not True:
            return str(focus_result.get("message") or "[focus] switch refused")
        attrs.pop("focus", None)
        attrs["focus_applied"] = True
    if _delegate_declared_report_only(attrs):
        # The model was asked to declare what the user wanted rather than to
        # refrain from acting, because suppression by prose kept losing to the
        # habit of acting (2026-07-31: a status-only question still created work
        # in 20% of tag-path runs and 58% of tool-path runs). A declaration the
        # host can act on turns an unenforceable rule into an enforced one.
        logger.info(
            "[DELEGATE-INTENT] report-only declared; answering from the ledger "
            "instead of starting work: task=%r",
            str(task_text or "")[:120],
        )
        try:
            from server.task_lookup import lookup_enabled
        except Exception:
            return None
        if lookup_enabled():
            # The answering half of the declaration: until task lookup, the
            # line above was aspiration — nothing read the ledger, and the
            # model answered from whatever the roster happened to carry.
            return await _answer_report_from_ledger(task_text, attrs)
        return None
    if _delegate_declared_focus(attrs):
        # Focus modifies where work goes; it does not replace that work. Apply
        # it first so downstream routing observes the new destination. A pure
        # switch stops here, while a legacy compound switch consumes the focus
        # intent and continues as the safe known degradation: a new execute
        # WorkItem. New outputs use the orthogonal focus modifier above, which
        # preserves the actual operation intent.
        focus_result = await _handle_declared_focus(
            attrs,
            announce_result=(
                not str(task_text or "").strip()
                and not reference_selection_resumed
            ),
        )
        if focus_result.get("ok") is not True:
            return str(focus_result.get("message") or "[focus] switch refused")
        if not str(task_text or "").strip():
            return str(focus_result.get("message") or "[focus] destination updated")
        attrs["intent"] = "execute"
        attrs["focus_applied"] = True
    if _delegate_declared_retract(attrs):
        # Before this branch existed the tag fell through to provider routing
        # and "stop that one" started a task instead of ending one.
        return await _handle_declared_retract(task_text)
    ambiguous_amend = str(attrs.get("amend_ambiguous") or "").strip()
    if ambiguous_amend:
        # Resolution found several candidates. Starting anything here means
        # picking one, and picking wrong writes into a worktree that does not
        # hold the file the user meant — silent, unlike one question.
        logger.info("[DELEGATE-AMEND] blocked, asking which task: %s", ambiguous_amend)
        try:
            if await _defer_ambiguous_amend(task_text, attrs):
                return "[amend blocked] awaiting WorkItem selection"
        except Exception:
            logger.exception("failed to publish structured amend selection")
        await _announce_amend_ambiguous(ambiguous_amend)
        return "[amend blocked] several tasks match that file"
    missing_amend = str(attrs.get("amend_missing") or "").strip()
    if missing_amend:
        logger.info("[DELEGATE-AMEND] blocked, tracked target missing: %s", missing_amend)
        await _announce_amend_missing(missing_amend)
        return "[amend blocked] tracked target was not found"
    declared_operation = str(
        attrs.get("action") or attrs.get("browser_action") or ""
    ).strip()
    if declared_operation and not str(task_text or "").strip():
        # A structured operation is already executable, but the Work Ledger
        # and Observer still need a human-readable description.  The exact
        # current utterance is authoritative and avoids asking the model to
        # duplicate the same instruction in a second field.
        task_text = " ".join(
            str(attrs.get("_host_source_user_text") or "").split()
        )
    provider_requirements, provider_selection = _delegate_provider_selection(task_text, attrs)
    provider = provider_selection.provider_id
    from core import session_manager as sm

    task_text, handoff_audit = _rebase_web_goal_for_selected_provider(
        task_text,
        attrs,
        selected_provider=provider,
        requirements=provider_requirements,
        session_manager=sm,
    )
    if handoff_audit:
        attrs["_host_provider_handoff"] = handoff_audit
    from agent_host.provider_runtime import runtime as provider_runtime
    from agent_host.provider_workspace import workspace_route_authority

    # Interaction branches are subordinate to the canonical control decision.
    # Retire/squash a different Provider's branch before its structural fast
    # paths can intercept later turns after the handoff.
    try:
        from core import session_manager as _sm
        from server.interaction_branch import get_interaction_branch_coordinator

        interaction_coordinator = get_interaction_branch_coordinator()
        interaction_session_id = _sm.get_current_session_id() or ""
        if (
            interaction_coordinator is not None
            and interaction_coordinator.close_for_provider_handoff(
                interaction_session_id,
                next_provider=provider,
            )
        ):
            logger.info(
                "interaction branch closed for canonical provider handoff: "
                "session=%s provider=%s",
                interaction_session_id,
                provider,
            )
    except Exception:
        logger.exception("failed to close interaction branch during provider handoff")

    provider_manifest = provider_runtime.get_manifest(provider)
    if provider_manifest is None:
        raise ValueError(f"selected provider is not registered: {provider}")
    action = str(attrs.get("action") or attrs.get("browser_action") or "").strip().lower()
    branch_intent = str(attrs.get("branch") or "").strip().lower()
    workspace_authority = workspace_route_authority(
        provider_manifest.capabilities.workspace_ownership
    )
    workspace_route = _delegate_workspace_route(
        provider,
        attrs,
        manifest=provider_manifest,
    )
    logger.info(
        "[PROVIDER-DISPATCH] turn_id=%s provider=%s intent=%s subject=%s "
        "workspace_ref=%s project_id=%s dispatch_source=%s "
        "route_status=%s route_reason=%s route_source=%s task_chars=%d",
        str(attrs.get("_host_turn_id") or ""),
        provider,
        str(attrs.get("intent") or ""),
        str(attrs.get("subject") or ""),
        str(attrs.get("workspace_ref") or attrs.get("workspaceRef") or ""),
        str(attrs.get("project_id") or attrs.get("projectId") or ""),
        str(attrs.get("_host_dispatch_source") or ""),
        str(workspace_route.get("status") or ""),
        str(workspace_route.get("reason") or ""),
        str(workspace_route.get("source") or ""),
        len(str(task_text or "")),
    )
    if workspace_authority == "host" and workspace_route.get("status") != "resolved":
        logger.warning(
            "%s delegate blocked before execution: reason=%s candidates=%s",
            provider,
            workspace_route.get("reason") or "workspace_unresolved",
            len(workspace_route.get("candidates") or []),
        )
        await _announce_provider_workspace_block(provider, workspace_route)
        return "[workspace routing blocked] project context is required"
    delegate_cwd = workspace_route.get("cwd") or None
    delegate_mode = _delegate_mode_for_provider(
        provider,
        attrs,
        action,
        task_text,
        workspace_access=provider_requirements.workspace_access,
    )

    # ── 单脑路由（2026-07-04）：主 LLM 通过 branch 属性表达分支意图 ──────────
    # continue → 在活跃分支内后台执行（不阻塞对话，observer 叙述结果）；
    # close    → 关闭活跃分支（模型标签前的那句话就是语音应答）；
    # new/缺省 → 走通用 provider 路径，intent 随 metadata 下传给
    #            _should_start_new_branch 显式判定。
    if provider == "browser" and branch_intent in {"continue", "close"}:
        try:
            from core import session_manager as _sm
            from server.interaction_branch import get_interaction_branch_coordinator

            coordinator = get_interaction_branch_coordinator()
            branch_session_id = _sm.get_current_session_id() or ""
            if coordinator is not None:
                if branch_intent == "close":
                    closed = coordinator.close_active_branch(branch_session_id, reason="llm_close")
                    logger.info("llm branch=close handled; closed=%s", closed)
                    return None
                run = await coordinator.continue_from_delegate(
                    session_id=branch_session_id,
                    task=task_text,
                    source_user_text=str(
                        attrs.get("_host_source_user_text") or ""
                    ),
                    turn_id=str(attrs.get("_host_turn_id") or ""),
                )
                if run is not None:
                    return None
                logger.info("branch=continue without active branch; falling through as new run")
        except Exception:
            logger.exception("branch intent handling failed; falling through to provider run")

    from server.delegate_dispatch import (
        DelegateDispatchPlan,
        dispatch_delegate,
    )

    sanitized_task, sanitize_info = _sanitize_delegate_task_for_provider(
        task_text,
        attrs,
        provider=provider,
        session_manager=sm,
    )
    if sanitize_info:
        removed_parameters = _remove_ungrounded_persona_parameters(attrs)
        if removed_parameters:
            sanitize_info["removed_parameters"] = removed_parameters
        logger.warning(
            "delegate task sanitized: provider=%s reason=%s original=%r replacement=%r",
            provider,
            sanitize_info.get("reason") or "unknown",
            str(task_text or "")[:220],
            sanitized_task[:220],
        )
        task_text = sanitized_task
    delegate_mode = _delegate_mode_for_provider(
        provider,
        attrs,
        action,
        task_text,
        workspace_access=provider_requirements.workspace_access,
    )
    browser_parameters: dict = {}
    browser_audit: dict = {}
    if provider == "browser":
        from agent_host.browser_request_contract import (
            normalize_delegate_browser_request,
        )

        browser_normalization = normalize_delegate_browser_request(
            task_text,
            action,
            attrs,
        )
        action = browser_normalization.action
        delegate_mode = _delegate_mode_for_provider(
            provider,
            attrs,
            action,
            task_text,
            workspace_access=provider_requirements.workspace_access,
        )
        browser_parameters = dict(browser_normalization.parameters)
        browser_audit = dict(browser_normalization.audit)
    return await dispatch_delegate(
        DelegateDispatchPlan(
            task_text=str(task_text or ""),
            attrs=dict(attrs),
            provider=provider,
            requirements=provider_requirements,
            selection=provider_selection,
            manifest=provider_manifest,
            workspace_route=dict(workspace_route),
            workspace_authority=workspace_authority,
            delegate_cwd=delegate_cwd,
            delegate_mode=delegate_mode,
            action=action,
            branch_intent=branch_intent,
            sanitize_info=dict(sanitize_info),
            browser_parameters=browser_parameters,
            browser_audit=browser_audit,
        ),
        announce_start_failure=_announce_provider_start_failure,
        route_amendment=_route_active_amendment,
    )

_DELEGATE_PERSONA_MARKERS = (
    "牧瀬紅莉栖",
    "牧濑红莉栖",
    "紅莉栖",
    "红莉栖",
    "まきせ",
    "くりす",
    "クリス",
    "クリスティーナ",
    "kurisu",
    "makise",
    "christina",
    "steins;gate",
    "steins gate",
    "viktor chondria",
)


_BROWSER_EXECUTION_PARAMETER_KEYS = (
    "action",
    "browser_action",
    "branch",
    "url",
    "query",
    "text",
    "label",
    "selector_text",
    "ref",
    "action_ref",
    "target_ref",
    "value",
    "input",
    "submit",
)


def _rebase_web_goal_for_selected_provider(
    task_text: str,
    attrs: dict,
    *,
    selected_provider: str,
    requirements,
    session_manager,
) -> tuple[str, dict]:
    """Transfer an address-less Web goal without carrying Browser guesses.

    Provider selection may correctly lower a model-proposed Browser operation
    to Agent research.  In that case the model's URL/task is precisely the
    evidence that was rejected as capability authority, so it cannot remain
    the execution payload.  The exact source turn is authoritative; an
    interrupted immediately-adjacent turn is bounded conversational context.
    """

    requested = str(attrs.get("provider") or "").strip().lower()
    if requested in {"web", "browser_provider", "playwright"}:
        requested = "browser"
    source_user = " ".join(
        str(attrs.get("_host_source_user_text") or "").split()
    )
    if not (
        requested == "browser"
        and str(selected_provider or "").strip().lower() == "openclaw"
        and str(getattr(requirements, "task_kind", "") or "") == "research"
        and source_user
    ):
        return task_text, {}

    antecedent_user = _user_message_before_current(
        session_manager,
        current_user=source_user,
    )
    interrupted_antecedent = _immediately_preceding_assistant_was_interrupted(
        session_manager,
        current_user=source_user,
    )
    lines = [
        "Complete this external Web goal from the main conversation.",
    ]
    if interrupted_antecedent and antecedent_user:
        lines.append(f"Immediate prior user request (context only): {antecedent_user}")
    lines.append(f"Latest user instruction (authoritative): {source_user}")
    identity_grounded = _user_explicitly_references_assistant_identity(
        source_user
    ) or (
        interrupted_antecedent
        and _user_explicitly_references_assistant_identity(antecedent_user)
    )
    lines.append(
        "Do not inherit an older page or research target when it conflicts "
        "with the latest instruction."
    )

    removed: list[str] = []
    for key in _BROWSER_EXECUTION_PARAMETER_KEYS:
        if key in attrs:
            attrs.pop(key, None)
            removed.append(key)
    return "\n".join(lines), {
        "reason": "browser_goal_lowered_to_agent_research",
        "requested_provider": requested,
        "selected_provider": selected_provider,
        "source_authority": "current_user",
        "interrupted_antecedent_included": bool(
            interrupted_antecedent and antecedent_user
        ),
        "identity_grounded": identity_grounded,
        "removed_browser_parameters": removed,
    }


def _sanitize_delegate_task_for_provider(
    task_text: str,
    attrs: dict,
    *,
    provider: str,
    session_manager,
) -> tuple[str, dict]:
    task = " ".join(str(task_text or "").split())
    source_user = " ".join(
        str(attrs.get("_host_source_user_text") or "").split()
    )
    latest_user = _latest_user_message(session_manager)
    antecedent_user = _user_message_before_current(
        session_manager,
        current_user=source_user,
    )
    authoritative_user = source_user or latest_user
    if not task:
        return authoritative_user, {
            "reason": "empty_task_replaced_with_source_user",
            "source": "current_turn" if source_user else "conversation_history",
        } if authoritative_user else {}
    confirmed_prior_request = _consume_control_payload_grounding(attrs)
    persona_grounded = _delegate_persona_reference_is_grounded(
        source_user,
        antecedent_user,
        provider=provider,
        confirmed_prior_request=confirmed_prior_request,
        interrupted_antecedent=_immediately_preceding_assistant_was_interrupted(
            session_manager,
            current_user=source_user,
        ),
    )
    persona_in_parameters = any(
        _has_persona_marker(str(attrs.get(key) or ""))
        for key in _DELEGATE_PERSONA_PARAMETER_KEYS
    )
    if (
        (_has_persona_marker(task) or persona_in_parameters)
        and authoritative_user
        and not persona_grounded
    ):
        return authoritative_user, {
            "reason": "persona_leak_removed",
            "provider": provider,
            "original_task": task[:500],
            "source_user": source_user[:500],
            "latest_user": latest_user[:500],
            "antecedent_user": antecedent_user[:500],
            "confirmed_prior_request": confirmed_prior_request,
            "replacement_source": (
                "current_turn" if source_user else "conversation_history"
            ),
        }
    return task, {}


def _consume_control_payload_grounding(attrs: dict) -> bool:
    """Consume the typed proof emitted by canonical ControlDecision.

    Private-looking strings in a role-authored tag are not Host authority.  A
    positive result therefore requires the frozen dataclass installed by
    ``reconcile_control_decision``; the evidence is removed before Provider
    routing while the ordinary string ``_host_payload_source`` remains for
    audit metadata.
    """

    from server.control_decision import (
        CONTROL_PAYLOAD_GROUNDING_ATTR,
        ControlPayloadGrounding,
    )

    evidence = attrs.pop(CONTROL_PAYLOAD_GROUNDING_ATTR, None)
    return bool(
        isinstance(evidence, ControlPayloadGrounding)
        and evidence.continuity == "confirmed_prior_request"
    )


def _latest_user_message(session_manager) -> str:
    try:
        dialog = getattr(session_manager.conversation_history, "dialog", [])
        for message in reversed(list(dialog)):
            if message.get("role") == "user":
                return " ".join(str(message.get("content") or "").split())
    except Exception:
        logger.exception("failed to inspect latest user message for delegate sanitization")
    return ""


def _user_message_before_current(session_manager, *, current_user: str) -> str:
    """Return the immediate user antecedent in the real persisted ordering.

    During Provider dispatch the current user message is already in history.
    Older tests treated the latest history entry as the antecedent, which made
    one-turn retry authorization pass in isolation but fail in production.
    If a caller supplies a history snapshot from before the current turn, the
    latest user entry is already the correct antecedent.
    """

    current = " ".join(str(current_user or "").split())
    try:
        dialog = getattr(session_manager.conversation_history, "dialog", [])
        users = [
            " ".join(str(message.get("content") or "").split())
            for message in reversed(list(dialog))
            if message.get("role") == "user"
        ]
        if current and users and users[0] == current:
            users = users[1:]
        return users[0] if users else ""
    except Exception:
        logger.exception("failed to inspect prior user message for delegate sanitization")
    return ""


def _immediately_preceding_assistant_was_interrupted(
    session_manager,
    *,
    current_user: str,
) -> bool:
    """Use the persisted barge-in boundary, not language-specific corrections."""

    current = " ".join(str(current_user or "").split())
    try:
        dialog = list(
            getattr(session_manager.conversation_history, "dialog", [])
        )
        preceding_index = len(dialog) - 1
        if current:
            for index in range(len(dialog) - 1, -1, -1):
                message = dialog[index]
                if message.get("role") != "user":
                    continue
                content = " ".join(str(message.get("content") or "").split())
                if content == current:
                    preceding_index = index - 1
                    break
        if preceding_index < 0:
            return False
        preceding = dialog[preceding_index]
        return bool(
            preceding.get("role") == "assistant"
            and "[interrupted by user]"
            in str(preceding.get("content") or "").lower()
        )
    except Exception:
        logger.exception("failed to inspect interrupted conversation boundary")
    return False


def _has_persona_marker(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _DELEGATE_PERSONA_MARKERS)


_ASSISTANT_IDENTITY_REFERENCE_MARKERS = (
    "你自己",
    "妳自己",
    "您的身份",
    "你的身份",
    "你的页面",
    "你的頁面",
    "あなた自身",
    "君自身",
    "自分のページ",
    "yourself",
    "your own",
    "about you",
)


def _user_explicitly_references_assistant_identity(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return _has_persona_marker(normalized) or any(
        marker in normalized for marker in _ASSISTANT_IDENTITY_REFERENCE_MARKERS
    )


_PRIOR_REQUEST_RETRY_MARKERS = (
    "再试",
    "重试",
    "再来一次",
    "继续刚才",
    "继续上次",
    "もう一度",
    "再試行",
    "さっきの",
    "try again",
    "retry",
    "one more time",
    "continue the previous",
)


def _delegate_persona_reference_is_grounded(
    source_user: str,
    antecedent_user: str,
    *,
    provider: str = "",
    confirmed_prior_request: bool = False,
    interrupted_antecedent: bool = False,
) -> bool:
    """Accept identity from current authority or one bounded continuation."""

    if _user_explicitly_references_assistant_identity(source_user):
        return True
    if not source_user:
        return _user_explicitly_references_assistant_identity(antecedent_user)
    if confirmed_prior_request:
        return _user_explicitly_references_assistant_identity(antecedent_user)
    if interrupted_antecedent:
        return _user_explicitly_references_assistant_identity(antecedent_user)
    normalized_source = " ".join(str(source_user or "").lower().split())
    retry = any(marker in normalized_source for marker in _PRIOR_REQUEST_RETRY_MARKERS)
    provider_retarget = bool(
        str(provider or "").strip()
        and str(provider or "").strip().lower() in normalized_source
    )
    return bool(
        (retry or provider_retarget)
        and _user_explicitly_references_assistant_identity(antecedent_user)
    )


_DELEGATE_PERSONA_PARAMETER_KEYS = (
    "url",
    "query",
    "text",
    "label",
    "selector_text",
    "value",
    "input",
)


def _remove_ungrounded_persona_parameters(attrs: dict) -> list[str]:
    """Keep a persona rewrite atomic across task and action arguments."""

    removed: list[str] = []
    for key in _DELEGATE_PERSONA_PARAMETER_KEYS:
        value = attrs.get(key)
        if value not in (None, "") and _has_persona_marker(str(value)):
            attrs.pop(key, None)
            removed.append(key)
    return removed


def _noop_warmup() -> str:
    return ""


# entry.

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Amadeus backend server")
    parser.add_argument("--port", type=int, default=17777)
    args = parser.parse_args()
    asyncio.run(bootstrap(port=args.port))
