"""Adapter for system config, status, and lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from collections.abc import Callable
from typing import Any

from config.catalog import configuration_groups, voice_backend_groups
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


def _catalog_configuration(
    group_id: str, settings: Any, *, options: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    group = configuration_groups()[group_id]
    fields = []
    for key, definition in group["config"].items():
        secret = definition.get("secret", False)
        value = getattr(settings, key)
        field = _startup_field(
            key, definition["title"]["en-US"], "" if secret else value,
            field_type="secret" if secret else {
                "string": "text", "path": "path", "url": "url", "enum": "select",
                "boolean": "boolean", "integer": "number", "number": "number",
            }[definition["type"]],
            options=tuple({"value": option, "label": option} if isinstance(option, str) else {
                "value": option["value"], "label": option["label"]["en-US"],
            } for option in definition.get("options", ())),
            description=definition.get("description", {}).get("en-US", ""),
            minimum=definition.get("min"), maximum=definition.get("max"), step=definition.get("step"),
            secret_configured=bool(value) if secret else None,
            editable=group["desktop"],
        )
        field["restart_required"] = group["restart_required"]
        if options and key in options:
            field.update(type="select", options=options[key])
        fields.append(field)
    return {
        "id": group["id"], "label": group["title"]["en-US"],
        "description": group["description"]["en-US"], "fields": fields,
    }


def _voice_backend_configuration(group: dict[str, Any], settings: Any, statuses: list[dict[str, Any]], selected: str) -> dict[str, Any]:
    backend_id = group["voice_backend"]["id"]
    status = next((item for item in statuses if item["id"] == backend_id), {})
    return {
        **_catalog_configuration(group["id"], settings),
        "active": selected == backend_id,
        "configured": bool(status.get("available")),
        "status": str(status.get("state") or "unavailable"),
        "status_ok": bool(status.get("available")),
        "status_detail": str(status.get("detail") or ""),
    }


def _voice_configuration(settings: Any, emotion_pack: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    from asr.registry import asr_backend_statuses
    from tts.registry import tts_backend_statuses
    from config.asset_packages import external_asset_pack_status
    from tts.reference_pack import PACK_ID
    import tts.pipeline as tts_pipeline

    if emotion_pack is None:
        emotion_pack = external_asset_pack_status(PACK_ID)
    emotion_runtime = tts_pipeline.emotion_reference_status()
    emotion_requested = bool(settings.ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING)

    asr_selected = str(settings.ASR_BACKEND or "qwen3_asr").strip().lower()
    tts_selected = str(settings.TTS_BACKEND or "gpt_sovits").strip().lower()
    asr_statuses = asr_backend_statuses(asr_selected)
    tts_statuses = tts_backend_statuses(tts_selected)
    asr_status = next((item for item in asr_statuses if item["selected"]), {})
    tts_status = next((item for item in tts_statuses if item["selected"]), {})


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
            **_catalog_configuration("conversation_asr", settings, options={"ASR_BACKEND": [{"value": item["id"], "label": item["label"]} for item in asr_statuses], "MICROPHONE_DEVICE_INDEX": microphone_options}),

            "active": True,
            "configured": bool(asr_status.get("available")),
            "status": str(asr_status.get("state") or "unavailable"),
            "status_ok": bool(asr_status.get("available")),
            "status_detail": str(asr_status.get("detail") or ""),
        },
        {
            **_catalog_configuration("asr_remote", settings),

            "active": asr_selected == "openai_compatible",
            "configured": bool(settings.ASR_API_BASE_URL and settings.ASR_API_MODEL),
            "status": "remote" if asr_selected == "openai_compatible" else "available",
            "status_ok": bool(settings.ASR_API_BASE_URL and settings.ASR_API_MODEL),
        },
        {
            **_catalog_configuration("wake_asr", settings),

            "active": bool(settings.WAKE_ENABLED),
            "configured": not bool(settings.WAKE_ENABLED) or bool(wake_status.get("available")),
            "status": "disabled" if not settings.WAKE_ENABLED else str(wake_status.get("state") or "unavailable"),
            "status_ok": not bool(settings.WAKE_ENABLED) or bool(wake_status.get("available")),
            "status_detail": str(wake_status.get("detail") or ""),
        },
        {
            **_catalog_configuration("acoustic_pipeline", settings),

            "active": bool(settings.AEC_REALTIME_ENABLED),
            "configured": True,
            "status": "available",
            "status_ok": True,
        },
        {
            **_catalog_configuration("voice_reference_profile", settings),

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
        },
        {
            **_catalog_configuration("speech_synthesis", settings, options={
                "TTS_BACKEND": [{"value": item["id"], "label": item["label"]} for item in tts_statuses],
            }),
            "active": tts_selected != "disabled",
            "configured": bool(tts_status.get("available")),
            "status": str(tts_status.get("state") or "unavailable"),
            "status_ok": bool(tts_status.get("available")),
            "status_detail": str(tts_status.get("detail") or ""),
        },
        *(_voice_backend_configuration(group, settings, tts_statuses, tts_selected)
              for group in voice_backend_groups()),
        {
            **_catalog_configuration("tts_emotion_references", settings),

            "active": tts_selected == "gpt_sovits" and emotion_requested,
            "configured": not emotion_requested or bool(emotion_pack["installed"]),
            "status": str(emotion_runtime["state"]),
            "status_ok": not emotion_requested or bool(emotion_runtime["ready"]),
            "status_detail": str(emotion_runtime["detail"]),
        },

    ]


def _artifact_configuration(settings: Any) -> list[dict[str, Any]]:
    return [{
        "id": "auip_artifact_style",
        "label": "Artifact appearance",
        "configured": True,
        "status_ok": True,
        "status": "enabled" if settings.AUIP_ARTIFACT_STYLE_ENABLED else "disabled",
        "description": "A shared visual language for newly authored AUIP apps; each app keeps its own content and layout.",
        "fields": [_startup_field(
            "AUIP_ARTIFACT_STYLE_ENABLED", "Use Amadeus style",
            settings.AUIP_ARTIFACT_STYLE_ENABLED, field_type="boolean",
            description="After a backend restart, provide the style guide and CSS for future AUIP creation. Existing apps keep their design; explicit user design requests take priority.",
        )],
    }]


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
    rag_status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    active = str(active_provider or "deepseek").strip().lower()
    from core.character_rag import CharacterRAG

    rag_status = rag_status if rag_status is not None else CharacterRAG().status()
    rag_detail = (
        f"{rag_status['detail']} Directory: {rag_status['index_dir']}. "
        f"Applied threshold: {rag_status['max_distance']}; top-k: {rag_status['top_k']}."
    )
    if rag_status.get("model"):
        rag_detail += f" Model: {rag_status['model']}; entries: {rag_status['entries']}."
    last_retrieval = rag_status.get("last_retrieval")
    if last_retrieval:
        rag_detail += (
            f" Last retrieval: {last_retrieval['matched_count']} matches; "
            f"nearest distance: {last_retrieval['nearest_distance']}; "
            f"reference selected: {last_retrieval['reference_selected']}."
        )
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
            "id": "character_rag",
            "label": "Character knowledge (optional RAG)",
            "description": "Local retrieval for all chat models. Build an index first; retrieved excerpts are sent to the selected model, including remote APIs. Restart after changes.",
            "active": bool(settings.RAG_ENABLED),
            "configured": bool(rag_status["index_present"]),
            "status": rag_status["state"],
            "status_ok": rag_status["state"] in {"ready", "disabled"},
            "status_detail": rag_detail,
            "fields": [
                _startup_field("RAG_ENABLED", "Enable character knowledge", bool(settings.RAG_ENABLED), field_type="boolean"),
                _startup_field("RAG_INDEX_DIR", "Built index directory", settings.RAG_INDEX_DIR, field_type="path"),
                _startup_field("RAG_TOP_K", "Maximum results", settings.RAG_TOP_K, field_type="number", minimum=1, maximum=20, step=1),
                _startup_field("RAG_MAX_DISTANCE", "Maximum squared L2 distance", settings.RAG_MAX_DISTANCE, field_type="number", minimum=0, maximum=4, step=0.01),
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
            "description": "Shared fast first-sentence endpoint. Hybrid pairs it with Bedrock, Hybrid2 with DeepSeek, and Hybrid3 with OpenAI-compatible. The optional Hybrid BAT launcher shares the llama.cpp executable and GGUF settings above.",
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
    import os

    from server.auip_b2 import b2_runtime_unavailable_reason
    from server.auip_b2_role_llm import has_b2_role_model_config

    b2_unavailable = b2_runtime_unavailable_reason(
        role_branch_mode=getattr(settings, "AUIP_APPSESSION_ROLE_BRANCH_MODE", "b2"),
        control_decision_available=bool(
            getattr(settings, "AUIP_CONTROL_DECISION_ENABLED", False)
        ),
        role_model_available=has_b2_role_model_config(),
    )
    b2_active = str(
        getattr(settings, "AUIP_APPSESSION_ROLE_BRANCH_MODE", "b2") or ""
    ).strip().lower() == "b2"
    if b2_unavailable == "b2_control_decision_unavailable":
        b2_status_detail = (
            "B2 is selected, but its source-local AUIP decision lane is disabled. "
            "Application actions remain blocked; Chat and Settings stay available."
        )
    elif b2_unavailable == "b2_role_model_unavailable":
        b2_status_detail = (
            "B2 is selected, but no supported OpenAI or DeepSeek action model credential "
            "is configured. Application actions remain blocked until setup and restart."
        )
    elif b2_active:
        b2_status_detail = "B2 application action decisions are available."
    else:
        b2_status_detail = "B2 is not selected; this action role is optional."

    vn_provider_override = os.environ.get("VN_LLM_PROVIDER", "").strip().lower()
    vn_provider = vn_provider_override or "deepseek"
    vn_model_override = os.environ.get("VN_LLM_MODEL", "").strip()
    vn_configured = bool(
        settings.OPENAI_API_KEY if vn_provider == "openai" else settings.DEEPSEEK_API_KEY
    )

    return [
        {
            "id": "vn_companion",
            "label": "VN companion",
            "description": "Dedicated VN reasoning and reaction role. DeepSeek is the recommended default; OpenAI-compatible is also supported.",
            "active": True,
            "configured": vn_configured,
            "status": "needs_setup" if not vn_configured else "override" if vn_provider_override or vn_model_override else "recommended",
            "status_ok": vn_configured,
            "fields": [
                _startup_field(
                    "VN_LLM_PROVIDER", "Model connection",
                    vn_provider,
                    field_type="select",
                    options=(
                        {"value": "deepseek", "label": "DeepSeek · Recommended"},
                        {"value": "openai", "label": "OpenAI-compatible"},
                    ),
                ),
                _startup_field(
                    "VN_LLM_MODEL", "Model override",
                    vn_model_override,
                    description="Optional. Leave blank to use the model from the selected connection.",
                ),
            ],
        },
        {
            "id": "work_planner",
            "label": "Work planner / router",
            "description": "Plans and routes cooperative Work; an empty model inherits the main conversation model on the existing backend.",
            "active": bool(
                getattr(settings, "COOPERATIVE_WORK_PLANNER_ENABLED", False)
            ),
            "configured": True,
            "status": "override" if settings.COOPERATIVE_WORK_PLANNER_MODEL else "inherited",
            "status_ok": True,
            "fields": [
                _startup_field(
                    "COOPERATIVE_WORK_PLANNER_MODEL", "Model override",
                    settings.COOPERATIVE_WORK_PLANNER_MODEL,
                    description="Leave empty to inherit the main conversation model.",
                ),
            ],
        },
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
            "id": "browser_branch_planner",
            "label": "Browser branch planner",
            "description": "Chooses bounded browser branches; inherits a supported main provider and its model when left blank.",
            "configured": True,
            "status": "override" if os.environ.get("BROWSER_BRANCH_PROVIDER") or os.environ.get("BROWSER_BRANCH_MODEL") else "inherited",
            "status_ok": True,
            "fields": [
                _startup_field(
                    "BROWSER_BRANCH_PROVIDER", "Provider override",
                    os.environ.get("BROWSER_BRANCH_PROVIDER", ""),
                    field_type="select",
                    options=(
                        {"value": "", "label": "Inherit supported main provider"},
                        {"value": "deepseek", "label": "DeepSeek"},
                        {"value": "openai", "label": "OpenAI-compatible"},
                    ),
                ),
                _startup_field(
                    "BROWSER_BRANCH_MODEL", "Model override",
                    os.environ.get("BROWSER_BRANCH_MODEL", ""),
                    description="Leave empty to use the selected provider's configured model.",
                ),
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
            "description": "Decision-quality model used by the default B2 AppSession action path.",
            "active": b2_active,
            "configured": not bool(b2_unavailable),
            "status": "needs_setup" if b2_unavailable else "available" if b2_active else "optional",
            "status_ok": not bool(b2_unavailable),
            "status_detail": b2_status_detail,
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
        {
            "id": "vn_subtitle_translation",
            "label": "VN subtitle translation",
            "description": "Translates Japanese game dialogue into Simplified Chinese for display.",
            "configured": True,
            "status": "override" if os.environ.get("VN_SUBTITLE_TRANSLATE_PROVIDER") or os.environ.get("VN_SUBTITLE_TRANSLATE_MODEL") else "inherited",
            "status_ok": True,
            "fields": [
                _startup_field(
                    "VN_SUBTITLE_TRANSLATE_PROVIDER", "Provider override",
                    os.environ.get("VN_SUBTITLE_TRANSLATE_PROVIDER", ""),
                    field_type="select",
                    options=(
                        {"value": "", "label": "DeepSeek default"},
                        {"value": "deepseek", "label": "DeepSeek"},
                        {"value": "openai", "label": "OpenAI-compatible"},
                    ),
                ),
                _startup_field(
                    "VN_SUBTITLE_TRANSLATE_MODEL", "Model override",
                    os.environ.get("VN_SUBTITLE_TRANSLATE_MODEL", ""),
                    description="Leave empty to use the selected provider's configured model.",
                ),
            ],
        },
        {
            "id": "vn_speech_translation",
            "label": "VN speech translation",
            "description": "Translates Chinese companion reactions into Japanese before speech synthesis.",
            "configured": True,
            "status": "override" if os.environ.get("VN_TTS_TRANSLATE_PROVIDER") or os.environ.get("VN_TTS_TRANSLATE_MODEL") else "inherited",
            "status_ok": True,
            "fields": [
                _startup_field(
                    "VN_TTS_TRANSLATE_PROVIDER", "Provider override",
                    os.environ.get("VN_TTS_TRANSLATE_PROVIDER", ""),
                    field_type="select",
                    options=(
                        {"value": "", "label": "DeepSeek default"},
                        {"value": "deepseek", "label": "DeepSeek"},
                        {"value": "openai", "label": "OpenAI-compatible"},
                    ),
                ),
                _startup_field(
                    "VN_TTS_TRANSLATE_MODEL", "Model override",
                    os.environ.get("VN_TTS_TRANSLATE_MODEL", ""),
                    description="Leave empty to use the selected provider's configured model.",
                ),
            ],
        },
    ]


def _acp_credentials() -> list[dict[str, Any]]:
    import os

    return [
        _startup_field("ANTHROPIC_API_KEY", "Anthropic API key", field_type="secret",
                       secret_configured=bool(os.environ.get("ANTHROPIC_API_KEY"))),
        _startup_field("DEEPSEEK_API_KEY", "DeepSeek API key", field_type="secret",
                       secret_configured=bool(os.environ.get("DEEPSEEK_API_KEY"))),
    ]


def _work_provider_configuration(settings: Any) -> list[dict[str, Any]]:
    codex_transport = (
        "app_server" if settings.CODEX_APP_SERVER_PROVIDER_ENABLED
        else "direct" if settings.DIRECT_CODEX_PROVIDER_ENABLED
        else "disabled"
    )
    codex_auth_mode = str(
        getattr(settings, "CODEX_APP_SERVER_AUTH_MODE", "model_api") or "model_api"
    ).strip().lower()
    codex_model_provider = str(
        getattr(settings, "CODEX_APP_SERVER_MODEL_PROVIDER", "deepseek") or "deepseek"
    ).strip().lower()
    codex_connection_options = []
    if bool(getattr(settings, "DEEPSEEK_API_KEY", "")) or codex_model_provider == "deepseek":
        codex_connection_options.append({
            "value": "deepseek",
            "label": "DeepSeek" if getattr(settings, "DEEPSEEK_API_KEY", "") else "DeepSeek · Not configured",
        })
    if bool(getattr(settings, "OPENAI_API_KEY", "")) or codex_model_provider == "openai":
        codex_connection_options.append({
            "value": "openai",
            "label": "OpenAI-compatible" if getattr(settings, "OPENAI_API_KEY", "") else "OpenAI-compatible · Not configured",
        })
    codex_fields = [
        _startup_field(
            "CODEX_PROVIDER_TRANSPORT", "Transport", codex_transport,
            field_type="select", options=("app_server", "direct", "disabled"),
        ),
    ]
    if codex_transport == "app_server":
        codex_fields.extend([
            _startup_field(
            "CODEX_APP_SERVER_CODEX_BIN", "App Server executable",
            settings.CODEX_APP_SERVER_CODEX_BIN, field_type="path",
            ),
            _startup_field(
            "CODEX_APP_SERVER_AUTH_MODE", "App Server authentication",
            codex_auth_mode, field_type="select", options=(
                {"value": "chatgpt", "label": "ChatGPT subscription"},
                {"value": "model_api", "label": "Model API connection"},
            ),
            description="Run `codex login` once for subscription use. Model API reuses a connection from Models.",
            ),
        ])
        if codex_auth_mode == "chatgpt":
            codex_fields.append(_startup_field(
                "CODEX_APP_SERVER_CHATGPT_MODEL", "Subscription model override",
                settings.CODEX_APP_SERVER_CHATGPT_MODEL,
                description="Optional. Leave blank to use the model selected by the signed-in Codex client.",
            ))
        else:
            codex_fields.extend([
            _startup_field(
                "CODEX_APP_SERVER_MODEL_PROVIDER", "Model API connection",
                settings.CODEX_APP_SERVER_MODEL_PROVIDER,
                field_type="select", options=tuple(codex_connection_options),
                description="Reuses the API key and endpoint configured in Models.",
            ),
            _startup_field(
                "CODEX_APP_SERVER_MODEL", "Model", settings.CODEX_APP_SERVER_MODEL,
                description="Defaults to the model from the selected Models connection.",
            ),
            ])
        codex_fields.extend([
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
        ])
    elif codex_transport == "direct":
        codex_fields.append(_startup_field(
            "DIRECT_CODEX_CLI_PATH", "Direct CLI executable",
            settings.DIRECT_CODEX_CLI_PATH, field_type="path",
            description="Direct CLI uses the existing local `codex login` session.",
        ))
    return [
        {
            "id": "pi", "label": "Pi",
            "description": "Default daily agent using the desktop-installed native RPC runtime and shared Models credentials; Work role assignment is independent.",
            "fields": [
                _startup_field("PI_PROVIDER_ENABLED", "Enable Pi", settings.PI_PROVIDER_ENABLED, field_type="boolean"),
                _startup_field("PI_NODE_PATH", "Node executable", settings.PI_NODE_PATH, field_type="path"),
                _startup_field("PI_AGENT_DIR", "Pi configuration and sessions", settings.PI_AGENT_DIR, field_type="path"),
                _startup_field("PI_MODEL_PROVIDER", "Pi model provider", settings.PI_MODEL_PROVIDER),
                _startup_field("PI_MODEL", "Pi model", settings.PI_MODEL),
            ],
        },
        {
            "id": "browser",
            "label": "Browser",
            "description": "Host-managed browser work Provider; no connection settings.",
            "fields": [],
        },
        {
            "id": "openclaw",
            "label": "OpenClaw",
            "description": "Optional Gateway provider; Work role assignment is independent. Existing sessions remain supported.",
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
            "fields": codex_fields,
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
        from llm.visual_context import provider_supports_direct_image
        from server import visual_runtime
        from server import presentation_runtime
        from server import chat_translation_runtime
        from render.character_pack import character_pack_status
        from config.asset_packages import external_asset_pack_status
        from tts.reference_pack import PACK_ID
        import tts.pipeline as tts_pipeline
        from core.character_rag import get_character_rag
        from llm.prompts import get_character_prompt_config

        vision = visual_runtime.get_config()
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
        emotion_pack = await asyncio.to_thread(external_asset_pack_status, PACK_ID)
        voice_configuration, local_status, hybrid_status = await asyncio.gather(
            asyncio.to_thread(_voice_configuration, settings, emotion_pack),
            asyncio.to_thread(local_backend_status, settings, project_root=project_root),
            asyncio.to_thread(hybrid_local_status, settings),
        )
        from server.runtime_status import status_collector

        head = status_collector.hybrid_head()
        outcome_detail = {
            "presented": "Local first sentence delivered.",
            "empty": "Local head returned no visible text; remote reply continued.",
            "skipped_remote_ready": "Remote reply was ready first; unused local head cancelled.",
            "interrupted": "Local head interrupted with its turn.",
        }.get(head["outcome"])
        if outcome_detail:
            hybrid_status["detail"] += " Last head in this conversation: " + outcome_detail
        return {
            "vts_ws_url": getattr(settings, 'VTS_WS_URL', ''),
            "chat_supports_images": provider_supports_direct_image(
                active_provider, llm_client.DEEPSEEK_MODEL_NAME,
            ),
            "llm_provider": active_provider,
            "hybrid_head": head,
            "tts_device": getattr(settings, 'TTS_DEVICE', ''),
            "tts_mode": tts_pipeline.current_tts_mode(),
            "tts_output_language": tts_pipeline.current_tts_language_code(),
            "tts_backend": getattr(settings, "TTS_BACKEND", "gpt_sovits"),
            "asr_backend": asr_backend,
            "asr_language": getattr(settings, "ASR_LANGUAGE", "auto"),
            "asr_context": getattr(settings, "ASR_CONTEXT", ""),
            "local_llm_type": llm_client.LOCAL_LLM_TYPE,
            "aec_realtime_enabled": bool(getattr(settings, "AEC_REALTIME_ENABLED", False)),
            "aec_realtime_barge_in": bool(getattr(settings, "AEC_REALTIME_BARGE_IN", False)),
            "aec_realtime_delay_ms": float(getattr(settings, "AEC_REALTIME_DELAY_MS", 280.0)),
            "wake_enabled": bool(getattr(settings, "WAKE_ENABLED", False)),
            "visual_asset_pack": external_asset_pack_status("visual-runtime"),
            "character_pack": character_pack_status(),
            "emotion_reference_pack": emotion_pack,
            **get_character_prompt_config(),
            "settings_scope": "runtime_only",
            "model_connections": _model_connections(
                settings,
                active_provider,
                local_status=local_status,
                hybrid_status=hybrid_status,
                rag_status=get_character_rag().status(),
            ),
            "model_roles": _model_role_configuration(settings),
            "work_provider_configuration": _work_provider_configuration(settings),
            "graphics": {
                "profile": settings.GRAPHICS_PROFILE,
                "custom_max_fps": settings.RENDER_MAX_FPS,
                "custom_max_resolution": settings.RENDER_MAX_RESOLUTION,
                "texture_sampling": settings.RENDER_TEXTURE_SAMPLING,
                "bc7_cache": settings.RENDER_BC7_CACHE,
                "effective_max_fps": settings.RENDER_EFFECTIVE_MAX_FPS,
                "effective_max_resolution": settings.RENDER_EFFECTIVE_MAX_RESOLUTION,
            },
            "acp_credentials": _acp_credentials(),
            "artifact_configuration": _artifact_configuration(settings),
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
            **chat_translation_runtime.get_config(),
            "control_decision_mode": "retired",
            "retired_settings": settings.retired_settings(),
            "cooperative_chat_enabled": True,
            "cooperative_work_planner_enabled": bool(
                getattr(settings, "COOPERATIVE_WORK_PLANNER_ENABLED", False)
            ),
            "cooperative_work_planner_model": str(
                getattr(settings, "COOPERATIVE_WORK_PLANNER_MODEL", "") or ""
            ),
            "cooperative_chat_input_capabilities": {
                "typed_text": True,
                "confirmed_transcript_text": True,
                "visual_attachment": True,
                "speculative_voice": False,
                "physical_voice_validated": False,
            },
            "cooperative_chat_provider": str(
                getattr(settings, "COOPERATIVE_CHAT_PROVIDER", "") or ""
            ),
            "work_coding_provider": settings.WORK_CODING_PROVIDER,
            "work_execution_provider": settings.WORK_EXECUTION_PROVIDER,
            "cooperative_permission_policy": str(
                getattr(settings, "COOPERATIVE_CHAT_PERMISSION_POLICY", "") or ""
            ),
        }

    async def _set_config(self, params: dict[str, Any]) -> dict[str, Any]:
        values = params.get("values", {})
        if not isinstance(values, dict) or not values:
            raise ValueError("system.set_config requires a non-empty values object")

        from config import settings
        from server import presentation_runtime
        from server import chat_translation_runtime
        from server import visual_runtime
        from server import wallpaper_subtitle_runtime
        import tts.pipeline as tts_pipeline

        from llm.prompts import CHARACTER_PROMPT_SETTING, normalize_character_prompt, set_character_prompt

        allowed = {
            CHARACTER_PROMPT_SETTING,
            "llm_provider",
            "local_llm_type",
            "tts_mode",
            "tts_output_language",
            "asr_backend",
            "vision_enabled",
            "vision_mode",
            "vision_scope",
            "vision_max_long_side",
            "vision_jpeg_quality",
            "vision_region",
            "vision_window_handle",
            "presentation_locale",
            "wallpaper_caption_mode",
            "wallpaper_subtitle_language",
            "chat_translation_subtitles_enabled",
        }
        unknown = sorted(str(key) for key in values if str(key) not in allowed)
        if unknown:
            raise ValueError(f"unsupported runtime setting(s): {', '.join(unknown)}")
        values = {str(key): value for key, value in values.items()}

        if CHARACTER_PROMPT_SETTING in values:
            values[CHARACTER_PROMPT_SETTING] = normalize_character_prompt(values[CHARACTER_PROMPT_SETTING])

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
            if mode not in {"cuda_graph", "parallel", "parallel2", "cuda graph ×1", "parallel ×2", "graph"}:
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
        if (
            "chat_translation_subtitles_enabled" in values
            and not isinstance(values["chat_translation_subtitles_enabled"], bool)
        ):
            raise ValueError("chat_translation_subtitles_enabled must be a boolean")

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
        if CHARACTER_PROMPT_SETTING in values:
            updated.extend(set_character_prompt(values[CHARACTER_PROMPT_SETTING]))
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

            provider = str(values["llm_provider"]).strip().lower()
            llm_client.configure(llm_provider=provider)
            updated.append("llm_provider")
        if "local_llm_type" in values:
            import llm.client as llm_client

            llm_client.configure(local_llm_type=str(values["local_llm_type"]))
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
        chat_translation_updated = chat_translation_runtime.set_config(values)
        if presentation_updated:
            wallpaper_subtitle_runtime.refresh()
        updated = list(dict.fromkeys([
            *updated,
            *visual_updated,
            *presentation_updated,
            *chat_translation_updated,
        ]))
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
