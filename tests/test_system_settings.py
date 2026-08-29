from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from server.handlers.asr_handler import AsrHandler
from server.handlers.system_handler import (
    SystemHandler,
    _avatar_configuration,
    _model_connections,
    _voice_configuration,
    _work_provider_configuration,
)


class _FakeASRManager:
    def __init__(self, backend: str = "qwen3_asr") -> None:
        self._backend_name = backend
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_settings_connection_descriptors_never_return_secret_values() -> None:
    from config import settings

    groups = [
        *_model_connections(settings, "deepseek"),
        *_voice_configuration(settings),
        *_work_provider_configuration(settings),
    ]
    secret_fields = [
        field
        for group in groups
        for field in group["fields"]
        if field["type"] == "secret"
    ]
    assert secret_fields
    assert all("value" not in field for field in secret_fields)
    assert {field["key"] for field in secret_fields} >= {
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OPENCLAW_GATEWAY_TOKEN",
        "ASR_API_KEY",
        "TTS_API_KEY",
        "MIMO_TTS_API_KEY",
    }


def test_deepseek_main_model_is_an_independent_editable_startup_field() -> None:
    from config import settings

    deepseek = next(
        group for group in _model_connections(settings, "deepseek")
        if group["id"] == "deepseek"
    )
    model = next(field for field in deepseek["fields"] if field["label"] == "Model")
    assert model["key"] == "DEEPSEEK_MODEL_NAME"
    assert model["value"] == settings.DEEPSEEK_MODEL_NAME
    assert model["editable"] is True


def test_user_managed_voice_and_avatar_startup_controls_are_grouped_by_owner() -> None:
    from config import settings

    voice = {group["id"]: group for group in _voice_configuration(settings)}
    conversation = {field["key"] for field in voice["conversation_asr"]["fields"]}
    acoustic = {field["key"] for field in voice["acoustic_pipeline"]["fields"]}
    embedded = {field["key"] for field in voice["tts_embedded_v3"]["fields"]}
    references = {
        field["key"] for field in voice["voice_reference_profile"]["fields"]
    }
    avatar = _avatar_configuration(settings)[0]

    assert {"QWEN3_ASR_DEVICE", "ASR_VAD_SILENCE_MS"} <= conversation
    assert acoustic == {
        "AEC_REALTIME_ENABLED",
        "AEC_REALTIME_BARGE_IN",
        "AEC_REALTIME_DELAY_MS",
    }
    assert "TTS_DEVICE" in embedded
    assert references == {
        "TTS_REF_AUDIO_JA",
        "TTS_REF_TEXT_JA",
        "TTS_REF_AUDIO_EN",
        "TTS_REF_TEXT_EN",
    }
    assert "GPT-SoVITS" in voice["voice_reference_profile"]["status_detail"]
    assert {field["key"] for field in avatar["fields"]} == {
        "VTS_ENABLED",
        "VTS_WS_URL",
        "VTS_TOKEN_FILE",
    }


def test_settings_only_publish_composed_work_providers() -> None:
    from config import settings

    assert {group["id"] for group in _work_provider_configuration(settings)} == {
        "browser",
        "openclaw",
        "codex",
    }


def test_asr_handler_owns_desired_and_loaded_backend() -> None:
    async def run() -> None:
        handler = AsrHandler()
        assert handler.backend_name in {"qwen3_asr", "sense_voice", "openai_compatible"}

        await handler.set_backend("sense_voice")
        assert handler.backend_name == "sense_voice"

        manager = _FakeASRManager("sense_voice")
        handler._asr_manager = manager
        await handler.set_backend("qwen3_asr")
        assert manager.closed is True
        assert handler._asr_manager is None
        assert handler.backend_name == "qwen3_asr"

    asyncio.run(run())


def test_asr_backend_switch_rejects_active_listening() -> None:
    async def run() -> None:
        handler = AsrHandler()
        handler._active = True
        with pytest.raises(RuntimeError, match="stop ASR listening"):
            await handler.set_backend("sense_voice")

    asyncio.run(run())


