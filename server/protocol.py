"""
IPC protocol contract between frontend and Python backend.

Every message is JSON with this envelope:
    { "type": "req|evt|res", "id": "<uuid>", "method": "...", "params": {...} }

- req  : client requests something, expects a res with matching id
- evt  : server pushes an unsolicited event
- res  : server responds to a previous req (success or error)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypedDict

class StrEnum(str, Enum):
    """Python 3.10 compat: str Enum, added to stdlib in 3.11."""
    pass

# NotRequired added in 3.11; use typing_extensions fallback
try:
    from typing import NotRequired
except ImportError:
    from typing_extensions import NotRequired  # type: ignore[assignment]


# ── envelope ────────────────────────────────────────────────────────────────

class Envelope(TypedDict):
    type: str          # "req" | "evt" | "res"
    id: str            # uuid, echoed back for req→res correlation
    method: str        # e.g. "chat.send"
    params: dict[str, Any]


# ── method constants ────────────────────────────────────────────────────────

class Method(StrEnum):
    # Chat
    CHAT_SEND          = "chat.send"
    CHAT_ABORT         = "chat.abort"
    CHAT_TRANSLATE     = "chat.translate"
    CHAT_USER          = "chat.user"
    CHAT_TOKEN         = "chat.token"
    CHAT_COMPLETE      = "chat.complete"
    CHAT_ERROR         = "chat.error"
    CHAT_INTERRUPTED   = "chat.interrupted"
    CHAT_WORK_NOTE     = "chat.work_note"
    # Internal delivery receipt.  A Work Ledger terminal note stays pending
    # until the Observer has published its truthful text/voice decision.
    CHAT_WORK_NOTE_DELIVERED = "chat.work_note_delivered"
    CHAT_OBSERVER_DECISION = "chat.observer_decision"

    # Sessions
    SESSION_LIST       = "session.list"
    SESSION_CREATE     = "session.create"
    SESSION_LOAD       = "session.load"
    SESSION_DELETE     = "session.delete"
    SESSION_RENAME     = "session.rename"
    SESSION_OPEN_CONTEXT = "session.open_context"
    SESSION_CORRECT_PROJECT = "session.correct_project"
    SESSION_CHANGED    = "session.changed"
    PROJECT_CREATE     = "project.create"
    PROJECT_APPS_LIST  = "project.apps.list"
    DRAFT_APPS_LIST    = "draft.apps.list"

    # TTS
    TTS_SET_MODE       = "tts.set_mode"
    TTS_INTERRUPT      = "tts.interrupt"
    TTS_STATUS         = "tts.status"
    TTS_SENTENCE_START = "tts.sentence_start"
    TTS_SENTENCE_END   = "tts.sentence_end"
    TTS_TURN_COMPLETE  = "tts.turn_complete"

    # ASR
    ASR_START          = "asr.start"
    ASR_STOP           = "asr.stop"
    ASR_RECOGNIZED     = "asr.recognized"
    ASR_STATUS         = "asr.status"

    # Wake word
    WAKE_START         = "wake.start"
    WAKE_STOP          = "wake.stop"
    WAKE_STATUS        = "wake.status"
    WAKE_DETECTED      = "wake.detected"

    # VTS
    VTS_CONNECT        = "vts.connect"
    VTS_DISCONNECT     = "vts.disconnect"
    VTS_CONNECTED      = "vts.connected"
    VTS_DISCONNECTED   = "vts.disconnected"
    VTS_MODEL_LOADED   = "vts.model_loaded"

    # Expression / Emotion
    EXPRESSION_TRIGGER   = "expression.trigger"
    EXPRESSION_SET_BACKEND = "expression.set_backend"
    EXPRESSION_PRESETS   = "expression.presets"

    # VAD
    VAD_ENERGY         = "vad.energy"

    # OpenClaw
    OPENCLAW_TASK_EVENT  = "openclaw.task_event"
    OPENCLAW_TASK_RESULT = "openclaw.task_result"
    OPENCLAW_SUBMIT_TASK = "openclaw.submit_task"

    # VN Player
    VN_START             = "vn.start"
    VN_STOP              = "vn.stop"
    VN_STATUS            = "vn.status"
    VN_LINE              = "vn.line"
    VN_REACTION          = "vn.reaction"
    VN_CONTEXT_UPDATED   = "vn.context.updated"
    VN_SUMMARY           = "vn.summary"
    VN_ERROR             = "vn.error"
    VN_PLAYER_NOTE       = "vn.player.note"
    VN_PLAYER_ASK        = "vn.player.ask"
    VN_PLAYER_PIN        = "vn.player.pin"
    VN_CHOICE_ASK        = "vn.choice.ask"
    VN_MODE_SET          = "vn.mode.set"
    VN_LAUNCH_PROFILES   = "vn.launch.profiles"
    VN_LAUNCH_START      = "vn.launch.start"
    VN_LAUNCH_STOP       = "vn.launch.stop"
    VN_LAUNCH_STATUS     = "vn.launch.status"

    # Generic providers and future AUIP apps
    PROVIDER_RUN         = "provider.run"
    PROVIDER_CANCEL      = "provider.cancel"
    PROVIDER_LIST        = "provider.list"
    PROVIDER_EVENT       = "provider.event"
    PROVIDER_RESULT      = "provider.result"
    PROVIDER_ACTIVITY_LIST = "provider.activity.list"
    PROVIDER_DIFF        = "provider.diff"
    PROVIDER_STATUS      = "provider.status"
    # Read-only Host catalog for installed/built-in horizontal capabilities.
    # Native manifests and payloads remain in their owning subsystem.
    CAPABILITY_LIST      = "capability.list"
    # Host-managed MCP connections are available only to compatible Work
    # Providers. Main Chat never receives their tool schemas.
    MCP_CONNECTION_LIST  = "mcp.connection.list"
    MCP_CONNECTION_TEST  = "mcp.connection.test"
    # Cooperative AUIP applications. Apps publish bounded semantic facts;
    # only the host may invoke actions or turn receipts into character memory.
    AUIP_REGISTER        = "auip.register"
    AUIP_STATE_PUBLISH   = "auip.state.publish"
    AUIP_EVENT_PUBLISH   = "auip.event.publish"
    AUIP_ACTION_INVOKE   = "auip.action.invoke"
    AUIP_ACTION_RESULT   = "auip.action.result"
    AUIP_ACTION_REQUESTED = "auip.action.requested"
    AUIP_CONTROLLER_STATUS_PUBLISH = "auip.controller.status.publish"
    AUIP_CONTROLLER_REVOKE_REQUESTED = "auip.controller.revoke.requested"
    AUIP_STANCE_SET      = "auip.stance.set"
    AUIP_MODE_SET        = "auip.mode.set"
    AUIP_STEP            = "auip.step"
    AUIP_LEAVE           = "auip.leave"
    AUIP_SESSION_FOCUS   = "auip.session.focus"
    AUIP_SESSION_GET     = "auip.session.get"
    AUIP_SESSION_CLOSE   = "auip.session.close"
    AUIP_UPDATED         = "auip.updated"
    # Role/host requests a verified application launch; the trusted desktop
    # surface reports whether the OS accepted the open request.  AppSession
    # truth still begins only at AUIP registration.
    AUIP_LAUNCH_REQUESTED = "auip.launch.requested"
    AUIP_LAUNCH_RESULT   = "auip.launch.result"
    # Trusted desktop lifecycle for a Host-created AUIP surface.  Closing the
    # surface is distinct from claiming an arbitrary external process stopped.
    AUIP_SURFACE_CLOSE_REQUESTED = "auip.surface.close.requested"
    AUIP_SURFACE_CLOSE_RESULT = "auip.surface.close.result"
    # Trusted host surface only: validate one registered artifact and issue a
    # short-lived ticket for the restricted /auip/ws endpoint.
    AUIP_ATTACH_PREPARE  = "auip.attach.prepare"
    # Restricted app surface: an approved external bundle may ask the Host to
    # offer a user-visible Attach choice.  A request is not a ticket and does
    # not create an AppSession.
    AUIP_ATTACH_REQUEST  = "auip.attach.request"

    # Durable provider-neutral work control plane
    WORK_LIST           = "work.list"
    WORK_GET            = "work.get"
    WORK_START          = "work.start"
    WORK_FOCUS          = "work.focus"
    WORK_CONTINUE       = "work.continue"
    WORK_RETRY          = "work.retry"
    WORK_RESUME         = "work.resume"
    WORK_ACCEPT         = "work.accept"
    WORK_REOPEN         = "work.reopen"
    WORK_PROMOTE        = "work.promote"
    WORK_PROJECT_STATE  = "work.project.state"
    WORK_ARCHIVE        = "work.archive"
    WORK_PERMISSION_RESOLVE = "work.permission.resolve"
    WORK_UPDATED        = "work.updated"
    # Host-owned static preview for one exact WorkItem generation.  The
    # renderer supplies only ledger identity/revision; Host owns cwd, entry,
    # loopback URL, watcher, and server lifecycle.
    WORK_PREVIEW_OPEN   = "work.preview.open"
    WORK_PREVIEW_GET    = "work.preview.get"
    WORK_PREVIEW_CLOSE  = "work.preview.close"
    WORK_PREVIEW_UPDATED = "work.preview.updated"
    WORK_PREVIEW_OPEN_REQUESTED = "work.preview.open.requested"

    # Session-scoped, provider-neutral user decision requests.  Permission
    # remains a distinct audited Work control; this surface currently carries
    # only bounded object selections.
    ATTENTION_LIST      = "attention.list"
    ATTENTION_RESOLVE   = "attention.resolve"
    ATTENTION_UPDATED   = "attention.updated"

    # System / Config
    SYSTEM_GET_CONFIG  = "system.get_config"
    SYSTEM_SET_CONFIG  = "system.set_config"
    SYSTEM_LIST_WINDOWS = "system.list_windows"
    SYSTEM_GET_LOG     = "system.get_log"
    SYSTEM_CONFIG      = "system.config"
    SYSTEM_STATUS      = "system.status"
    SYSTEM_ERROR       = "system.error"
    RUNTIME_STATUS     = "runtime.status"

    # Render
    RENDER_START       = "render.start"
    RENDER_STOP        = "render.stop"
    RENDER_READY       = "render.ready"
    RENDER_EMOTION     = "render.emotion"
    RENDER_SPEAKING    = "render.speaking"
    RENDER_MOUTH       = "render.mouth"
    RENDER_SUBTITLE    = "render.subtitle"
    RENDER_SPRITE_FRAMES = "render.sprite_frames"
    RENDER_MODE        = "render.mode"
    RENDER_IDLE_ANIMATION    = "render.idle_animation"
    RENDER_IDLE_FRAME_INTERVAL = "render.idle_frame_interval"
    RENDER_SPRITE_CLIP_CONFIG = "render.sprite_clip_config"
    RENDER_MOUTH_CONFIG      = "render.mouth_config"
    RENDER_SPRITEFORGE_GRAPH = "render.spriteforge_graph"
    RENDER_SPRITEFORGE_INTENT = "render.spriteforge_intent"
    RENDER_SPRITEFORGE_RELEASE = "render.spriteforge_release"
    RENDER_HOLD_FRAME        = "render.hold_frame"
    RENDER_CLEAR_HOLD        = "render.clear_hold"

    # Wallpaper
    WALLPAPER_START    = "wallpaper.start"
    WALLPAPER_STOP     = "wallpaper.stop"
    WALLPAPER_ACTIVITY = "wallpaper.activity"
    WALLPAPER_CANVAS   = "wallpaper.canvas"
    WALLPAPER_READY    = "wallpaper.ready"
    WALLPAPER_EXITED   = "wallpaper.exited"


# ── param schemas (TypedDict for documentation, not enforced at runtime) ───

class ChatSendParams(TypedDict):
    text: str
    provider: str            # "deepseek" | "gemini" | "bedrock"
    model: NotRequired[str]
    session_id: NotRequired[str]
    visual: NotRequired[dict[str, Any]]

class ChatTokenParams(TypedDict):
    token: str
    turn_id: str

class ChatCompleteParams(TypedDict):
    turn_id: str
    full_text: str

class ChatTranslateParams(TypedDict):
    text: str
    turn_id: NotRequired[str]

class TtsSetModeParams(TypedDict):
    mode: str                # "gpt_sovits" | "edge" | ...

class TtsSentenceStartParams(TypedDict):
    sentence: str
    index: int

class AsrRecognizedParams(TypedDict):
    text: str
    is_final: bool

class AsrStatusParams(TypedDict):
    status: str              # "idle" | "listening" | "processing"

class VadEnergyParams(TypedDict):
    value: float

class ExpressionTriggerParams(TypedDict):
    name: str
    params: NotRequired[dict[str, float]]

class ExpressionSetBackendParams(TypedDict):
    backend: str             # "vts" | "pixi" | "both"

class SystemConfigParams(TypedDict):
    values: dict[str, Any]

class SystemStatusParams(TypedDict):
    vts_connected: bool
    tts_ready: bool
    asr_ready: bool
    live_api_active: bool
    backend: str             # "vts" | "pixi"
    vision: NotRequired[dict[str, Any]]


class ProviderRunParams(TypedDict):
    provider: str            # registered provider id
    task: str
    cwd: NotRequired[str]
    mode: NotRequired[str]
    metadata: NotRequired[dict[str, Any]]
    work_item_id: NotRequired[str]


class ProviderEventParams(TypedDict):
    provider: str
    run_id: str
    type: str
    payload: dict[str, Any]
    metadata: NotRequired[dict[str, Any]]
    time_ms: NotRequired[int]
