"""
Backend service entry point - initializes all subsystems and starts the
WebSocket server. Mirrors the initialization in main.py's main() but
without any GUI (PyQt5 / Tkinter) dependencies.

Usage:
    python -m server.app              # default port 17777
    python -m server.app --port 9077  # custom port
"""

from llm.character_voice_lines import voice_line

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import mimetypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from typing import Any, TYPE_CHECKING

# Intel OpenMP, which torch uses for CPU work, keeps each worker thread spinning
# for 200 ms after a parallel region. Silero VAD runs every 32 ms on the wake
# and listening threads, so one worker never slept and the idle backend used a
# whole CPU core. 1 ms still keeps workers warm across back-to-back CPU
# inference. This must run before anything imports torch.
os.environ.setdefault("KMP_BLOCKTIME", "1")

if TYPE_CHECKING:
    pass

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


def _source_session_role_identity(session_id: str) -> dict[str, str]:
    """Resolve first-acceptance identity from the persisted source conversation."""
    from core import session_manager as sm
    from llm.character_prompts import character_identity

    return character_identity(sm.require_session_character(session_id))


def _append_work_observer_to_history(decision: dict) -> None:
    """Keep allowed Work narration in the conversation whose role presented it."""
    entry = str(decision.get("main_chat_entry") or "").strip()
    if not entry:
        return
    try:
        from core import session_manager as sm

        current_session_id = str(sm.get_current_session_id() or "")
        source_session_id = str(decision.get("session_id") or "")
        if not current_session_id or (source_session_id and source_session_id != current_session_id):
            return
        sm.require_session_character(current_session_id)
        sm.conversation_history.add_assistant(f"[WORK_OBSERVER]\n{entry}")
        sm.save_session(current_session_id, enable_conversation=True)
    except Exception:
        logger.exception("failed to append work observer decision to history")


# force the project root onto sys.path.
ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.log_encoding import install_mojibake_repair_filter, install_stdio_mojibake_repair
from server.local_auth import LocalAuthPolicy, clear_inherited_auth_environment
from config import settings
from config.log_privacy import protected_text

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
# (CUDA DLL ordering). It is a T2b (local-cu124) dependency: L1 installs skip
# the preload. Supported absence (ModuleNotFoundError naming onnxruntime
# itself) is skipped; a present-but-broken install surfaces instead of
# masquerading as absence.
try:
    import onnxruntime  # noqa: F401  # eager before torchmetrics chain