def test_system_settings_reject_unknown_keys_instead_of_claiming_update() -> None:
    async def run() -> None:
        handler = SystemHandler()
        with pytest.raises(ValueError, match="unsupported runtime setting"):
            await handler._set_config({"values": {"floating_subtitle": True}})

    asyncio.run(run())


def test_system_settings_report_optional_character_pack_status() -> None:
    async def run() -> None:
        handler = SystemHandler()
        expected = {
            "id": "kurisu",
            "display_name": "Kurisu",
            "installed": False,
            "state": "not_installed",
        }
        with patch("render.character_pack.character_pack_status", return_value=expected):
            result = await handler._get_config({})
        assert result["character_pack"] == expected

    asyncio.run(run())


def test_system_settings_report_optional_visual_asset_pack_status() -> None:
    async def run() -> None:
        handler = SystemHandler()
        expected = {
            "id": "visual-runtime",
            "display_name": "Visual Runtime Pack",
            "installed": False,
            "state": "not_installed",
        }
        with patch("config.asset_packages.external_asset_pack_status", return_value=expected):
            result = await handler._get_config({})
        assert result["visual_asset_pack"] == expected

    asyncio.run(run())


def test_voice_settings_keep_wake_and_conversation_recognition_independent() -> None:
    from config import settings

    groups = {group["id"]: group for group in _voice_configuration(settings)}

    conversation_keys = {field["key"] for field in groups["conversation_asr"]["fields"]}
    wake_keys = {field["key"] for field in groups["wake_asr"]["fields"]}
    assert "ASR_BACKEND" in conversation_keys
    assert "WAKE_ASR_BACKEND" in wake_keys
    assert "WAKE_ASR_BACKEND" not in conversation_keys
    assert {group["status"] for group in groups.values()} <= {
        "installed",
        "not_installed",
        "unavailable",
        "remote",
        "available",
        "disabled",
    }
    assert "v3 checkpoints only" in groups["speech_synthesis"]["description"]
    embedded_tts = groups["tts_embedded_v3"]
    assert "v1 and v2 checkpoints are not supported" in embedded_tts["description"]
    assert {field["key"] for field in embedded_tts["fields"]} == {
        "TTS_GPT_MODEL_PATH",
        "TTS_SOVITS_MODEL_PATH",
        "TTS_DEVICE",
    }
    remote_tts_fields = {
        field["key"]: field for field in groups["tts_remote"]["fields"]
    }
    assert remote_tts_fields["TTS_API_STREAM_PROTOCOL"]["options"] == [
        {"value": "buffered", "label": "Buffered WAV · compatible"},
        {"value": "openai_sse", "label": "OpenAI SSE · streaming PCM"},
    ]
    assert {field["key"] for field in groups["tts_mimo"]["fields"]} == {
        "MIMO_TTS_BASE_URL",
        "MIMO_TTS_API_KEY",
        "MIMO_TTS_MODEL",
        "MIMO_TTS_VOICE",
    }


def test_mimo_desktop_settings_persist_values_and_encrypt_the_key() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "electron"
        / "src"
        / "main"
        / "desktopSettings.ts"
    ).read_text(encoding="utf-8")
    value_block = source[source.index("const VALUE_KEYS"):source.index("const SECRET_KEYS")]
    secret_block = source[
        source.index("const SECRET_KEYS"):source.index("const CODEX_TRANSPORT_KEYS")
    ]

    assert all(
        f"'{key}'" in value_block
        for key in ("MIMO_TTS_BASE_URL", "MIMO_TTS_MODEL", "MIMO_TTS_VOICE")
    )
    assert "'MIMO_TTS_API_KEY'" in secret_block


def test_voice_settings_publish_microphone_choices_without_recording_audio() -> None:
    from asr.microphone import MicDeviceDescriptor
    from config import settings

    device = MicDeviceDescriptor(
        index=7,
        name="USB Studio Mic",
        host_api="Windows WASAPI",
        max_input_channels=1,
        default_sample_rate=48000.0,
        device_class="usb",
    )
    with patch("asr.microphone.list_microphone_devices", return_value=[device]):
        groups = {group["id"]: group for group in _voice_configuration(settings)}
    microphone = next(
        field
        for field in groups["conversation_asr"]["fields"]
        if field["key"] == "MICROPHONE_DEVICE_INDEX"
    )
    assert microphone["options"] == [
        {"value": "-1", "label": "Automatic"},
        {"value": "7", "label": "USB Studio Mic · Windows WASAPI"},
    ]


