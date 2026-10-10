from __future__ import annotations

import copy
import json
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest

from config import catalog
from config.environment import EnvironmentReader
from server.handlers import system_handler


def test_no_new_or_migrated_handwritten_config_declarations() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    baseline = json.loads((root / "config/catalog_legacy.json").read_text(encoding="utf-8"))
    declared = {key for group in catalog.configuration_groups().values() for key in group["config"]}
    for filename, allowed in baseline["python"].items():
        for node in ast.walk(ast.parse((root / filename).read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            func = node.func
            environment_read = (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                                and func.value.id == "_ENV" and func.attr in {"boolean", "integer", "number", "string", "secret"})
            handwritten_field = isinstance(func, ast.Name) and func.id in {
                "_startup_field", "_str", "_bool", "_int", "_float", "_secret",
            }
            if environment_read or handwritten_field:
                key = node.args[0].value
                assert key in allowed and key not in declared, f"{filename}:{node.lineno}: declare {key} in the catalog"


def test_catalog_defaults_remain_registered_in_the_environment_reader() -> None:
    reader = EnvironmentReader({})
    values = catalog.read_catalog_environment(reader)
    declared = {field.key: field for field in reader.fields()}
    for group in catalog.configuration_groups().values():
        for key, field in group["config"].items():
            if field.get("scope", "backend") != "backend":
                assert key not in values
                if field.get("scope") == "session" and not field.get("computed_default"):
                    assert catalog.read_catalog_value(EnvironmentReader({}), key) == field.get("default", "")
                continue
            if field.get("computed_default"):
                assert key not in values
                continue
            assert values[key] == field.get("default", "")
            assert declared[key].default == field.get("default", "")
            assert declared[key].value_type == ("secret" if field.get("secret") else {
                "boolean": "bool", "integer": "int", "number": "float",
            }.get(field["type"], "str"))


def test_catalog_preserves_explicit_empty_values_and_secret_normalization() -> None:
    reader = EnvironmentReader({
        "FISH_TTS_MODEL": "", "FISH_TTS_LATENCY": "low",
        "FISH_TTS_API_KEY": '  "test-key"  ',
    })
    values = catalog.read_catalog_environment(reader)
    assert values["FISH_TTS_MODEL"] == ""
    assert values["FISH_TTS_LATENCY"] == "low"
    assert values["FISH_TTS_API_KEY"] == "test-key"


def test_status_uses_effective_values_and_never_presents_secret_contents() -> None:
    values = catalog.read_catalog_environment(EnvironmentReader({
        "FISH_TTS_MODEL": "effective-model", "FISH_TTS_API_KEY": "test-private-key",
    }))
    group = system_handler._catalog_configuration("tts_fish_audio", SimpleNamespace(**values))
    fields = {field["key"]: field for field in group["fields"]}
    assert fields["FISH_TTS_MODEL"]["value"] == "effective-model"
    assert fields["FISH_TTS_API_KEY"]["configured"] is True
    assert "value" not in fields["FISH_TTS_API_KEY"]
    assert "test-private-key" not in json.dumps(group)
    assert "available" not in group  # Availability still belongs to the runtime probe.
    assert all(field["restart_required"] for field in group["fields"])


def test_new_declaration_reaches_settings_and_status_without_glue(monkeypatch) -> None:
    groups = copy.deepcopy(catalog.configuration_groups())
    groups["tts_fish_audio"]["config"]["TEST_VOICE_STYLE"] = {
        "type": "enum", "title": {"en-US": "Voice style", "zh-CN": "语音风格"},
        "default": "natural", "options": ["natural", "calm"],
    }
    monkeypatch.setattr(catalog, "configuration_groups", lambda: groups)
    monkeypatch.setattr(system_handler, "configuration_groups", lambda: groups)
    values = catalog.read_catalog_environment(EnvironmentReader({"TEST_VOICE_STYLE": "calm"}))
    group = system_handler._catalog_configuration("tts_fish_audio", SimpleNamespace(**values))
    field = next(field for field in group["fields"] if field["key"] == "TEST_VOICE_STYLE")
    assert field["value"] == "calm"
    assert field["options"] == [{"value": "natural", "label": "natural"}, {"value": "calm", "label": "calm"}]


def test_invalid_unselected_fish_configuration_is_left_to_the_backend_owner() -> None:
    values = catalog.read_catalog_environment(EnvironmentReader({"FISH_TTS_LATENCY": "invalid"}))
    assert values["FISH_TTS_LATENCY"] == "invalid"


@pytest.mark.parametrize("default", [False, True])
@pytest.mark.parametrize("override", [None, "true", "false"])
def test_computed_sampling_default_keeps_explicit_environment_authority(default, override) -> None:
    reader = EnvironmentReader({} if override is None else {"RENDER_TEXTURE_SAMPLING": override})
    assert "RENDER_TEXTURE_SAMPLING" not in catalog.read_catalog_environment(reader)
    values = catalog.read_catalog_environment(reader, computed_defaults={"RENDER_TEXTURE_SAMPLING": default})
    assert values == {"RENDER_TEXTURE_SAMPLING": default if override is None else override == "true"}


def test_new_tts_declaration_and_implementation_reach_registry_and_status(monkeypatch) -> None:
    from tts import registry
    from tts.backend import BaseTTSBackend

    calls = []

    class FixtureBackend(BaseTTSBackend):
        backend_id = "catalog_fixture"

        def load(self):
            calls.append("load")

        def synthesize(self, request):
            raise NotImplementedError

    implementation = ModuleType("tts.backends.catalog_fixture")
    implementation.Backend = FixtureBackend
    implementation.probe = lambda: ("remote", "fixture configured")
    monkeypatch.setitem(sys.modules, implementation.__name__, implementation)
    group = copy.deepcopy(catalog.configuration_groups()["tts_fish_audio"])
    group["id"] = "tts_catalog_fixture"
    group["voice_backend"].update(
        id="catalog_fixture", factory=f"{implementation.__name__}:Backend", probe=f"{implementation.__name__}:probe",
    )
    group["config"] = {"CATALOG_FIXTURE_MODEL": {
        "type": "string", "title": {"en-US": "Model", "zh-CN": "模型"}, "default": "fixture-model",
    }}
    definitions = {group["id"]: group}
    monkeypatch.setattr(catalog, "configuration_groups", lambda: definitions)
    monkeypatch.setattr(system_handler, "configuration_groups", lambda: definitions)
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_BUILTINS_READY", False)

    assert registry.tts_backend_ids() == ("catalog_fixture", "disabled")
    assert calls == []  # Listing a builtin must not construct/load it.
    values = catalog.read_catalog_environment(EnvironmentReader({}))
    statuses = registry.tts_backend_statuses("catalog_fixture")
    projection = system_handler._voice_backend_configuration(group, SimpleNamespace(**values), statuses, "catalog_fixture")
    assert projection["active"] and projection["configured"]
    assert projection["fields"][0]["value"] == "fixture-model"
    assert registry.create_tts_runtime("catalog_fixture").backend_id == "catalog_fixture"
    assert calls == ["load"]
    with pytest.raises(ValueError, match="cannot unregister built-in"):
        registry.unregister_tts_backend("catalog_fixture")


def test_model_catalog_keeps_legacy_alias_and_cli_string_contract():
    reader = EnvironmentReader({'LM_STUDIO_URL': 'http://localhost:1235', 'LOCAL_LLM_CLI_CONTEXT': '8192'})
    with pytest.warns(DeprecationWarning, match='LM_STUDIO_URL'):
        values = catalog.read_catalog_environment(reader)
    assert values['LOCAL_LLM_LM_STUDIO_URL'] == 'http://localhost:1235'
    assert values['LOCAL_LLM_CLI_CONTEXT'] == '8192'
    assert values['RAG_MAX_DISTANCE'] == 0.33
    inherited = catalog.read_catalog_environment(reader, computed_defaults={
        'HYBRID_LOCAL_LLM_URL': 'http://localhost:8089/v1', 'HYBRID_LOCAL_LLM_MODEL': 'local-test-model',
    })
    assert inherited == {'HYBRID_LOCAL_LLM_URL': 'http://localhost:8089/v1', 'HYBRID_LOCAL_LLM_MODEL': 'local-test-model'}
    explicit = catalog.read_catalog_environment(EnvironmentReader({'HYBRID_LOCAL_LLM_MODEL': ''}),
        computed_defaults={'HYBRID_LOCAL_LLM_MODEL': 'inherited'})
    assert explicit['HYBRID_LOCAL_LLM_MODEL'] == ''


def test_vn_session_defaults_remain_late_bound_with_catalog_defaults(monkeypatch):
    from vn_player.runtime import VNPlayerRuntime
    state = SimpleNamespace(_retrospective_enabled=False)
    monkeypatch.delenv('VN_LLM_PROVIDER', raising=False)
    monkeypatch.delenv('VN_LLM_MODEL', raising=False)
    default = VNPlayerRuntime._profile_defaults(state, 'base')
    assert default['provider'] == 'deepseek'
    assert default['model'] == ''
    monkeypatch.setenv('VN_LLM_PROVIDER', 'openai')
    monkeypatch.setenv('VN_LLM_MODEL', 'session-model')
    explicit = VNPlayerRuntime._profile_defaults(state, 'base')
    assert explicit['provider'] == 'openai'
    assert explicit['model'] == 'session-model'
