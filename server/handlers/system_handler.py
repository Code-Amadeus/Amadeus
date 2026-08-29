"""Adapter for system config, status, and lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from collections.abc import Callable
from typing import Any

from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import RequestHandler


def _startup_field(
    key: str,
    label: str,
    value: Any = "",
    *,
    field_type: str = "text",
    options: tuple[Any, ...] = (),
    description: str = "",
    secret_configured: bool | None = None,
    editable: bool = True,
    minimum: float | None = None,
    maximum: float | None = None,
    step: float | None = None,
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "key": key,
        "label": label,
        "type": field_type,
        "description": description,
        "restart_required": True,
        "editable": bool(editable),
    }
    if options:
        field["options"] = list(options)
    if minimum is not None:
        field["min"] = minimum
    if maximum is not None:
        field["max"] = maximum
    if step is not None:
        field["step"] = step
    if field_type == "secret":
        field["configured"] = bool(secret_configured)
    else:
        field["value"] = value
    return field


def _voice_configuration(settings: Any) -> list[dict[str, Any]]:
    from asr.registry import asr_backend_statuses
    from tts.registry import tts_backend_statuses

    asr_selected = str(settings.ASR_BACKEND or "qwen3_asr").strip().lower()
    tts_selected = str(settings.TTS_BACKEND or "gpt_sovits").strip().lower()
    asr_statuses = asr_backend_statuses(asr_selected)
    tts_statuses = tts_backend_statuses(tts_selected)
    asr_status = next((item for item in asr_statuses if item["selected"]), {})
    tts_status = next((item for item in tts_statuses if item["selected"]), {})
    embedded_tts_status = next(
        (item for item in tts_statuses if item["id"] == "gpt_sovits"),
        {},
    )
    remote_tts_status = next(
        (item for item in tts_statuses if item["id"] == "openai_compatible"),
        {},
    )
    reference_consumers = [
        str(item.get("label") or item.get("id") or "")
        for item in tts_statuses
        if item.get("supports_reference_conditioning")
    ]
    selected_reference_consumer = bool(
        tts_status.get("supports_reference_conditioning")
    )
    wake_status = next(
        (
            item
            for item in asr_backend_statuses(str(settings.WAKE_ASR_BACKEND or "sense_voice"))
            if item["selected"]
        ),
        {},
    )
    try:
        from asr.microphone import list_microphone_devices

        microphones = list_microphone_devices()
    except Exception:
        microphones = []
    microphone_options: list[dict[str, str]] = [
        {"value": "-1", "label": "Automatic"},
    ]
    microphone_options.extend(
        {
            "value": str(item.index),
            "label": f"{item.name} · {item.host_api}" if item.host_api else item.name,
        }
        for item in microphones
        if item.index is not None
    )
    return [
        {
            "id": "conversation_asr",
            "label": "Conversation recognition",
            "description": "Full transcription after manual listening or Wake handoff. Qwen remains the embedded default and owns speculative endpoint optimization.",
            "active": True,
            "configured": bool(asr_status.get("available")),
            "status": str(asr_status.get("state") or "unavailable"),
            "status_ok": bool(asr_status.get("available")),
            "status_detail": str(asr_status.get("detail") or ""),
            "fields": [
                _startup_field(
                    "ASR_BACKEND", "Backend", settings.ASR_BACKEND,
                    field_type="select",
                    options=tuple(
                        {"value": item["id"], "label": item["label"]}
                        for item in asr_statuses
                    ),
                ),
                _startup_field(
                    "ASR_LANGUAGE", "Recognition language", settings.ASR_LANGUAGE,
                    description="auto or an ISO-639-1 language code such as en, ja, or zh.",
                ),
                _startup_field(
                    "ASR_CONTEXT", "Context and terminology", settings.ASR_CONTEXT,
                    description="Prompt or domain vocabulary used by compatible full recognizers.",
                ),
                _startup_field(
                    "QWEN3_ASR_MODEL_PATH", "Qwen model directory",
                    settings.QWEN3_ASR_MODEL_PATH, field_type="path",
                    description=(
                        "Leave blank to use assets/models/asr/qwen3-asr-0.6b, then a legacy "
                        "Hugging Face cache if present."
                    ),
                ),
                _startup_field(
                    "QWEN3_ASR_DEVICE", "Qwen device", settings.QWEN3_ASR_DEVICE,
                    field_type="select", options=("auto", "cpu", "cuda"),
                    description="Used only by the embedded Qwen recognizer.",
                ),
                _startup_field(
                    "QWEN3_ASR_REQUIRE_CUDA", "Require Qwen CUDA",
                    bool(settings.QWEN3_ASR_REQUIRE_CUDA), field_type="boolean",
                    description="Fail visibly instead of falling back to CPU when CUDA is requested but unavailable.",
                ),
                _startup_field(
                    "MICROPHONE_DEVICE_INDEX", "Microphone", settings.MICROPHONE_DEVICE_INDEX,
                    field_type="select", options=tuple(microphone_options),
                ),
                _startup_field(
                    "MICROPHONE_PREFERRED_NAME", "Preferred microphone name",
                    settings.MICROPHONE_PREFERRED_NAME,
                    description="Optional partial-name fallback when device indices change.",
                ),
                _startup_field(
                    "ASR_LISTEN_TIMEOUT_SECONDS", "Wait for speech",
                    settings.ASR_LISTEN_TIMEOUT_SECONDS, field_type="number",
                    minimum=1, maximum=120, step=1,
                    description="Seconds to wait for speech to begin after listening starts.",
                ),
                _startup_field(
                    "ASR_VAD_SILENCE_MS", "End-of-speech pause",
                    settings.ASR_VAD_SILENCE_MS, field_type="number",
                    minimum=100, maximum=3000, step=50,
                    description="Silence required before a spoken turn is considered complete. Increase this if natural pauses are cut off.",
                ),
            ],
        },
        {
            "id": "asr_remote",
            "label": "Remote transcription API",
            "description": "OpenAI-compatible POST /audio/transcriptions. Used only when Conversation recognition selects openai_compatible.",
            "active": asr_selected == "openai_compatible",
            "configured": bool(settings.ASR_API_BASE_URL and settings.ASR_API_MODEL),
            "status": "remote" if asr_selected == "openai_compatible" else "available",
            "status_ok": bool(settings.ASR_API_BASE_URL and settings.ASR_API_MODEL),
            "fields": [
                _startup_field("ASR_API_BASE_URL", "API base URL", settings.ASR_API_BASE_URL, field_type="url"),
                _startup_field("ASR_API_KEY", "API key", field_type="secret", secret_configured=bool(settings.ASR_API_KEY)),
                _startup_field("ASR_API_MODEL", "Model", settings.ASR_API_MODEL),
            ],
        },
        {
            "id": "wake_asr",
            "label": "Wake recognition",
            "description": "Independent always-on recognizer. It can stay on SenseVoice while Conversation recognition uses Qwen or a remote API.",
            "active": bool(settings.WAKE_ENABLED),
            "configured": not bool(settings.WAKE_ENABLED) or bool(wake_status.get("available")),
            "status": "disabled" if not settings.WAKE_ENABLED else str(wake_status.get("state") or "unavailable"),
            "status_ok": not bool(settings.WAKE_ENABLED) or bool(wake_status.get("available")),
            "status_detail": str(wake_status.get("detail") or ""),
            "fields": [
                _startup_field("WAKE_ENABLED", "Wake service", bool(settings.WAKE_ENABLED), field_type="boolean"),
                _startup_field(
                    "WAKE_PHRASES", "Wake phrases", settings.WAKE_PHRASES,
                    description="Comma-separated phrases matched by the wake recognizer.",
                ),
                _startup_field(
                    "WAKE_AUTO_SEND_TO_CHAT", "Send command to Chat",
                    bool(settings.WAKE_AUTO_SEND_TO_CHAT), field_type="boolean",
                    description="Submit the recognized command after a wake handoff.",
                ),
                _startup_field(
                    "WAKE_ASR_BACKEND", "Wake backend", settings.WAKE_ASR_BACKEND,
                    field_type="select", options=("sense_voice", "qwen3_asr"),
                ),
                _startup_field(
                    "WAKE_SENSEVOICE_LANGUAGES", "Wake languages",
                    settings.WAKE_SENSEVOICE_LANGUAGES,
                    description="Comma-separated SenseVoice language passes.",
                ),
                _startup_field(
                    "SENSEVOICE_LANGUAGE", "SenseVoice conversation language",
                    settings.SENSEVOICE_LANGUAGE, field_type="select",
                    options=("auto", "en", "zh", "ja", "yue", "ko"),
                ),
                _startup_field(
                    "SENSEVOICE_MODEL_PATH", "SenseVoice model path",
                    settings.SENSEVOICE_MODEL_PATH, field_type="path",
                    description="Optional local model directory when it is not installed in the default cache.",
                ),
            ],
        },
        {
            "id": "acoustic_pipeline",
            "label": "Echo cancellation & interruption",
            "description": "Desktop startup controls for realtime AEC and barge-in. These settings affect when microphone speech may interrupt playback.",
            "active": bool(settings.AEC_REALTIME_ENABLED),
            "configured": True,
            "status": "available",
            "status_ok": True,
            "fields": [
                _startup_field(
                    "AEC_REALTIME_ENABLED", "Realtime echo cancellation",
                    bool(settings.AEC_REALTIME_ENABLED), field_type="boolean",
                ),
                _startup_field(
                    "AEC_REALTIME_BARGE_IN", "Allow microphone interruption",
                    bool(settings.AEC_REALTIME_BARGE_IN), field_type="boolean",
                    description="When enabled, confirmed near-end speech may stop current playback.",
                ),
                _startup_field(
                    "AEC_REALTIME_DELAY_MS", "AEC reference delay",
                    settings.AEC_REALTIME_DELAY_MS, field_type="number",
                    minimum=0, maximum=2000, step=10,
                    description="Explicit playback-to-microphone reference delay in milliseconds.",
                ),
            ],
        },
        {
            "id": "voice_reference_profile",
            "label": "Voice reference profile",
            "description": "Shared reference-conditioning inputs carried by the common TTS request contract. Backends that do not declare this capability ignore them.",
            "active": selected_reference_consumer,
            "configured": bool(
                settings.TTS_REF_AUDIO_JA or settings.TTS_REF_AUDIO_EN
            ),
            "status": "available",
            "status_ok": True,
            "status_detail": (
                "Used by current backend: "
                + str(tts_status.get("label") or tts_selected)
                if selected_reference_consumer
                else "Stored as a shared profile; the current backend ignores reference conditioning."
                + (
                    " Supported by: " + ", ".join(reference_consumers) + "."
                    if reference_consumers
                    else ""
                )
            ),
            "fields": [
                _startup_field(
                    "TTS_REF_AUDIO_JA", "Japanese reference audio",
                    settings.TTS_REF_AUDIO_JA, field_type="path",
                ),
                _startup_field(
                    "TTS_REF_TEXT_JA", "Japanese reference transcript",
                    settings.TTS_REF_TEXT_JA,
                ),
                _startup_field(
                    "TTS_REF_AUDIO_EN", "English reference audio",
                    settings.TTS_REF_AUDIO_EN, field_type="path",
                ),
                _startup_field(
                    "TTS_REF_TEXT_EN", "English reference transcript",
                    settings.TTS_REF_TEXT_EN,
                ),
            ],
        },
        {
            "id": "speech_synthesis",
            "label": "Speech synthesis",
            "description": "The embedded default is Amadeus's low-latency GPT-SoVITS v3 rewrite and accepts v3 checkpoints only. Remote audio enters the same playback, subtitle, AEC, and mouth-signal pipeline.",
            "active": tts_selected != "disabled",
            "configured": bool(tts_status.get("available")),
            "status": str(tts_status.get("state") or "unavailable"),
            "status_ok": bool(tts_status.get("available")),
            "status_detail": str(tts_status.get("detail") or ""),
            "fields": [
                _startup_field(
                    "TTS_BACKEND", "Backend", settings.TTS_BACKEND,
                    field_type="select",
                    options=tuple(
                        {"value": item["id"], "label": item["label"]}
                        for item in tts_statuses
                    ),
                ),
            ],
        },
        {
            "id": "tts_embedded_v3",
            "label": "Embedded GPT-SoVITS v3 model",
            "description": "Checkpoint pair for the Amadeus low-latency rewrite. v1 and v2 checkpoints are not supported by this runtime.",
            "active": tts_selected == "gpt_sovits",
            "configured": bool(embedded_tts_status.get("available")),
            "status": str(embedded_tts_status.get("state") or "not_installed"),
            "status_ok": bool(embedded_tts_status.get("available")),
            "status_detail": str(embedded_tts_status.get("detail") or ""),
            "fields": [
                _startup_field(
                    "TTS_DEVICE", "Inference device", settings.TTS_DEVICE,
                    description="auto/cuda, cuda:N, or cpu. Used only by the embedded backend.",
                ),
                _startup_field(
                    "TTS_GPT_MODEL_PATH", "GPT semantic checkpoint (v3)",
                    settings.TTS_GPT_MODEL_PATH,
                    description="Path to a GPT-SoVITS v3 .ckpt file; relative paths resolve from the repository root.",
                ),
                _startup_field(
                    "TTS_SOVITS_MODEL_PATH", "SoVITS acoustic checkpoint (v3)",
                    settings.TTS_SOVITS_MODEL_PATH,
                    description="Path to a GPT-SoVITS v3 .pth file; relative paths resolve from the repository root.",
                ),
            ],
        },
        {
            "id": "tts_remote",
            "label": "Remote speech API",
            "description": "Buffered WAV keeps broad OpenAI-compatible support. OpenAI SSE streams PCM into first-packet playback and must be explicitly selected.",
            "active": tts_selected == "openai_compatible",
            "configured": bool(remote_tts_status.get("available")),
            "status": str(remote_tts_status.get("state") or "unavailable"),
            "status_ok": bool(remote_tts_status.get("available")),
            "status_detail": str(remote_tts_status.get("detail") or ""),
            "fields": [
                _startup_field("TTS_API_BASE_URL", "API base URL", settings.TTS_API_BASE_URL, field_type="url"),
                _startup_field("TTS_API_KEY", "API key", field_type="secret", secret_configured=bool(settings.TTS_API_KEY)),
                _startup_field("TTS_API_MODEL", "Model", settings.TTS_API_MODEL),
                _startup_field("TTS_API_VOICE", "Voice", settings.TTS_API_VOICE),
                _startup_field(
                    "TTS_API_STREAM_PROTOCOL", "Response mode",
                    settings.TTS_API_STREAM_PROTOCOL,
                    field_type="select",
                    options=(
                        {"value": "buffered", "label": "Buffered WAV · compatible"},
                        {"value": "openai_sse", "label": "OpenAI SSE · streaming PCM"},
                    ),
                    description="Use OpenAI SSE only when the endpoint implements speech.audio.delta events.",
                ),
            ],
        },
    ]


def _avatar_configuration(settings: Any) -> list[dict[str, Any]]:
    enabled = bool(settings.VTS_ENABLED)
    return [
        {
            "id": "vts_compatibility",
            "label": "VTube Studio compatibility",
            "description": "Optional downstream mouth-signal and parameter forwarding. SpriteForge browser animation remains independent of this compatibility path.",
            "active": enabled,
            "configured": not enabled or bool(str(settings.VTS_WS_URL or "").strip()),
            "status": "available" if enabled else "disabled",
            "status_ok": not enabled or bool(str(settings.VTS_WS_URL or "").strip()),
            "fields": [
                _startup_field(
                    "VTS_ENABLED", "Enable compatibility output", enabled,
                    field_type="boolean",
                ),
                _startup_field(
                    "VTS_WS_URL", "WebSocket URL", settings.VTS_WS_URL,
                    field_type="url",
                ),
                _startup_field(
                    "VTS_TOKEN_FILE", "Authentication token file",
                    settings.VTS_TOKEN_FILE, field_type="path",
                    description="Local token cache path; the token itself is never shown in Settings.",
                ),
            ],
        }
    ]


def _model_connections(
    settings: Any,
    active_provider: str,
    *,
    local_status: dict[str, Any] | None = None,
    hybrid_status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    active = str(active_provider or "deepseek").strip().lower()
    active_connections = {
        "hybrid": {"hybrid_local", "bedrock"},
        "hybrid2": {"hybrid_local", "deepseek"},
        "hybrid3": {"hybrid_local", "openai"},
    }.get(active, {active})
    local_type = str(settings.LOCAL_LLM_TYPE or "llama_server").strip().lower()
    local_fields = [
        _startup_field(
            "LOCAL_LLM_TYPE", "Backend type", local_type,
            field_type="select", options=("llama_server", "lmstudio", "ollama", "cli"),
        ),
        _startup_field("LOCAL_LLM_MODEL", "Model", settings.LOCAL_LLM_MODEL),
        _startup_field(
            "RAG_ENABLED_FOR_LOCAL", "Local knowledge retrieval",
            bool(settings.RAG_ENABLED_FOR_LOCAL), field_type="boolean",
        ),
        _startup_field(
            "RAG_TOP_K", "Knowledge results", settings.RAG_TOP_K,
            field_type="number", minimum=1, maximum=20, step=1,
        ),
        _startup_field(
            "RAG_MAX_DISTANCE", "Knowledge distance threshold",
            settings.RAG_MAX_DISTANCE, field_type="number",
            minimum=0, maximum=2, step=0.01,
        ),
    ]
    if local_type == "llama_server":
        local_fields.extend(
            [
                _startup_field(
                    "LOCAL_LLM_LAUNCH_MODE", "Server ownership",
                    settings.LOCAL_LLM_LAUNCH_MODE,
                    field_type="select", options=(
                        {"value": "external", "label": "External server"},
                        {"value": "managed", "label": "Managed by Amadeus"},
                    ),
                    description="External reuses an existing llama.cpp server; managed starts and stops it with Amadeus.",
                ),
                _startup_field(
                    "LOCAL_LLM_URL", "llama.cpp server URL", settings.LOCAL_LLM_URL,
                    field_type="url",
                ),
            ]
        )
        local_fields.extend(
            [
                _startup_field(
                    "LOCAL_LLM_CLI_PATH", "llama-server executable",
                    settings.LOCAL_LLM_CLI_PATH, field_type="path",
                    description="Used by managed mode and the repository BAT launchers; optional for an independently managed external server.",
                ),
                _startup_field(
                    "LOCAL_LLM_CLI_MODEL_PATH", "GGUF model file",
                    settings.LOCAL_LLM_MODEL_PATH, field_type="path",
                    description="Used by managed mode and the repository BAT launchers.",
                ),
                _startup_field(
                    "LOCAL_LLM_CLI_CONTEXT", "Context size",
                    getattr(settings, "_LLM_CONTEXT", "4096"),
                ),
                _startup_field(
                    "LOCAL_LLM_CLI_THREADS", "CPU threads",
                    getattr(settings, "_LLM_THREADS", "4"),
                ),
                _startup_field(
                    "LOCAL_LLM_CLI_NGL", "GPU layers",
                    getattr(settings, "_LLM_NGL", "99"),
                ),
                _startup_field(
                    "LOCAL_LLM_CUDA_VISIBLE_DEVICES", "Visible GPU IDs",
                    settings.LOCAL_LLM_CUDA_VISIBLE_DEVICES,
                    description="Optional nvidia-smi indices, for example 1. Leave blank for automatic visibility.",
                ),
            ]
        )
    elif local_type == "lmstudio":
        local_fields.append(
            _startup_field(
                "LOCAL_LLM_LM_STUDIO_URL", "LM Studio URL",
                settings.LOCAL_LLM_LM_STUDIO_URL, field_type="url",
            )
        )
    elif local_type == "ollama":
        local_fields.append(
            _startup_field(
                "LOCAL_LLM_OLLAMA_URL", "Ollama URL",
                settings.LOCAL_LLM_OLLAMA_URL, field_type="url",
            )
        )
    else:
        local_fields.extend(
            [
                _startup_field(
                    "LOCAL_LLM_CLI_PATH", "llama-cli executable",
                    settings.LOCAL_LLM_CLI_PATH, field_type="path",
                ),
                _startup_field(
                    "LOCAL_LLM_CLI_MODEL_PATH", "GGUF model file",
                    settings.LOCAL_LLM_MODEL_PATH, field_type="path",
                ),
            ]
        )

    local_status = dict(local_status or {})
    hybrid_status = dict(hybrid_status or {})
    return [
        {
            "id": "profile",
            "label": "Desktop default",
            "description": "The model profile selected when the desktop backend starts.",
            "active": True,
            "configured": True,
            "fields": [
                _startup_field(
                    "LLM_PROVIDER", "Default model profile", settings.LLM_PROVIDER,
                    field_type="select",
                    options=("deepseek", "openai", "gemini", "bedrock", "local", "hybrid", "hybrid2", "hybrid3"),
                ),
            ],
        },
        {
            "id": "deepseek",
            "label": "DeepSeek",
            "active": "deepseek" in active_connections,
            "configured": bool(settings.DEEPSEEK_API_KEY),
            "fields": [
                _startup_field(
                    "DEEPSEEK_API_KEY", "API key", field_type="secret",
                    secret_configured=bool(settings.DEEPSEEK_API_KEY),
                ),
                _startup_field(
                    "DEEPSEEK_BASE_URL", "Base URL", settings.DEEPSEEK_BASE_URL,
                    field_type="url",
                ),
                _startup_field(
                    "DEEPSEEK_MODEL_NAME", "Model", settings.DEEPSEEK_MODEL_NAME,
                    description="Independent from the Codex Work Provider model.",
                ),
            ],
        },
        {
            "id": "openai",
            "label": "OpenAI-compatible",
            "active": "openai" in active_connections,
            "configured": bool(settings.OPENAI_API_KEY),
            "fields": [
                _startup_field(
                    "OPENAI_API_KEY", "API key", field_type="secret",
                    secret_configured=bool(settings.OPENAI_API_KEY),
                ),
                _startup_field(
                    "OPENAI_BASE_URL", "Base URL", settings.OPENAI_BASE_URL,
                    field_type="url",
                ),
                _startup_field("OPENAI_MODEL_NAME", "Model", settings.OPENAI_MODEL_NAME),
            ],
        },
        {
            "id": "gemini",
            "label": "Gemini",
            "active": "gemini" in active_connections,
            "configured": bool(settings.GEMINI_API_KEY),
            "fields": [
                _startup_field(
                    "GEMINI_API_KEY", "API key", field_type="secret",
                    secret_configured=bool(settings.GEMINI_API_KEY),
                ),
                _startup_field("GEMINI_MODEL_NAME", "Model", settings.GEMINI_MODEL_NAME),
            ],
        },
        {
            "id": "bedrock",
            "label": "AWS Bedrock",
            "active": "bedrock" in active_connections,
            "configured": bool(
                settings.AWS_BEDROCK_BEARER_TOKEN
                or settings.AWS_BEDROCK_AUTH_MODE in {"auto", "boto3"}
            ),
            "fields": [
                _startup_field(
                    "BEDROCK_AUTH_MODE", "Authentication", settings.AWS_BEDROCK_AUTH_MODE,
                    field_type="select", options=("auto", "boto3", "bearer"),
                ),
                _startup_field(
                    "AWS_BEARER_TOKEN_BEDROCK", "Bearer token", field_type="secret",
                    secret_configured=bool(settings.AWS_BEDROCK_BEARER_TOKEN),
                ),
                _startup_field("AWS_BEDROCK_REGION", "Region", settings.AWS_BEDROCK_REGION),
                _startup_field("AWS_BEDROCK_MODEL_ID", "Model ID", settings.AWS_BEDROCK_MODEL_ID),
                _startup_field(
                    "AWS_BEDROCK_USE_INFERENCE_PROFILE", "Use inference profile",
                    bool(settings.AWS_BEDROCK_USE_INFERENCE_PROFILE), field_type="boolean",
                ),
                _startup_field(
                    "AWS_BEDROCK_INFERENCE_PROFILE_ID", "Inference profile ID",
                    settings.AWS_BEDROCK_INFERENCE_PROFILE_ID,
                ),
            ],
        },
        {
            "id": "local",
            "label": "Pure-local model",
            "description": "Within the optional pure-local profile, llama.cpp is the default backend; LM Studio, Ollama, and llama-cli remain compatibility choices.",
            "active": "local" in active_connections,
            "configured": bool(local_status.get("configured")),
            "status": str(local_status.get("state") or "unavailable"),
            "status_ok": bool(local_status.get("available")),
            "status_detail": str(local_status.get("detail") or "Status is checked at startup."),
            "fields": local_fields,
        },
        {
            "id": "hybrid_local",
            "label": "Hybrid local head",
            "description": "Dedicated OpenAI-compatible endpoint used only for the fast first sentence in hybrid profiles. The optional Hybrid BAT launcher shares the llama.cpp executable and GGUF settings above.",
            "active": "hybrid_local" in active_connections,
            "configured": bool(hybrid_status.get("configured")),
            "status": str(hybrid_status.get("state") or "unavailable"),
            "status_ok": bool(hybrid_status.get("available")),
            "status_detail": str(hybrid_status.get("detail") or "Status is checked at startup."),
            "fields": [
                _startup_field(
                    "HYBRID_LOCAL_LLM_URL", "Head endpoint",
                    settings.HYBRID_LOCAL_LLM_URL, field_type="url",
                ),
                _startup_field(
                    "HYBRID_LOCAL_LLM_MODEL", "Head model",
                    settings.HYBRID_LOCAL_LLM_MODEL,
                ),
            ],
        },
    ]


def _model_role_configuration(settings: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": "work_observer",
            "label": "Work observer",
            "description": "Summarizes Provider progress; inherits the main model when left blank.",
            "configured": True,
            "fields": [
                _startup_field("WORK_OBSERVER_PROVIDER", "Provider override", settings.WORK_OBSERVER_PROVIDER),
                _startup_field("WORK_OBSERVER_MODEL", "Model override", settings.WORK_OBSERVER_MODEL),
            ],
        },
        {
            "id": "auip_narration",
            "label": "AUIP narration",
            "description": "Narrates verified application outcomes; inherits Work observer/main model.",
            "configured": True,
            "fields": [
                _startup_field("AUIP_NARRATION_PROVIDER", "Provider override", settings.AUIP_NARRATION_PROVIDER),
                _startup_field("AUIP_NARRATION_MODEL", "Model override", settings.AUIP_NARRATION_MODEL),
            ],
        },
        {
            "id": "auip_action",
            "label": "AUIP action decision",
            "description": "Optional decision-quality override for AUIP participation.",
            "configured": True,
            "fields": [
                _startup_field("AUIP_ACTION_PROVIDER", "Provider override", settings.AUIP_ACTION_PROVIDER),
                _startup_field("AUIP_ACTION_MODEL", "Model override", settings.AUIP_ACTION_MODEL),
                _startup_field(
                    "AUIP_ACTION_REASONING_EFFORT", "Reasoning effort",
                    settings.AUIP_ACTION_REASONING_EFFORT, field_type="select",
                    options=("none", "minimal", "low", "medium", "high", "max"),
                ),
                _startup_field(
                    "AUIP_ACTION_SERVICE_TIER", "Service tier",
                    settings.AUIP_ACTION_SERVICE_TIER, field_type="select",
                    options=("auto", "default", "fast", "priority"),
                ),
            ],
        },
    ]


def _work_provider_configuration(settings: Any) -> list[dict[str, Any]]:
    codex_transport = (
        "app_server" if settings.CODEX_APP_SERVER_PROVIDER_ENABLED
        else "direct" if settings.DIRECT_CODEX_PROVIDER_ENABLED
        else "disabled"
    )
    return [
        {
            "id": "browser",
            "label": "Browser",
            "description": "Host-managed browser work Provider; no connection settings.",
            "fields": [],
        },
        {
            "id": "openclaw",
            "label": "OpenClaw",
            "description": "Remote agent Gateway used only after the main role delegates work.",
            "fields": [
                _startup_field(
                    "OPENCLAW_BASE_URL", "Gateway URL", settings.OPENCLAW_BASE_URL,
                    field_type="url",
                ),
                _startup_field(
                    "OPENCLAW_GATEWAY_TOKEN", "Gateway token", field_type="secret",
                    secret_configured=bool(settings.OPENCLAW_TOKEN),
                ),
                _startup_field(
                    "OPENCLAW_PROJECT_DIR", "OpenClaw project directory",
                    settings.OPENCLAW_PROJECT_DIR, field_type="path",
                ),
            ],
        },
        {
            "id": "codex",
            "label": "Codex",
            "description": "Coding Provider. Exactly one App Server or Direct transport may own this id.",
            "fields": [
                _startup_field(
                    "CODEX_PROVIDER_TRANSPORT", "Transport", codex_transport,
                    field_type="select", options=("app_server", "direct", "disabled"),
                ),
                _startup_field(
                    "CODEX_APP_SERVER_CODEX_BIN", "App Server executable",
                    settings.CODEX_APP_SERVER_CODEX_BIN, field_type="path",
                ),
                _startup_field(
                    "CODEX_APP_SERVER_MODEL_PROVIDER", "Model provider",
                    settings.CODEX_APP_SERVER_MODEL_PROVIDER,
                ),
                _startup_field(
                    "CODEX_APP_SERVER_PROVIDER_BASE_URL", "Provider base URL",
                    settings.CODEX_APP_SERVER_PROVIDER_BASE_URL, field_type="url",
                ),
                _startup_field("CODEX_APP_SERVER_MODEL", "Model", settings.CODEX_APP_SERVER_MODEL),
                _startup_field(
                    "CODEX_APP_SERVER_REASONING_EFFORT", "Reasoning effort",
                    settings.CODEX_APP_SERVER_REASONING_EFFORT, field_type="select",
                    options=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
                ),
                _startup_field(
                    "CODEX_APP_SERVER_SERVICE_TIER", "Service tier",
                    settings.CODEX_APP_SERVER_SERVICE_TIER, field_type="select",
                    options=("", "auto", "default", "flex", "priority", "fast", "ultrafast"),
                ),
                _startup_field(
                    "DIRECT_CODEX_CLI_PATH", "Direct CLI executable",
                    settings.DIRECT_CODEX_CLI_PATH, field_type="path",
                ),
            ],
        },
    ]


class SystemHandler(RequestHandler):
    methods = [
        Method.SYSTEM_GET_CONFIG,
        Method.SYSTEM_SET_CONFIG,
        Method.SYSTEM_LIST_WINDOWS,
        Method.SYSTEM_GET_LOG,
        Method.RUNTIME_STATUS,
    ]

    def __init__(self) -> None:
        self._vts_manager = None
        self._asr_manager = None
        self._asr_handler = None
        self._asr_manager_getter: Callable[[], Any] | None = None
        self._playback_manager = None
        self._is_chat_busy: Callable[[], bool] | None = None
        self._log_path: Path | None = None

    def configure(
        self,
        vts_manager=None,
        asr_manager=None,
        asr_handler=None,
        playback_manager=None,
        is_chat_busy: Callable[[], bool] | None = None,
        project_root: Path | None = None,
        asr_manager_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._vts_manager = vts_manager
        self._asr_manager = asr_manager
        self._asr_handler = asr_handler
        self._asr_manager_getter = asr_manager_getter
        self._playback_manager = playback_manager
        self._is_chat_busy = is_chat_busy
        if project_root:
            self._log_path = Path(project_root) / "server.log"

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.SYSTEM_GET_CONFIG:
            return await self._get_config(params)
        if method == Method.SYSTEM_SET_CONFIG:
            return await self._set_config(params)
        if method == Method.SYSTEM_LIST_WINDOWS:
            return await self._list_windows(params)
        if method == Method.SYSTEM_GET_LOG:
            return await self._get_log(params)
        if method == Method.RUNTIME_STATUS:
            from server.runtime_status import status_collector

            # collect() 是同步只读操作，mic 段可能触发 PyAudio 设备枚举
            # （几十毫秒），放线程里跑避免阻塞事件循环。
            return await asyncio.to_thread(status_collector.collect)
        return None

    async def _get_config(self, params: dict[str, Any]) -> dict[str, Any]:
        # read from existing config/settings.py constants
        from config import settings
        import llm.client as llm_client
        from server import visual_runtime
        from server import presentation_runtime
        from render.character_pack import character_pack_status
        from config.asset_packages import external_asset_pack_status
        import tts.pipeline as tts_pipeline
        from core.chat_runtime import get_chat_runtime
        vision = visual_runtime.get_config()
        chat_runtime = get_chat_runtime()
        from asr.registry import asr_backend_statuses
        from llm.local_backends import hybrid_local_status, local_backend_status
        from tts.registry import tts_backend_statuses

        asr_backend = (
            str(getattr(self._asr_handler, "backend_name", "") or "")
            if self._asr_handler is not None
            else str(getattr(settings, "ASR_BACKEND", "qwen3_asr") or "qwen3_asr")
        )
        active_provider = getattr(llm_client, 'LLM_PROVIDER', 'deepseek')
        project_root = Path(__file__).resolve().parents[2]
        voice_configuration, local_status, hybrid_status = await asyncio.gather(
            asyncio.to_thread(_voice_configuration, settings),
            asyncio.to_thread(local_backend_status, settings, project_root=project_root),
            asyncio.to_thread(hybrid_local_status, settings),
        )
        return {
            "vts_ws_url": getattr(settings, 'VTS_WS_URL', ''),
            "llm_provider": active_provider,
            "tts_device": getattr(settings, 'TTS_DEVICE', ''),
            "tts_mode": tts_pipeline.current_tts_mode(),
            "tts_output_language": tts_pipeline.current_tts_language_code(),
            "tts_backend": getattr(settings, "TTS_BACKEND", "gpt_sovits"),
            "asr_backend": asr_backend,
            "asr_language": getattr(settings, "ASR_LANGUAGE", "auto"),
            "asr_context": getattr(settings, "ASR_CONTEXT", ""),
            "local_llm_type": chat_runtime.local_llm_type,
            "aec_realtime_enabled": bool(getattr(settings, "AEC_REALTIME_ENABLED", False)),
            "aec_realtime_barge_in": bool(getattr(settings, "AEC_REALTIME_BARGE_IN", False)),
            "aec_realtime_delay_ms": float(getattr(settings, "AEC_REALTIME_DELAY_MS", 280.0)),
            "wake_enabled": bool(getattr(settings, "WAKE_ENABLED", False)),
            "visual_asset_pack": external_asset_pack_status("visual-runtime"),
            "character_pack": character_pack_status(),
            "settings_scope": "runtime_only",
            "model_connections": _model_connections(
                settings,
                active_provider,
                local_status=local_status,
                hybrid_status=hybrid_status,
            ),
            "model_roles": _model_role_configuration(settings),
            "work_provider_configuration": _work_provider_configuration(settings),
            "voice_configuration": voice_configuration,
            "avatar_configuration": _avatar_configuration(settings),
            "asr_backends": asr_backend_statuses(asr_backend),
            "tts_backends": tts_backend_statuses(
                str(getattr(settings, "TTS_BACKEND", "gpt_sovits"))
            ),
            "vision_enabled": vision.get("enabled", False),
            "vision_mode": vision.get("mode", "off"),
            "vision_scope": vision.get("scope", "full_screen"),
            "vision_provider": vision.get("provider", "auto"),
            "vision_max_long_side": vision.get("max_long_side", 960),
            "vision_jpeg_quality": vision.get("jpeg_quality", 68),
            "vision_region": vision.get("region", ""),
            "vision_window_handle": vision.get("window_handle", ""),
            **presentation_runtime.get_config(),
            "control_decision_mode": (
                "authority"
                if bool(getattr(chat_runtime, "_control_proposal_authority", False))
                else "shadow"
                if getattr(chat_runtime, "_control_proposal_observer", None) is not None
                else "disabled"
            ),
        }

    async def _set_config(self, params: dict[str, Any]) -> dict[str, Any]:
        values = params.get("values", {})
        if not isinstance(values, dict) or not values:
            raise ValueError("system.set_config requires a non-empty values object")

        from server import presentation_runtime
        from server import visual_runtime
        from server import wallpaper_subtitle_runtime
        import tts.pipeline as tts_pipeline

        allowed = {
            "llm_provider",
            "local_llm_type",
            "tts_mode",
            "tts_output_language",
            "asr_backend",
            "vision_enabled",
            "vision_mode",
            "vision_scope",
            "vision_provider",
            "vision_max_long_side",
            "vision_jpeg_quality",
            "vision_region",
            "vision_window_handle",
            "presentation_locale",
            "wallpaper_caption_mode",
            "wallpaper_subtitle_language",
        }
        unknown = sorted(str(key) for key in values if str(key) not in allowed)
        if unknown:
            raise ValueError(f"unsupported runtime setting(s): {', '.join(unknown)}")
        values = {str(key): value for key, value in values.items()}

        if "llm_provider" in values:
            provider = str(values["llm_provider"] or "").strip().lower()
            if provider not in {
                "deepseek", "openai", "gemini", "bedrock", "local",
                "hybrid", "hybrid2", "hybrid3",
            }:
                raise ValueError(f"unsupported LLM provider: {provider!r}")
        if "local_llm_type" in values:
            local_type = str(values["local_llm_type"] or "").strip().lower()
            if local_type not in {"llama_server", "lmstudio", "ollama", "cli"}:
                raise ValueError(f"unsupported local LLM type: {local_type!r}")
        if "tts_mode" in values:
            mode = str(values["tts_mode"] or "").strip().lower()
            if mode not in {"cuda_graph", "parallel", "cuda graph ×1", "parallel ×2", "graph"}:
                raise ValueError(f"unsupported TTS mode: {values['tts_mode']!r}")
        if "tts_output_language" in values:
            language = str(values["tts_output_language"] or "").strip().lower()
            if language not in {"ja", "jp", "japanese", "日文", "en", "english", "英文"}:
                raise ValueError(f"unsupported TTS language: {values['tts_output_language']!r}")
        if "asr_backend" in values:
            from asr.registry import asr_backend_ids

            backend = str(values["asr_backend"] or "").strip().lower()
            if backend not in set(asr_backend_ids()):
                raise ValueError(f"unsupported ASR backend: {backend!r}")
        if "vision_enabled" in values and not isinstance(values["vision_enabled"], bool):
            raise ValueError("vision_enabled must be a boolean")
        if "vision_mode" in values:
            mode = str(values["vision_mode"] or "").strip().lower()
            if mode not in {"off", "on_demand", "watching", "self_aware"}:
                raise ValueError(f"unsupported vision mode: {mode!r}")
            values["vision_mode"] = mode
        if "vision_scope" in values:
            scope = str(values["vision_scope"] or "").strip().lower()
            if scope not in {
                "full_screen", "current_window", "selected_window",
                "wallpaper_surface", "region",
            }:
                raise ValueError(f"unsupported vision scope: {scope!r}")
            values["vision_scope"] = scope
        if "vision_max_long_side" in values:
            try:
                max_long_side = int(values["vision_max_long_side"])
            except (TypeError, ValueError) as exc:
                raise ValueError("vision_max_long_side must be an integer") from exc
            if not 320 <= max_long_side <= 4096:
                raise ValueError("vision_max_long_side must be between 320 and 4096")
            values["vision_max_long_side"] = max_long_side
        if "vision_jpeg_quality" in values:
            try:
                jpeg_quality = int(values["vision_jpeg_quality"])
            except (TypeError, ValueError) as exc:
                raise ValueError("vision_jpeg_quality must be an integer") from exc
            if not 35 <= jpeg_quality <= 92:
                raise ValueError("vision_jpeg_quality must be between 35 and 92")
            values["vision_jpeg_quality"] = jpeg_quality
        if "presentation_locale" in values:
            locale = str(values["presentation_locale"] or "").strip()
            if locale not in presentation_runtime.VALID_PRESENTATION_LOCALES:
                raise ValueError(f"unsupported presentation locale: {locale!r}")
        if "wallpaper_caption_mode" in values:
            caption_mode = str(values["wallpaper_caption_mode"] or "").strip().lower()
            if caption_mode not in presentation_runtime.VALID_CAPTION_MODES:
                raise ValueError(f"unsupported wallpaper caption mode: {caption_mode!r}")
            values["wallpaper_caption_mode"] = caption_mode

        if {"llm_provider", "local_llm_type"}.intersection(values):
            if self._is_chat_busy is not None and self._is_chat_busy():
                raise RuntimeError("wait for the active chat turn before changing LLM routing")
        if {"tts_mode", "tts_output_language"}.intersection(values):
            if self._is_chat_busy is not None and self._is_chat_busy():
                raise RuntimeError("wait for the active chat turn before changing TTS settings")
            playback = self._playback_manager
            if playback is not None:
                ready = getattr(playback, "player_is_ready", None)
                playing = bool(ready is not None and not ready.is_set())
                pending = bool(getattr(playback, "pending_audio", {}) or {})
                if playing or pending:
                    raise RuntimeError("wait for TTS playback to become idle before changing TTS settings")

        updated: list[str] = []
        if "asr_backend" in values:
            if self._asr_handler is None:
                raise RuntimeError("ASR runtime is unavailable")
            await self._asr_handler.set_backend(values["asr_backend"])
            updated.append("asr_backend")
        if "tts_mode" in values:
            tts_pipeline.reconfigure_tts_mode_name(str(values["tts_mode"]))
            updated.append("tts_mode")
        if "tts_output_language" in values:
            tts_pipeline.reconfigure_tts_language_code(str(values["tts_output_language"]))
            updated.append("tts_output_language")
        if "llm_provider" in values:
            import llm.client as llm_client
            from core.chat_runtime import get_chat_runtime

            provider = str(values["llm_provider"]).strip().lower()
            get_chat_runtime().set_provider(provider)
            llm_client.configure(llm_provider=provider)
            updated.append("llm_provider")
        if "local_llm_type" in values:
            from core.chat_runtime import get_chat_runtime

            get_chat_runtime().set_local_llm_type(str(values["local_llm_type"]))
            updated.append("local_llm_type")
        if {"llm_provider", "local_llm_type"}.intersection(values):
            from config import settings
            from llm.local_backends import should_manage_local_server
            from llm.llama_server import (
                start_llama_server,
                stop_llama_server,
                warmup_local_llm_cache,
            )

            if should_manage_local_server(settings):
                await start_llama_server()
                asyncio.create_task(warmup_local_llm_cache())
            else:
                await asyncio.to_thread(stop_llama_server)

        visual_updated = visual_runtime.set_config(values)
        presentation_updated = presentation_runtime.set_config(values)
        if presentation_updated:
            wallpaper_subtitle_runtime.refresh()
        updated = list(dict.fromkeys([*updated, *visual_updated, *presentation_updated]))
        current = await self._get_config({})
        await bus.emit(Method.SYSTEM_CONFIG, {"values": current, "updated": updated})
        return {"updated": updated, "values": current}

    async def _list_windows(self, params: dict[str, Any]) -> dict[str, Any]:
        from server import visual_runtime

        try:
            limit = int(params.get("limit", 40))
        except (TypeError, ValueError):
            limit = 40
        return {"windows": visual_runtime.list_capture_windows(limit=max(1, min(limit, 120)))}

    async def _get_log(self, params: dict[str, Any]) -> dict[str, Any]:
        lines = params.get("lines", 50)
        log = self._log_path
        if not log or not log.exists():
            return {"lines": [], "total": 0}
        try:
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except UnicodeDecodeError:
            # Windows system default may write in gbk
            with open(log, "r", encoding="gbk", errors="replace") as f:
                all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"lines": [l.rstrip("\n") for l in tail], "total": len(all_lines)}

    async def emit_status(self) -> None:
        """Periodic status push — call from a background task."""
        from server import visual_runtime

        asr_manager = self._asr_manager_getter() if self._asr_manager_getter else self._asr_manager
        await bus.emit(Method.SYSTEM_STATUS, {
            "vts_connected": self._vts_manager.connected if self._vts_manager else False,
            "tts_ready": self._playback_manager is not None,
            "asr_ready": asr_manager is not None,
            "vision": visual_runtime.get_config(),
        })