def test_system_settings_apply_real_tts_and_asr_owners() -> None:
    async def run() -> None:
        import tts.pipeline as tts_pipeline

        old_mode = tts_pipeline.current_tts_mode()
        old_language = tts_pipeline.current_tts_language_code()
        asr_handler = AsrHandler()
        handler = SystemHandler()
        handler.configure(asr_handler=asr_handler)
        try:
            with patch("server.handlers.system_handler.bus.emit", new=AsyncMock()):
                result = await handler._set_config(
                    {
                        "values": {
                            "tts_mode": "parallel",
                            "tts_output_language": "en",
                            "asr_backend": "sense_voice",
                        }
                    }
                )
            assert result["values"]["tts_mode"] == "parallel"
            assert result["values"]["tts_output_language"] == "en"
            assert result["values"]["asr_backend"] == "sense_voice"
            assert set(result["updated"]) == {
                "tts_mode",
                "tts_output_language",
                "asr_backend",
            }
        finally:
            tts_pipeline.reconfigure_tts_mode_name(old_mode)
            tts_pipeline.reconfigure_tts_language_code(old_language)

    asyncio.run(run())


def test_system_settings_validate_vision_numbers_before_applying() -> None:
    async def run() -> None:
        handler = SystemHandler()
        with pytest.raises(ValueError, match="between 35 and 92"):
            await handler._set_config({"values": {"vision_jpeg_quality": 100}})

    asyncio.run(run())


def test_system_settings_reject_tts_change_during_active_chat() -> None:
    async def run() -> None:
        handler = SystemHandler()
        handler.configure(is_chat_busy=lambda: True)
        with pytest.raises(RuntimeError, match="active chat turn"):
            await handler._set_config({"values": {"tts_output_language": "en"}})

    asyncio.run(run())


def test_system_settings_reject_llm_routing_change_during_active_chat() -> None:
    async def run() -> None:
        handler = SystemHandler()
        handler.configure(is_chat_busy=lambda: True)
        with pytest.raises(RuntimeError, match="active chat turn"):
            await handler._set_config({"values": {"llm_provider": "local"}})

    asyncio.run(run())


def test_llm_provider_update_syncs_both_runtime_owners() -> None:
    async def run() -> None:
        import llm.client as llm_client
        from core.chat_runtime import get_chat_runtime

        handler = SystemHandler()
        runtime = get_chat_runtime()
        old_client_provider = llm_client.LLM_PROVIDER
        old_runtime_provider = runtime.provider
        try:
            with patch("server.handlers.system_handler.bus.emit", new=AsyncMock()):
                result = await handler._set_config({"values": {"llm_provider": "openai"}})
            assert llm_client.LLM_PROVIDER == "openai"
            assert runtime.provider == "openai"
            assert result["values"]["llm_provider"] == "openai"
        finally:
            runtime.set_provider(old_runtime_provider)
            llm_client.configure(llm_provider=old_client_provider)

    asyncio.run(run())


def test_pure_local_backend_type_syncs_runtime_and_fallback() -> None:
    async def run() -> None:
        from config import settings
        import llm.client as llm_client
        from core.chat_runtime import get_chat_runtime

        handler = SystemHandler()
        runtime = get_chat_runtime()
        old_type = runtime.local_llm_type
        try:
            with patch("server.handlers.system_handler.bus.emit", new=AsyncMock()):
                result = await handler._set_config(
                    {"values": {"local_llm_type": "lmstudio"}}
                )
            assert runtime.local_llm_type == "lmstudio"
            assert llm_client.LOCAL_LLM_TYPE == "lmstudio"
            assert settings.LOCAL_LLM_TYPE == "lmstudio"
            assert result["values"]["local_llm_type"] == "lmstudio"
        finally:
            runtime.set_local_llm_type(old_type)

    asyncio.run(run())


