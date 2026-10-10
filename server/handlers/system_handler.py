"""Adapter for system config, status, and lifecycle."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from collections.abc import Callable
from typing import Any

from config.catalog import application_policy, configuration_groups, option_values, read_catalog_value, runtime_fields, voice_backend_groups
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


def _catalog_field(key: str, settings: Any, values: dict[str, Any] | None = None) -> dict[str, Any]:
    group = next(group for group in configuration_groups().values() if key in group["config"])
    definition = group["config"][key]
    secret = definition.get("secret", False)
    if values is not None and key in values:
        value = values[key]
    elif definition.get("scope") == "session":
        import os
        from config.environment import EnvironmentReader
        value = read_catalog_value(EnvironmentReader(os.environ), key)
    else:
        value = getattr(settings, definition.get("setting", key))
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
    field["apply"] = application_policy(key)
    field["restart_required"] = field["apply"] == "backend_restart"
    if "true_values" in definition:
        field["true_values"] = definition["true_values"]
    if definition.get("control"):
        field["type"] = definition["control"]
    return field


def _catalog_configuration(
    group_id: str, settings: Any, *, options: dict[str, list[dict[str, str]]] | None = None,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    group = configuration_groups()[group_id]
    fields = [_catalog_field(key, settings, values) for key in group["config"]]
    controls = {field["key"]: field.get("value") for field in fields}
    fields = [field for field in fields if all(
        controls.get(selector) is None or str(controls[selector]) in choices
        for selector, choices in group["config"][field["key"]].get("visible_when", {}).items()
    )]
    for field in fields:
        if options and field["key"] in options:
            field.update(type="select", options=options[field["key"]])
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
    return [{**_catalog_configuration("auip_artifact_style", settings),
             "configured": True, "status_ok": True,
             "status": "enabled" if settings.AUIP_ARTIFACT_STYLE_ENABLED else "disabled"}]


def _avatar_configuration(settings: Any) -> list[dict[str, Any]]:
    enabled = bool(settings.VTS_ENABLED)
    configured = not enabled or bool(str(settings.VTS_WS_URL or "").strip())
    return [{**_catalog_configuration("vts_compatibility", settings),
             "active": enabled, "configured": configured,
             "status": "available" if enabled else "disabled", "status_ok": configured}]


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
    local_fields = _catalog_configuration("local", settings)["fields"]

    local_status = dict(local_status or {})
    hybrid_status = dict(hybrid_status or {})
    return [
        {
            **_catalog_configuration("profile", settings),
            "active": True,
            "configured": True,
        },
        {
            **_catalog_configuration("character_rag", settings),


            "active": bool(settings.RAG_ENABLED),
            "configured": bool(rag_status["index_present"]),
            "status": rag_status["state"],
            "status_ok": rag_status["state"] in {"ready", "disabled"},
            "status_detail": rag_detail,

        },
        {
            **_catalog_configuration("deepseek", settings),

            "active": "deepseek" in active_connections,
            "configured": bool(settings.DEEPSEEK_API_KEY),

        },
        {
            **_catalog_configuration("openai", settings),

            "active": "openai" in active_connections,
            "configured": bool(settings.OPENAI_API_KEY),

        },
        {
            **_catalog_configuration("gemini", settings),

            "active": "gemini" in active_connections,
            "configured": bool(settings.GEMINI_API_KEY),

        },
        {
            **_catalog_configuration("bedrock", settings),

            "active": "bedrock" in active_connections,
            "configured": bool(
                settings.AWS_BEDROCK_BEARER_TOKEN
                or settings.AWS_BEDROCK_AUTH_MODE in {"auto", "boto3"}
            ),

        },
        {
            **_catalog_configuration("local", settings),


            "active": "local" in active_connections,
            "configured": bool(local_status.get("configured")),
            "status": str(local_status.get("state") or "unavailable"),
            "status_ok": bool(local_status.get("available")),
            "status_detail": str(local_status.get("detail") or "Status is checked at startup."),
            "fields": local_fields,
        },
        {
            **_catalog_configuration("hybrid_local", settings),


            "active": "hybrid_local" in active_connections,
            "configured": bool(hybrid_status.get("configured")),
            "status": str(hybrid_status.get("state") or "unavailable"),
            "status_ok": bool(hybrid_status.get("available")),
            "status_detail": str(hybrid_status.get("detail") or "Status is checked at startup."),

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
            **_catalog_configuration("vn_companion", settings, values={"VN_LLM_PROVIDER": vn_provider, "VN_LLM_MODEL": vn_model_override}),


            "active": True,
            "configured": vn_configured,
            "status": "needs_setup" if not vn_configured else "override" if vn_provider_override or vn_model_override else "recommended",
            "status_ok": vn_configured,

        },
        {
            **_catalog_configuration("work_planner", settings),


            "active": bool(
                getattr(settings, "COOPERATIVE_WORK_PLANNER_ENABLED", False)
            ),
            "configured": True,
            "status": "override" if settings.COOPERATIVE_WORK_PLANNER_MODEL else "inherited",
            "status_ok": True,

        },
        {
            **_catalog_configuration("work_observer", settings),


            "configured": True,

        },
        {
            **_catalog_configuration("browser_branch_planner", settings),


            "configured": True,
            "status": "override" if os.environ.get("BROWSER_BRANCH_PROVIDER") or os.environ.get("BROWSER_BRANCH_MODEL") else "inherited",
            "status_ok": True,

        },
        {
            **_catalog_configuration("auip_narration", settings),


            "configured": True,

        },
        {
            **_catalog_configuration("auip_action", settings),


            "active": b2_active,
            "configured": not bool(b2_unavailable),
            "status": "needs_setup" if b2_unavailable else "available" if b2_active else "optional",
            "status_ok": not bool(b2_unavailable),
            "status_detail": b2_status_detail,

        },
        {
            **_catalog_configuration("vn_subtitle_translation", settings),


            "configured": True,
            "status": "override" if os.environ.get("VN_SUBTITLE_TRANSLATE_PROVIDER") or os.environ.get("VN_SUBTITLE_TRANSLATE_MODEL") else "inherited",
            "status_ok": True,

        },
        {
            **_catalog_configuration("vn_speech_translation", settings),


            "configured": True,
            "status": "override" if os.environ.get("VN_TTS_TRANSLATE_PROVIDER") or os.environ.get("VN_TTS_TRANSLATE_MODEL") else "inherited",
            "status_ok": True,

        },
    ]


def _acp_credentials(settings: Any) -> list[dict[str, Any]]:

    return [
        _catalog_field("ANTHROPIC_API_KEY", settings),
        {**_catalog_field("DEEPSEEK_API_KEY", settings), "label": "DeepSeek API key"},
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
    codex_fields = _catalog_configuration("codex", settings,
        values={"CODEX_PROVIDER_TRANSPORT": codex_transport},
        options={"CODEX_APP_SERVER_MODEL_PROVIDER": codex_connection_options})["fields"]
    return [
        {
            **_catalog_configuration("pi", settings),


        },
        {
            "id": "browser",
            "label": "Browser",
            "description": "Host-managed browser work Provider; no connection settings.",
            "fields": [],
        },
        {
            **_catalog_configuration("openclaw", settings),



        },
        {
            **_catalog_configuration("codex", settings, values={"CODEX_PROVIDER_TRANSPORT": codex_transport}),


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
            "acp_credentials": _acp_credentials(settings),
            "artifact_configuration": _artifact_configuration(settings),
            "voice_configuration": voice_configuration,
            "avatar_configuration": _avatar_configuration(settings),
            "asr_backends": asr_backend_statuses(asr_backend),
            "tts_backends": tts_backend_statuses(
                str(getattr(settings, "TTS_BACKEND", "gpt_sovits"))
            ),
            **{field["runtime_key"]: vision[field["runtime_key"].removeprefix("vision_")]
               for field in configuration_groups()["vision"]["config"].values()},
            "vision_provider": vision["provider"],
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

        # The three compound/legacy inputs are owned by TTS and presentation.
        declared_runtime = runtime_fields()
        allowed = {*declared_runtime, "tts_mode", "tts_output_language", "wallpaper_subtitle_language"}
        unknown = sorted(str(key) for key in values if str(key) not in allowed)
        if unknown:
            raise ValueError(f"unsupported runtime setting(s): {', '.join(unknown)}")
        values = {str(key): value for key, value in values.items()}

        if CHARACTER_PROMPT_SETTING in values:
            values[CHARACTER_PROMPT_SETTING] = normalize_character_prompt(values[CHARACTER_PROMPT_SETTING])

        for key, definition in declared_runtime.items():
            if key not in values:
                continue
            value = values[key]
            if definition["type"] == "boolean" and not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            if definition["type"] == "enum":
                choices = option_values(definition)
                value = str(value or "").strip()
                if all(choice == choice.lower() for choice in choices):
                    value = value.lower()
                if value not in choices:
                    raise ValueError(f"unsupported {key}: {value!r}")
                values[key] = value
            if definition["type"] in {"integer", "number"}:
                try:
                    value = (int if definition["type"] == "integer" else float)(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"{key} must be an {definition['type']}") from exc
                if not math.isfinite(value):
                    raise ValueError(f"{key} must be a finite number")
                minimum, maximum = definition.get("min"), definition.get("max")
                if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
                    raise ValueError(f"{key} must be between {minimum} and {maximum}")
                values[key] = value
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