except ModuleNotFoundError as exc:
    if exc.name != "onnxruntime":
        raise

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

    from llm.character_prompts import active_character

    active_character()  # Validate the pinned startup pack before runtime composition.

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
    import llm.client as _llm_client_mod


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
    from server.chat_role_delivery import ChatRoleDelivery

    chat_role_delivery = ChatRoleDelivery()
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
        provider_input=provider_runtime.append_input,
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
    auip_launch.before_result_entry = auip_h.prepare_result_entry
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
    bus.on(Method.WORK_INPUT_UPDATED, auip_launch_callback)
    work_preview_auip_callback = work_preview.on_auip_updated
    bus.on(Method.AUIP_UPDATED, work_preview_auip_callback)
    auip_result_entry_callback = auip_launch.on_app_updated
    bus.on(Method.AUIP_UPDATED, auip_result_entry_callback)
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
        provider_cancel=provider_runtime.cancel,
        root=Path(str(os.environ.get("AMADEUS_INTERACTION_BRANCH_ROOT")
            or Path(ROOT) / "runtime" / "interaction_branches")),
        display_language=_observer_display_language,
    )

    async def _validate_provider_start_admission(
        request,
        run_id: str,
        phase: str,
    ) -> dict[str, Any]:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        scope_present = "interaction_branch_routing_scope" in metadata
        scope = metadata.get("interaction_branch_routing_scope")
        session_id = str(metadata.get("session_id") or "")
        if phase == "release" and not isinstance(scope, dict):
            return {"accepted": True, "reason": "no_reservation_to_release"}
        if not scope_present:
            accepted = not bool(
                interaction_branch.termination_pending_for_session(session_id)
                or interaction_branch.provider_admission_for_session(session_id)
            )
            return {
                "accepted": accepted,
                "reason": (
                    "no_browser_routing_scope"
                    if accepted
                    else "browser_transition_authority_unavailable"
                ),
            }
        if not isinstance(scope, dict):
            return {"accepted": False, "reason": "invalid_browser_routing_scope"}
        reservation_id = str(
            metadata.get("interaction_branch_admission_id") or ""
        )
        accepted, reason = await interaction_branch.provider_start_admission(
            scope,
            session_id=session_id,
            provider=str(request.provider or ""),
            reservation_id=reservation_id,
            run_id=run_id,
            phase=phase,
        )
        return {
            "accepted": accepted,
            "reason": reason,
        }

    provider_runtime.set_start_admission_validator(
        _validate_provider_start_admission
    )
    vn_h = VNPlayerHandler()
    vn_launch_h = VNLaunchHandler()

    handlers = (chat_h, session_h, tts_h, asr_h, wake_h, vts_h, expr_h, sys_h,
        render_h, wallpaper_h, provider_h, capability_h, mcp_connection_h,
        provider_activity_h, work_h, work_preview_h, attention_h, auip_h, vn_h,
        vn_launch_h)
    handlers += (chat_role_delivery,)
    for h in handlers:
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
        return {
            "status": "ok" if backend_ready else "starting",
            **auth_policy.health_fields(),
            "vts_connected": vts_manager.connected if vts_manager else False,
            "control_decision_mode": "retired",
            "cooperative_chat_mode": "authority",
            "cooperative_permission_policy": settings.COOPERATIVE_CHAT_PERMISSION_POLICY,
        }

    @app.get("/runtime/status")
    async def runtime_status(request: Request):
        if not _http_request_authenticated(request.headers, auth_policy):
            raise HTTPException(status_code=401, detail="Authentication required")
        if not _http_request_origin_allowed(request.headers, backend_port=port):
            raise HTTPException(status_code=403, detail="Untrusted request origin")
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

    @app.post("/wallpaper/stop")
    async def stop_wallpaper_host(request: Request):
        if not _http_request_authenticated(request.headers, auth_policy):
            raise HTTPException(status_code=401, detail="Authentication required")
        if not _http_request_origin_allowed(request.headers, backend_port=port):
            raise HTTPException(status_code=403, detail="Untrusted request origin")
        # Electron owns the native host process. Its loss terminates the same
        # backend lifecycle as a user disabling wallpaper, including wake/ASR.
        return await wallpaper_h.handle(Method.WALLPAPER_STOP, {})

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

    def _update_playback_subtitle(japanese_text: str, chinese_text: str = "") -> None:
        from server.vn_tts_bridge import update_playback_subtitle

        update_playback_subtitle(
            playback_manager.current_playing_id, japanese_text, chinese_text,
            update=_update_wallpaper_subtitle,
        )

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
                from server.vn_tts_bridge import display_vn_subtitle, is_vn_sentence

                if is_vn_sentence(sentence_id):
                    asyncio.create_task(display_vn_subtitle(
                        sentence_id, japanese_text,
                        display=_server_display_chinese_subtitle_with_text,
                        is_current=playback_manager.is_current_playback_sentence,
                    ))
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
            update_subtitle_display=_update_playback_subtitle,
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
        from server.vn_tts_bridge import schedule_overlay_playback

        schedule_overlay_playback(sentence_id, True, server_loop)
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
        from server.vn_tts_bridge import schedule_overlay_playback

        schedule_overlay_playback(sentence_id, False, server_loop)
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
    from tts.pipeline import MAX_SELECTABLE_TTS_CONCURRENCY, selectable_tts_concurrency

    # The semaphore selects ×1/×2 and Settings switches it at runtime, so the
    # worker pool must already fit the most parallel selectable mode.
    tts_concurrency = selectable_tts_concurrency(EXP_TTS_MAX_CONCURRENCY)
    if tts_concurrency != EXP_TTS_MAX_CONCURRENCY:
        logger.warning(
            "EXP_TTS_MAX_CONCURRENCY=%s is outside the selectable TTS modes; using %s",
            EXP_TTS_MAX_CONCURRENCY,
            tts_concurrency,
        )
    tts_executor = ThreadPoolExecutor(max_workers=MAX_SELECTABLE_TTS_CONCURRENCY)
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
    exp_tts_semaphore = asyncio.Semaphore(tts_concurrency)
    exp_play_condition = asyncio.Condition()

    # OpenClaw gateway.
    openclaw_gateway_start_task = None
    try:
        from openclaw.gateway import start_openclaw_gateway as _start_oc
        openclaw_gateway_start_task = asyncio.create_task(_start_oc())
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

    async def _start_asr_from_wake(payload=None, *, continuous: bool | None = None):
        payload = payload or {}
        if continuous is None and sys.platform == "win32" and wallpaper_h.is_running():
            continuous = True
        qwen_hot_window = max(float(WAKE_AWAKE_SECONDS), float(ASR_IDLE_UNLOAD_SECONDS))
        if not await _main_voice_allowed_now("wake detected"):
            return {"status": "error", "error": "voice_unavailable_during_vn"}
        logger.info("wake detected; ASR conversation mode=%s hot_window_seconds=%.1f",
                    "continuous" if continuous else "timed", qwen_hot_window)
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
        return await asr_h.start_listening(
            {
                **({"continuous": continuous} if continuous is not None else {}),
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
        from core.turn_coordinator import TurnAuthorityError

        try:
            from server.speculative_turn import get_speculative_launcher

            if await get_speculative_launcher().resolve(text):
                logger.info("%s text matched speculative turn; confirmed, skip resend", source)
                return
        except TurnAuthorityError:
            raise
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
        result = await vn_h.handle_asr(payload)
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
            marker = "[interrupted by user]"
            turn_id = str(payload.get("turn_id") or "")
            sid = str(payload.get("session_id") or "")
            interrupted_prefix = completed_text or accumulated_text
            interrupted_text = (
                f"{interrupted_prefix} {marker}".strip()
                if interrupted_prefix else marker
            )
            changed = sm.persist_interrupted_assistant_turn(
                sid,
                turn_id=turn_id,
                heard_content=completed_text or accumulated_text,
                marker=marker,
            )
            logger.info(
                "emitting chat.interrupted turn=%s text_len=%s completed_len=%s subscribers=%s source=%s",
                payload.get("turn_id") or "",
                len(interrupted_text),
                len(interrupted_prefix),
                bus.subscriber_count(Method.CHAT_INTERRUPTED),
                payload.get("source") or "",
            )
            from server.handlers.session_handler import _display_interruption

            visible_interruption = _display_interruption(
                interrupted_text, interrupted_prefix, marker)
            await bus.emit(
                Method.CHAT_INTERRUPTED,
                {
                    **visible_interruption,
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
        await vn_h.asr_stopped(payload)
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
    _llm_client_mod.configure(llm_provider=LLM_PROVIDER)
    from core.chat_runtime import get_chat_runtime

    chat_runtime = get_chat_runtime()
    chat_runtime.configure(
        playback_manager=playback_manager,
        pending_sentence_items=pending_sentence_items,
    )
    pre_translation_runtime.configure(
        False if e2e_no_tts else _pre_translation_enabled()
    )

    from server.control_ledger import ControlLedgerStore
    from server.cooperative_chat_ingress import CooperativeChatManager
    from server.cooperative_delivery import CooperativeHostDelivery
    from server.scratch_workspace import create_scratch_workspace
    from llm.prompts import get_system_prompt

    provider_id = str(settings.COOPERATIVE_CHAT_PROVIDER or "").strip().lower()
    if not provider_id:
        raise RuntimeError("cooperative Chat Provider configuration is empty")
    if provider_runtime.get_manifest(provider_id) is None:
        logger.warning(
            "cooperative Chat execution Provider is unavailable at startup: %s; "
            "role-only turns remain available and execution requests will reject",
            provider_id,
        )
    try:
        requirements_payload = json.loads(settings.COOPERATIVE_CHAT_REQUIREMENTS_JSON)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid cooperative Chat Provider requirements JSON") from exc
    if not isinstance(requirements_payload, dict):
        raise RuntimeError("cooperative Chat Provider requirements must be an object")
    try:
        additional_requirements_payload = json.loads(
            settings.COOPERATIVE_CHAT_ADDITIONAL_REQUIREMENTS_JSON
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "invalid additional cooperative Provider requirements JSON"
        ) from exc
    if not isinstance(additional_requirements_payload, dict):
        raise RuntimeError(
            "additional cooperative Provider requirements must be an object"
        )
    from agent_host.provider_roles import work_provider_roles, work_context_requirements

    context_requirements = work_context_requirements(provider_runtime,
        roles=work_provider_roles(), primary_policy=requirements_payload,
        additional_policies=additional_requirements_payload)
    cooperative_ledger = ControlLedgerStore(Path(work_ledger_store.db_path))
    cooperative_deliveries = {}

    async def _query_cooperative_chat(messages, *, visual_context=None, on_text=None,
                                      json_output=None):
        from server.cooperative_delivery import query_role_messages
        from llm.prompts import (
            finalize_system_prompt_language,
            wrap_user_message_for_language_lock,
        )

        role_messages = [dict(message) for message in messages]
        role_messages[0]["content"] = finalize_system_prompt_language(
            role_messages[0]["content"])
        # The same query port also serves typed-reference and other generic
        # JSON decisions. Only the loop's explicit sources select role mode.
        try:
            frame = json.loads(role_messages[-1]["content"])
        except (TypeError, ValueError):
            frame = None
        source_kind = frame.get("source_kind") if isinstance(frame, dict) else None
        if source_kind == "user":
            role_messages[-1]["content"] = wrap_user_message_for_language_lock(
                role_messages[-1]["content"])

        return await query_role_messages(_llm_client_mod.remote_llm_messages_query,
            role_messages,
            json_output=(source_kind not in ("provider", "host_receipt")
                if json_output is None else json_output),
            on_text=on_text, visual_context=visual_context, temperature=0.0,
            max_tokens=max(1, int(settings.COOPERATIVE_CHAT_QUERY_MAX_TOKENS)),
            timeout=max(1.0, float(settings.COOPERATIVE_CHAT_QUERY_TIMEOUT_S)))

    def _cooperative_publisher(session_id):
        from core import session_manager as cooperative_sessions

        delivery = CooperativeHostDelivery(session_id=session_id,
            display=chat_role_delivery.publish, narration_sink=_speak_cooperative_chat,
            role_stream_factory=lambda cause, gui_callback=None,
                auip_background_capture_release=None:
                chat_runtime.begin_role_text_stream(
                    turn_id=cause, speech=not e2e_no_tts,
                    gui_callback=gui_callback,
                    auip_background_capture_release=(
                        auip_background_capture_release)),
            partial_display=chat_role_delivery.publish_partial,
            allows=chat_role_delivery.allows,
            record_display=cooperative_sessions.append_session_message,
            finish_execution=work_observer.finish_external_presentation,
            begin_execution_result=work_observer.begin_external_result)
        cooperative_deliveries[session_id] = delivery
        return delivery

    def _select_cooperative_role_provider(provider):
        selected = str(provider or "").strip()
        if selected:
            _llm_client_mod.configure(llm_provider=selected)

    from core.character_rag import role_character_reference
    from llm.hybrid_stream import hybrid_local_head

    cooperative_chat = CooperativeChatManager(chat_h, ledger=cooperative_ledger,
        fence_scope="cooperative:foreground", provider=provider_id,
        runtime=provider_runtime, context_requirements=context_requirements,
        allocate=lambda label, context_id:create_scratch_workspace(
            label, unique_id=context_id), query=_query_cooperative_chat,
        persona=lambda: get_system_prompt("base"), publish_factory=_cooperative_publisher,
        role_provider_selector=_select_cooperative_role_provider,
        role_reference=role_character_reference,
        hybrid_head=hybrid_local_head,
        permission_policy=settings.COOPERATIVE_CHAT_PERMISSION_POLICY,
        permission_store=work_ledger_store,
        destination=work_ledger.destination)
    canvas_action_router.configure_cooperative_permission_action(
        cooperative_chat.resolve_permission
    )

    from server.work_control import WorkControl
    from server.work_effect_executor import WorkEffectExecutor

    cooperative_work_control = WorkControl(cooperative_ledger,
        work_ledger_store,
        source_role_identity_resolver=_source_session_role_identity,
        cooperative_context_resolver=cooperative_chat.resolve_work_recipient)
    work_ledger.configure_work_control(cooperative_work_control)
    cooperative_chat.configure_work(cooperative_work_control,
        WorkEffectExecutor(cooperative_work_control, provider_runtime, work_ledger),
        input_request=work_h.submit_input,
        report_request=_answer_report_from_ledger,
        focus_request=lambda attrs, *, session_id: _handle_declared_focus(
            attrs, announce_result=False, session_id=session_id))
    if settings.COOPERATIVE_WORK_PLANNER_ENABLED:
        from server.work_planner import RuntimeWorkPlanner

        async def _query_cooperative_work(messages):
            return await asyncio.to_thread(
                _llm_client_mod.remote_llm_messages_query, messages,
                json_output=True, temperature=0.0,
                model=settings.COOPERATIVE_WORK_PLANNER_MODEL or None,
                max_tokens=max(1, int(settings.CONTROL_DECISION_MAX_TOKENS)),
                timeout=max(1.0, float(settings.CONTROL_DECISION_TIMEOUT_S)))

        cooperative_chat.work_planner = RuntimeWorkPlanner(
            coordinator=work_ledger, query=_query_cooperative_work,
            provider=provider_id,
            project_limit=settings.CONTROL_DECISION_PROJECT_LIMIT,
            work_item_limit=settings.CONTROL_DECISION_WORK_ITEM_LIMIT,
            candidate_limit=settings.CONTROL_DECISION_EXHAUSTIVE_CANDIDATE_LIMIT)
    cooperative_chat.configure_browser(interaction_branch)

    def _prepare_provider_request(request, run_id, intake_authority=None):
        authority_kind = str(getattr(intake_authority, "kind", "") or "")
        if (authority_kind == "cooperative_provider_effect"
                or (not authority_kind and str(request.metadata.get(
                    "cooperative_context_id") or "").strip())):
            return cooperative_chat.prepare_runtime_request(
                request, run_id, intake_authority,
            )
        return work_ledger.prepare_request(request, run_id, intake_authority)

    provider_runtime.set_request_preparer(_prepare_provider_request)
    provider_runtime.set_native_session_checkpoint(
        cooperative_chat.checkpoint_native_session
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

    async def _speak_cooperative_chat(payload: dict) -> dict:
        """Reuse Main Chat's existing first-sentence and tail scheduling path."""

        voice_text = str(
            payload.get("voice_text_ja")
            or payload.get("voice_text")
            or payload.get("tts_text")
            or payload.get("text")
            or ""
        ).strip()
        if not voice_text:
            return {"status": "skipped", "reason": "empty_text"}
        if e2e_no_tts:
            return {
                "status": "skipped",
                "reason": "e2e_no_tts",
                "captured_text": voice_text,
            }
        try:
            return await chat_runtime.enqueue_completed_role_text(
                voice_text,
                turn_id=str(payload.get("turn_id") or payload.get("line_id") or ""),
            )
        except Exception:
            logger.exception("cooperative Main Chat TTS enqueue failed")
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
        if payload.get("vn_speech_segment") is not None:
            identity += f"-segment-{int(payload['vn_speech_segment'])}"
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


    async def _query_auip_control(messages: list[dict[str, str]]) -> str:
        return await _query_structured_control(messages, max_tokens=300)

    def _session_active_provider_work_attempt_ids(session_id: str) -> tuple[str, ...]:
        """Freeze host identity while exposing only liveness to the model."""

        roster = work_ledger.conversation_work_items_for_resolution(
            session_id,
            limit=200,
        )
        if not isinstance(roster, dict) or roster.get("complete") is not True:
            logger.warning(
                "[AUIP-CONTROL] active Work scope unavailable because the "
                "conversation roster is incomplete session_id=%s",
                session_id,
            )
            return ()
        items = roster.get("items")
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
                role_chooser=choose_b2_role_action,
                open_role_chooser=(
                    choose_b2_open_role_action
                    if settings.AUIP_B2_OPEN_PAYLOAD_MODE == "candidate"
                    else None
                ),
                open_payload_mode=settings.AUIP_B2_OPEN_PAYLOAD_MODE,
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

    if auip_control_decider is not None:
        async def _route_cooperative_auip_control(attrs, *, session_id,
                                                   user_text, turn_id, prepare_work=None):
            result = await auip_h.route_control(attrs,
                session_id=session_id, user_text=user_text,
                turn_id=turn_id, prepare_work=prepare_work)
            if (str(attrs.get("action") or "") == "step"
                    and isinstance(result, dict) and result.get("ok") is True):
                app_session_id = str(attrs.get("_host_app_session_id") or "")
                await auip_engagement.wait_for_idle(app_session_id)
                projection = auip_h.runtime.get(app_session_id)
                pending = projection.get("pending_action")
                verified = projection.get("latest_verified_self_action")
                if not isinstance(pending, dict) and not (
                        isinstance(verified, dict)
                        and verified.get("accepted") is True):
                    return {"ok":False,
                        "error":str(projection.get("operator_error")
                            or "auip_step_not_admitted")}
            return result

        cooperative_chat.configure_auip(
            auip_control_decider, _route_cooperative_auip_control,
            cancel_deferred=auip_launch.cancel_deferred,
            step_request=auip_b2.execute_user_decision if auip_b2 is not None else None,
            entry_context=lambda session:auip_launch.render_prompt_context(
                session, language="ja", include_control_contract=False))
    cooperative_chat.install(pending_sentence_items=pending_sentence_items,
        on_turn_finished=_handle_wake_chat_finished,
        presentation_interrupt=_interrupt_presentation_before_chat,
        background_interaction_interrupt=_interrupt_background_interaction_before_chat)
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
        cooperative_chat_getter=lambda: cooperative_chat,
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
        # _handle_asr_listening_stopped restores wake standby under the same flag.
        wake_resumable_fn=lambda: bool(WAKE_ENABLED),
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
    from server.character_presentation import coordinator as character_presentation

    from server.wallpaper_chat import wallpaper_chat_control

    wallpaper_h.configure(
        project_root=Path(ROOT),
        render_bridge=_render_signal_bridge,
        wake_start_fn=lambda: wake_h.start({}),
        wake_stop_fn=lambda: wake_h.stop({}),
        canvas_action_fn=canvas_action_router.route,
        chat_send_fn=lambda text, session_id, visual: chat_h.send_text(
            text, provider=_current_llm_provider(), visual=visual,
            session_id=session_id, source="wallpaper_keyboard",
        ),
        chat_control_fn=(lambda action: wallpaper_chat_control(
            action, session=session_h, asr=asr_h, system=sys_h, wake=wake_h,
            voice_start=lambda: _start_asr_from_wake(continuous=True),
        )) if sys.platform == "win32" else None,
        ensure_chat_session_fn=lambda: session_h.ensure_current_session(
            source="wallpaper_keyboard"
        ),
        canvas_projector=work_ledger.project_canvas,
        current_activity=character_presentation.current_activity,
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
    # process stopped or while the voice lane was wedged. First resume any
    # Host terminal pipeline that stopped before creating that durable note;
    # both operations run only after the Observer has subscribed.
    await work_ledger.recover_pending_terminal_results()
    await work_ledger.replay_pending_terminal_notices()
    from server.vn_tts_bridge import finish_vn_speech

    vn_h.configure(
        project_root=Path(ROOT), event_emit=bus.emit, speak_callback=_deliver_vn_narration,
        speech_epoch=_tts_pipeline.current_tts_epoch,
        speech_finished=finish_vn_speech,
        asr_control=asr_h.handle, asr_state=lambda: asr_h.listening_state(include_context=True),
        capture_game_view=lambda: vn_launch_h.handle(Method.VN_LAUNCH_CAPTURE, {}),
    )
    vn_launch_h.configure(
        project_root=Path(ROOT),
        runtime_start=lambda params: vn_h.handle(Method.VN_START, params),
        runtime_stop=lambda params: vn_h.handle(Method.VN_STOP, params),
        runtime_status=lambda: vn_h.handle(Method.VN_STATUS, {}),
        runtime_line=vn_h.submit_source_line,
        before_external_launch=_prepare_for_external_vn_launch,
        runtime_overlay=vn_h.set_overlay_url,
        backend_url=f"ws://127.0.0.1:{port}/ws",
        auth_policy=auth_policy,
    )

    # Start only after dependency configuration, inside the owning teardown scope.
    # Cancelling executor Futures cannot stop their threads: signal and join them.
    vts_worker_stop = threading.Event()
    vts_workers: list[asyncio.Future] = []
    try:
        vts_workers.append(loop.run_in_executor(None, _vts_action_mod.action_worker, vts_worker_stop))
        if vts_manager.connected and VTS_HEARTBEAT_ENABLED:
            vts_workers.append(loop.run_in_executor(None, _vts_action_mod.heartbeat_worker, vts_worker_stop))
        backend_ready = True
        logger.info(f"backend server ready on ws://127.0.0.1:{port}/ws")
        await server_task  # wait for server to finish
    finally:
        work_status_narrator = None

        async def _close_runtime() -> None:
            vts_worker_stop.set()
            try:
                await chat_h.close()
            except Exception:
                logger.exception("Chat shutdown failed; continuing shared-resource cleanup")
            try:
                await cooperative_chat.begin_close()
            except Exception:
                logger.exception(
                    "cooperative Chat stop preparation failed; continuing shared-resource cleanup"
                )
            await asyncio.gather(*vts_workers)
            bus.off(Method.WORK_UPDATED, auip_launch_callback)
            bus.off(Method.WORK_INPUT_UPDATED, auip_launch_callback)
            bus.off(Method.AUIP_UPDATED, work_preview_auip_callback)
            bus.off(Method.AUIP_UPDATED, auip_result_entry_callback)
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
            provider_runtime.set_native_session_checkpoint(None)
            provider_runtime.set_start_admission_validator(None)
            try:
                await provider_runtime.close()
            finally:
                try:
                    await cooperative_chat.finish_close()
                except Exception:
                    logger.exception(
                        "cooperative Chat observation drain failed after Provider close"
                    )
                if cooperative_ledger is not None:
                    cooperative_ledger.close()
            if openclaw_gateway_start_task is not None:
                if not openclaw_gateway_start_task.done():
                    openclaw_gateway_start_task.cancel()
                await asyncio.gather(openclaw_gateway_start_task, return_exceptions=True)
                from openclaw.gateway import close_openclaw_gateway

                await close_openclaw_gateway()
            await work_h.drain_inputs()
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

        # Retain the whole dependency-ordered teardown, not only its first close.
        # Repeated cancellation of the bootstrap caller cannot skip later owners.
        cleanup = asyncio.create_task(_close_runtime())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError


# adapter: drives core.chat_runtime directly (no main.py attribute injection).


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
    wait_for_idle: bool = True,
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

    floor_available = await _wait_for_output_idle() if wait_for_idle else False
    if wait_for_idle and not floor_available:
        logger.warning(
            "[%s] publishing text-only ledger answer after %.0fs busy floor",
            log_label,
            _ANSWER_IDLE_TIMEOUT_S,
        )
    speech_status = "text_only_busy" if wait_for_idle else "text_only_nonblocking"
    delivery_session_active = bool(
        not target_session_id
        or session_manager_module is None
        or str(session_manager_module.get_current_session_id() or "").strip()
        == target_session_id
    )
    if not delivery_session_active:
        speech_status = "suppressed_session_changed"
    elif floor_available and host_readonly_voice_sink is not None:
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
    session_id: str,
) -> None:
    """Narrate the host-confirmed focus result after the role turn yields."""

    async def publish() -> None:
        await _speak_task_lookup_answer(
            display_text,
            voice_text_ja=voice_text_ja,
            history_marker="FOCUS_RESULT",
            source="session_focus_result",
            log_label="FOCUS-RESULT",
            session_id=session_id,
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
    wait_for_idle: bool = True,
    publish=None,
) -> bool:
    """Resolve facts in the Host, then let the existing Narrator say them."""

    from server import task_lookup

    async def deliver(answer, *, voice_text_ja, source, log_label, delivery_observer=None):
        if publish is None:
            return await _speak_task_lookup_answer(
                answer, voice_text_ja=voice_text_ja, history_marker="TASK_STATUS",
                source=source, log_label=log_label, session_id=session_id,
                wait_for_idle=wait_for_idle, delivery_observer=delivery_observer)
        # A foreground report already owns this Chat turn. Its existing role
        # publisher owns display/history/TTS; waiting for that turn to go idle
        # here would wait for ourselves. The Narrator still composes only once.
        accepted = publish(answer)
        if hasattr(accepted, "__await__"):
            accepted = await accepted
        if delivery_observer is not None:
            delivery_observer({"session_id":session_id,
                "speech_status":"foreground_published" if accepted is True else "suppressed",
                "speak":False})
        return accepted is True

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

                return await deliver(
                    answer,
                    voice_text_ja=direct_voice,
                    source="work_status_narrator",
                    log_label="TASK-LOOKUP",
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
    return await deliver(
        answer,
        voice_text_ja=voice_text_ja,
        source="work_ledger_status_fallback",
        log_label="TASK-LOOKUP-FALLBACK",
    )


async def _answer_report_from_ledger(task_text: str, attrs: dict, *, publish=None) -> str:
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
        if publish is not None:
            published = publish(answer.display_text)
            if hasattr(published, "__await__"):
                published = await published
            published = published is True
        else:
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
    report_wait_for_idle = attrs.get("_host_nonblocking_report") is not True
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
        if await _answer_work_item_status(row, session_id=session_id,
                wait_for_idle=report_wait_for_idle,
                **({"publish":publish} if publish is not None else {})):
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
        if await _answer_work_item_status(row, session_id=session_id,
                wait_for_idle=report_wait_for_idle,
                **({"publish":publish} if publish is not None else {})):
            return "[report] answered from the ledger"
        return "[report] answer pass unavailable"
    reason = str(resolution.get("reason") or "")
    candidates = [
        candidate
        for candidate in (resolution.get("candidates") or [])
        if isinstance(candidate, dict)
    ]
    if reason == "ambiguous" and candidates:
        from server.reference_catalog import candidate_task_label

        titles = ", ".join(
            label
            for label in (candidate_task_label(candidate) for candidate in candidates[:4])
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
        from server.reference_catalog import explicit_file_references
        from server.work_ledger_coordinator import get_work_ledger_coordinator

        coordinator = get_work_ledger_coordinator()
        if coordinator is None:
            return []
        for reference in explicit_file_references(str(question or "")):
            found = coordinator.drafts_in_other_conversations(reference)
            if found:
                return found
    except Exception:
        # Wording help only: a failure here must not change the answer path.
        logger.debug("draft lookup outside the conversation failed", exc_info=True)
    return []


async def _handle_declared_focus(
    attrs: dict,
    *,
    announce_result: bool = True,
    session_id: str = "",
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
    current_session_id = sm.get_current_session_id() or ""
    frozen_session_id = str(session_id or "").strip()
    if frozen_session_id and current_session_id != frozen_session_id:
        return {
            "ok": False,
            "authority_blocked": True,
            "message": "[focus] originating Session is no longer active",
        }
    session_id = frozen_session_id or current_session_id
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
            voice_ja = voice_line("focus_voice_drafts")
            if _observer_display_language() == "japanese":
                display = voice_ja
            _schedule_focus_confirmation(
                display,
                voice_ja,
                status="cleared",
                session_id=session_id,
            )
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
            voice_ja = voice_line("focus_voice_failed")
            if _observer_display_language() == "japanese":
                display = voice_ja
            _schedule_focus_confirmation(
                display,
                voice_ja,
                status="rejected",
                session_id=session_id,
            )
        return {"ok": False, "message": f"[focus] {message}"}
    await coordinator.publish_snapshot(reason="session_project.changed")
    if announce_result:
        project_name = str(chosen.get("projectName") or "").strip() or "项目"
        display = f"已经确认切换到“{project_name}”项目，接下来的项目工作会从这里继续。"
        voice_ja = voice_line("focus_voice_project", project_name=project_name)
        if _observer_display_language() == "japanese":
            display = voice_ja
        _schedule_focus_confirmation(
            display,
            voice_ja,
            status="changed",
            session_id=session_id,
        )
    return {
        "ok": True,
        "message": f"[focus] now working in {chosen['projectName']}",
    }


def _noop_warmup() -> str:
    return ""


# entry.

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Amadeus backend server")
    parser.add_argument("--port", type=int, default=17777)
    args = parser.parse_args()
    asyncio.run(bootstrap(port=args.port))
