"""Static declarations and existing live owners share their setting contracts."""

from __future__ import annotations

import copy
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import catalog
from config.environment import EnvironmentReader
from server.handlers import system_handler


def test_application_policies_and_runtime_fields_are_declared_once():
    fields = catalog.runtime_fields()
    assert fields["vision_enabled"]["type"] == "boolean"
    assert fields["vision_max_long_side"]["type"] == "integer"
    assert fields["main_chat_character_prompt_ja"]["key"] == "AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA"
    assert "tts_mode" not in fields  # TTS still owns its composite mapping.
    assert catalog.application_policy("AMADEUS_UI_LOCALE") == "frontend"
    assert catalog.application_policy("AMADEUS_WINDOWS_STARTUP_MODE") == "desktop_restart"
    assert catalog.application_policy("LLM_PROVIDER") == "host"
    assert catalog.application_policy("VTS_ENABLED") == "backend_restart"
    assert not {"COOPERATIVE_CHAT_ENABLED", "AMADEUS_ACP_PROVIDERS"}.intersection(
        key for group in catalog.configuration_groups().values() for key in group["config"]
    )


def test_new_live_field_reaches_backend_status_without_a_handwritten_field(monkeypatch):
    groups = copy.deepcopy(catalog.configuration_groups())
    groups["presentation"]["config"]["TEST_RUNTIME_COUNTER"] = {
        "type": "integer", "title": {"en-US": "Fixture counter", "zh-CN": "测试计数器"},
        "default": 3, "scope": "session", "runtime_key": "fixture_counter", "min": 1, "max": 8, "step": 1,
    }
    monkeypatch.setattr(catalog, "configuration_groups", lambda: groups)
    monkeypatch.setattr(system_handler, "configuration_groups", lambda: groups)
    monkeypatch.setenv("TEST_RUNTIME_COUNTER", "5")
    fields = {field["key"]: field for field in system_handler._catalog_configuration(
        "presentation", SimpleNamespace(), values={"AMADEUS_WALLPAPER_CAPTION_MODE": "translated"},
    )["fields"]}
    assert fields["TEST_RUNTIME_COUNTER"]["value"] == 5
    assert fields["TEST_RUNTIME_COUNTER"]["apply"] == "host"
    assert fields["TEST_RUNTIME_COUNTER"]["restart_required"] is False
    assert catalog.read_catalog_value(EnvironmentReader({"TEST_RUNTIME_COUNTER": "7"}), "TEST_RUNTIME_COUNTER") == 7


def test_visual_owner_keeps_legacy_tolerant_parsing_and_declared_defaults(monkeypatch):
    from server import visual_runtime

    monkeypatch.setenv("AMADEUS_VISION_ENABLED", "on")
    monkeypatch.setenv("AMADEUS_VISION_MAX_LONG_SIDE", "invalid")
    monkeypatch.setenv("AMADEUS_VISION_MODE", "  ")
    assert visual_runtime._bool_env("AMADEUS_VISION_ENABLED") is True
    assert visual_runtime._int_env("AMADEUS_VISION_MAX_LONG_SIDE") == catalog.configuration_field("AMADEUS_VISION_MAX_LONG_SIDE")["default"]
    assert visual_runtime._str_env("AMADEUS_VISION_MODE") == catalog.configuration_field("AMADEUS_VISION_MODE")["default"]


def test_session_boolean_truth_values_preserve_the_existing_owner_contract():
    for key in ["AMADEUS_VISION_ENABLED", "AMADEUS_CHAT_TRANSLATION_SUBTITLES_ENABLED"]:
        assert catalog.read_catalog_value(EnvironmentReader({key: " ON "}), key) is True
        assert catalog.read_catalog_value(EnvironmentReader({key: "false"}), key) is False
    assert EnvironmentReader({"UNDECLARED_BOOLEAN": "on"}).boolean("UNDECLARED_BOOLEAN", False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,value,expected", [("integer", 6.8, 6), ("number", "6.8", 6.8)])
async def test_new_host_numeric_field_without_range_uses_its_declared_type(monkeypatch, kind, value, expected):
    from server import visual_runtime

    groups = copy.deepcopy(catalog.configuration_groups())
    groups["vision"]["config"]["TEST_RUNTIME_NUMBER"] = {
        "type": kind, "title": {"en-US": "Fixture number", "zh-CN": "测试数值"},
        "default": 3, "scope": "session", "runtime_key": "vision_fixture_number",
    }
    monkeypatch.setattr(catalog, "configuration_groups", lambda: groups)
    monkeypatch.setattr(system_handler, "configuration_groups", lambda: groups)
    applied = {}
    monkeypatch.setattr(visual_runtime, "set_config", lambda values: applied.update(values) or list(values))
    handler = system_handler.SystemHandler()
    monkeypatch.setattr(handler, "_get_config", AsyncMock(return_value={}))
    await handler._set_config({"values": {"vision_fixture_number": value}})
    assert applied["vision_fixture_number"] == expected
    assert isinstance(applied["vision_fixture_number"], int if kind == "integer" else float)
    if kind == "number":
        for invalid in ["NaN", "Infinity", "-Infinity"]:
            with pytest.raises(ValueError, match="finite number"):
                await handler._set_config({"values": {"vision_fixture_number": invalid}})


@pytest.mark.parametrize("legacy,mode", [("ja", "source"), ("both", "bilingual"), ("off", "off")])
def test_presentation_legacy_composite_input_keeps_its_owner(monkeypatch, legacy, mode):
    from server import presentation_runtime

    monkeypatch.setenv("AMADEUS_WALLPAPER_SUBTITLE_LANG", legacy)
    monkeypatch.delenv("AMADEUS_WALLPAPER_CAPTION_MODE", raising=False)
    importlib.reload(presentation_runtime)
    assert presentation_runtime.get_caption_mode() == mode
    monkeypatch.setenv("AMADEUS_WALLPAPER_CAPTION_MODE", "translated")
    importlib.reload(presentation_runtime)
    assert presentation_runtime.get_caption_mode() == "translated"
