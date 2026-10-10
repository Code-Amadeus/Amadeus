from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from server.handlers.asr_handler import AsrHandler
from server.handlers.system_handler import (
    SystemHandler,
    _avatar_configuration,
    _model_connections,
    _model_role_configuration,
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
        "FISH_TTS_API_KEY",
    }


def test_settings_exposes_default_b2_as_needing_setup_without_action_model() -> None:
    from config import settings

    with (
        patch.object(settings, "AUIP_APPSESSION_ROLE_BRANCH_MODE", "b2"),
        patch.object(settings, "AUIP_CONTROL_DECISION_ENABLED", True),
        patch(
            "server.auip_b2_role_llm.has_b2_role_model_config",
            return_value=False,
        ),
    ):
        roles = {group["id"]: group for group in _model_role_configuration(settings)}

    action = roles["auip_action"]
    assert action["active"] is True
    assert action["configured"] is False
    assert action["status"] == "needs_setup"
    assert action["status_ok"] is False
    assert "Application actions remain blocked" in action["status_detail"]


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
    assert "TTS_VOICE_PROFILE" in embedded
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
        "pi",
        "browser",
        "openclaw",
        "codex",
    }


@pytest.mark.parametrize("professional", [True, False])
def test_retired_false_is_read_only_and_does_not_change_runtime_or_work_permissions(monkeypatch, professional):
    from config import settings

    facts = ({"key": "COOPERATIVE_CHAT_ENABLED", "value": False,
        "source": "environment", "effective_behavior": "cooperative_only"},)
    monkeypatch.setattr(settings, "COOPERATIVE_CHAT_ENABLED", False)
    monkeypatch.setattr(settings, "_RETIRED_SETTINGS", facts)
    monkeypatch.setattr(settings, "COOPERATIVE_WORK_PLANNER_ENABLED", professional)
    monkeypatch.setattr(settings, "COOPERATIVE_CHAT_PERMISSION_POLICY", "ask")
    config = asyncio.run(SystemHandler()._get_config({}))
    assert config["cooperative_chat_enabled"] is True
    assert config["cooperative_work_planner_enabled"] is professional
    assert config["cooperative_permission_policy"] == "ask"
    assert config["control_decision_mode"] == "retired"
    assert config["retired_settings"] == list(facts)
    planner = next(group for group in config["model_roles"] if group["id"] == "work_planner")
    assert planner["active"] is professional
    with pytest.raises(ValueError, match="unsupported runtime setting"):
        asyncio.run(SystemHandler()._set_config({"values": {"COOPERATIVE_CHAT_ENABLED": True}}))


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


def test_emotion_pack_status_is_read_once_off_loop_and_shared(monkeypatch):
    import threading
    from config import asset_packages, settings
    from tts.reference_pack import PACK_ID

    expected = {"id": PACK_ID, "installed": True, "state": "installed"}
    calls = []
    original = asset_packages.external_asset_pack_status

    def status(pack_id, **kwargs):
        if pack_id == PACK_ID:
            calls.append(threading.get_ident())
            return expected
        return original(pack_id, **kwargs)

    monkeypatch.setattr(settings, 'ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING', True)
    monkeypatch.setattr(asset_packages, 'external_asset_pack_status', status)
    loop_thread = threading.get_ident()
    result = asyncio.run(SystemHandler()._get_config({}))
    assert len(calls) == 1 and calls[0] != loop_thread
    assert result['emotion_reference_pack'] is expected
    group = next(group for group in result['voice_configuration'] if group['id'] == 'tts_emotion_references')
    assert group['configured'] is True
    assert 'main_chat_character_prompt_ja' in result


def test_graphics_status_distinguishes_saved_custom_limits_from_applied_preset(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "GRAPHICS_PROFILE", "standard")
    monkeypatch.setattr(settings, "RENDER_MAX_FPS", 48)
    monkeypatch.setattr(settings, "RENDER_MAX_RESOLUTION", 1.25)
    fps, resolution = settings._resolve_graphics_profile("standard", 48, 1.25)
    monkeypatch.setattr(settings, "RENDER_EFFECTIVE_MAX_FPS", fps)
    monkeypatch.setattr(settings, "RENDER_EFFECTIVE_MAX_RESOLUTION", resolution)
    monkeypatch.setattr(settings, "RENDER_TEXTURE_SAMPLING", False)
    monkeypatch.setattr(settings, "RENDER_BC7_CACHE", True)
    result = asyncio.run(SystemHandler()._get_config({}))
    assert result["graphics"] == {"profile": "standard", "custom_max_fps": 48,
        "custom_max_resolution": 1.25, "texture_sampling": False, "bc7_cache": True,
        "effective_max_fps": 60, "effective_max_resolution": None}


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


def test_cooperative_settings_preserve_all_existing_role_backend_choices() -> None:
    async def run() -> None:
        from config import settings
        import llm.client as llm_client

        handler = SystemHandler()
        old_provider = llm_client.LLM_PROVIDER
        with (
            patch.object(settings, "COOPERATIVE_CHAT_ENABLED", True),
            patch.object(llm_client, "LLM_PROVIDER", "deepseek"),
            patch.object(llm_client, "DEEPSEEK_MODEL_NAME", "deepseek-v4-pro"),
            patch("server.handlers.system_handler.bus.emit", new=AsyncMock()),
        ):
            try:
                result = await handler._get_config({})
                assert result["llm_provider"] == "deepseek"
                assert result["chat_supports_images"] is False
                assert result["cooperative_chat_input_capabilities"] == {
                    "typed_text":True, "confirmed_transcript_text":True,
                    "visual_attachment":True, "speculative_voice":False,
                    "physical_voice_validated":False}
                changed = await handler._set_config(
                    {"values":{"llm_provider":"gemini"}})
                assert changed["values"]["llm_provider"] == "gemini"
                assert changed["values"]["chat_supports_images"] is True
                assert llm_client.LLM_PROVIDER == "gemini"
            finally:
                llm_client.configure(llm_provider=old_provider)

    asyncio.run(run())