def test_local_model_settings_show_only_the_selected_compatibility_profile() -> None:
    from config import settings

    status = {
        "configured": True,
        "available": True,
        "state": "available",
        "detail": "ready",
    }
    with patch.object(settings, "LOCAL_LLM_TYPE", "ollama"):
        groups = {
            group["id"]: group
            for group in _model_connections(
                settings,
                "local",
                local_status=status,
                hybrid_status=status,
            )
        }
    local_keys = {field["key"] for field in groups["local"]["fields"]}
    assert local_keys == {
        "LOCAL_LLM_TYPE",
        "LOCAL_LLM_MODEL",
        "LOCAL_LLM_OLLAMA_URL",
        "RAG_ENABLED_FOR_LOCAL",
        "RAG_TOP_K",
        "RAG_MAX_DISTANCE",
    }
    assert groups["local"]["active"] is True
    assert groups["hybrid_local"]["active"] is False

    hybrid_groups = {
        group["id"]: group
        for group in _model_connections(
            settings,
            "hybrid2",
            local_status=status,
            hybrid_status=status,
        )
    }
    assert hybrid_groups["local"]["active"] is False
    assert hybrid_groups["hybrid_local"]["active"] is True
    assert {field["key"] for field in hybrid_groups["hybrid_local"]["fields"]} == {
        "HYBRID_LOCAL_LLM_URL",
        "HYBRID_LOCAL_LLM_MODEL",
    }


def test_runtime_provider_switch_keeps_managed_llama_server_lifecycle_aligned() -> None:
    async def run() -> None:
        from config import settings
        import llm.client as llm_client
        from core.chat_runtime import get_chat_runtime

        handler = SystemHandler()
        runtime = get_chat_runtime()
        old_provider = runtime.provider
        old_type = runtime.local_llm_type
        old_launch_mode = settings.LOCAL_LLM_LAUNCH_MODE
        start = AsyncMock()
        warmup = AsyncMock()
        stop = Mock()
        try:
            settings.LOCAL_LLM_LAUNCH_MODE = "managed"
            runtime.set_local_llm_type("llama_server")
            with (
                patch("server.handlers.system_handler.bus.emit", new=AsyncMock()),
                patch("llm.llama_server.start_llama_server", new=start),
                patch("llm.llama_server.warmup_local_llm_cache", new=warmup),
                patch("llm.llama_server.stop_llama_server", new=stop),
            ):
                await handler._set_config({"values": {"llm_provider": "local"}})
                await asyncio.sleep(0)
                assert runtime.provider == "local"
                assert runtime.use_local_llm is True
                start.assert_awaited_once()
                warmup.assert_awaited_once()

                await handler._set_config({"values": {"llm_provider": "openai"}})
                assert runtime.provider == "openai"
                assert runtime.use_local_llm is False
                stop.assert_called_once()
        finally:
            settings.LOCAL_LLM_LAUNCH_MODE = old_launch_mode
            runtime.set_local_llm_type(old_type)
            runtime.set_provider(old_provider)
            llm_client.configure(llm_provider=old_provider, local_llm_type=old_type)

    asyncio.run(run())


def test_first_sentence_cache_key_tracks_runtime_tts_language() -> None:
    from config import settings
    import tts.pipeline as tts_pipeline
    from tts.first_sentence_audio_cache import FirstSentenceAudioCache

    old_language = tts_pipeline.current_tts_language_code()
    cache = FirstSentenceAudioCache("runtime/test-cache")
    try:
        tts_pipeline.reconfigure_tts_language_code("ja")
        assert cache.key_payload("hello", {})["tts_output_language"] == "ja"
        tts_pipeline.reconfigure_tts_language_code("en")
        assert cache.key_payload("hello", {})["tts_output_language"] == "en"
        assert settings.TTS_OUTPUT_LANGUAGE == "英文"
    finally:
        tts_pipeline.reconfigure_tts_language_code(old_language)