@pytest.mark.parametrize("provider,model,supported", [
    ("deepseek", "deepseek-flash", True),
    ("deepseek", "deepseek-v4-flash", True),
    ("hybrid2", "deepseek-flash", True),
    ("deepseek", "deepseek-v4-pro", False),
    ("local", "deepseek-flash", False),
])
def test_chat_image_capability_is_published_from_the_selected_model(provider, model, supported):
    import llm.client as llm_client

    with (
        patch.object(llm_client, "LLM_PROVIDER", provider),
        patch.object(llm_client, "DEEPSEEK_MODEL_NAME", model),
    ):
        config = asyncio.run(SystemHandler()._get_config({}))
    assert config["chat_supports_images"] is supported


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
    assert "v2Pro" in groups["speech_synthesis"]["description"]
    assert "v3" in groups["speech_synthesis"]["description"]
    embedded_tts = groups["tts_embedded_v3"]
    assert "checkpoint header selects" in embedded_tts["description"]
    assert {field["key"] for field in embedded_tts["fields"]} == {
        "TTS_VOICE_PROFILE",
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
    assert {field["key"] for field in groups["tts_fish_audio"]["fields"]} == {
        "FISH_TTS_WS_URL", "FISH_TTS_API_KEY", "FISH_TTS_MODEL",
        "FISH_TTS_REFERENCE_ID", "FISH_TTS_LATENCY",
    }


def test_mimo_catalog_separates_credentials_from_connection_values() -> None:
    from config.catalog import configuration_groups

    fields = configuration_groups()["tts_mimo"]["config"]
    assert fields["MIMO_TTS_API_KEY"]["secret"] is True
    assert "default" not in fields["MIMO_TTS_API_KEY"]
    assert all(not fields[key].get("secret") for key in (
        "MIMO_TTS_BASE_URL", "MIMO_TTS_MODEL", "MIMO_TTS_VOICE",
    ))


def test_voice_settings_publish_microphone_choices_without_recording_audio() -> None:
    import pytest

    pytest.importorskip("pyaudio", reason="voice tier (pyaudio) is not installed")
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
            await handler._set_config({"values": {"llm_provider": "openai"}})

    asyncio.run(run())


def test_llm_provider_update_syncs_configuration_owner_and_settings() -> None:
    async def run() -> None:
        import llm.client as llm_client
        from config import settings

        handler = SystemHandler()
        old_client_provider = llm_client.LLM_PROVIDER
        try:
            with patch("server.handlers.system_handler.bus.emit", new=AsyncMock()):
                result = await handler._set_config({"values": {"llm_provider": "openai"}})
            assert llm_client.LLM_PROVIDER == "openai"
            assert settings.LLM_PROVIDER == "openai"
            assert result["values"]["llm_provider"] == "openai"
        finally:
            llm_client.configure(llm_provider=old_client_provider)

    asyncio.run(run())


def test_pure_local_backend_type_syncs_configuration_owner_and_settings() -> None:
    async def run() -> None:
        from config import settings
        import llm.client as llm_client

        handler = SystemHandler()
        old_type = llm_client.LOCAL_LLM_TYPE
        try:
            with patch("server.handlers.system_handler.bus.emit", new=AsyncMock()):
                result = await handler._set_config(
                    {"values": {"local_llm_type": "lmstudio"}}
                )
            assert llm_client.LOCAL_LLM_TYPE == "lmstudio"
            assert settings.LOCAL_LLM_TYPE == "lmstudio"
            assert result["values"]["local_llm_type"] == "lmstudio"
        finally:
            llm_client.configure(local_llm_type=old_type)

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

        handler = SystemHandler()
        old_provider = llm_client.LLM_PROVIDER
        old_type = llm_client.LOCAL_LLM_TYPE
        old_launch_mode = settings.LOCAL_LLM_LAUNCH_MODE
        start = AsyncMock()
        warmup = AsyncMock()
        stop = Mock()
        try:
            settings.LOCAL_LLM_LAUNCH_MODE = "managed"
            llm_client.configure(local_llm_type="llama_server")
            with (
                patch.object(settings, "COOPERATIVE_CHAT_ENABLED", False),
                patch("server.handlers.system_handler.bus.emit", new=AsyncMock()),
                patch("llm.llama_server.start_llama_server", new=start),
                patch("llm.llama_server.warmup_local_llm_cache", new=warmup),
                patch("llm.llama_server.stop_llama_server", new=stop),
            ):
                await handler._set_config({"values": {"llm_provider": "local"}})
                await asyncio.sleep(0)
                assert llm_client.LLM_PROVIDER == "local"
                start.assert_awaited_once()
                warmup.assert_awaited_once()

                await handler._set_config({"values": {"llm_provider": "openai"}})
                assert llm_client.LLM_PROVIDER == "openai"
                stop.assert_called_once()
        finally:
            settings.LOCAL_LLM_LAUNCH_MODE = old_launch_mode
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
