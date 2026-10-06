from __future__ import annotations

import os

import pytest

from config.environment import (
    ConfigurationError,
    EnvironmentReader,
    load_project_environment,
)


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes "])
def test_boolean_preserves_legacy_truthy_values(raw: str) -> None:
    assert EnvironmentReader({"FLAG": raw}).boolean("FLAG", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "on", "", "unexpected"])
def test_boolean_preserves_legacy_false_for_every_other_value(raw: str) -> None:
    assert EnvironmentReader({"FLAG": raw}).boolean("FLAG", True) is False


def test_canonical_key_precedes_deprecated_alias() -> None:
    reader = EnvironmentReader({"CURRENT": "0", "OLD": "1"})
    assert reader.boolean("CURRENT", True, aliases=("OLD",)) is False


def test_alias_is_used_with_a_deprecation_warning() -> None:
    reader = EnvironmentReader({"OLD": "1"})
    with pytest.warns(DeprecationWarning, match="OLD is deprecated"):
        assert reader.boolean("CURRENT", False, aliases=("OLD",)) is True


def test_conflicting_declarations_fail_at_the_configuration_boundary() -> None:
    reader = EnvironmentReader({})
    reader.integer("COUNT", 1)
    with pytest.raises(ConfigurationError, match="conflicting declarations"):
        reader.integer("COUNT", 2)


def test_invalid_number_names_the_setting() -> None:
    reader = EnvironmentReader({"COUNT": "many"})
    with pytest.raises(ConfigurationError, match="COUNT must be an integer"):
        reader.integer("COUNT", 1)


def test_process_environment_precedes_dotenv(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("CONFIG_TEST_VALUE=dotenv\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_TEST_VALUE", "process")

    reader = load_project_environment(tmp_path)

    assert reader.string("CONFIG_TEST_VALUE") == "process"
    assert os.environ["CONFIG_TEST_VALUE"] == "process"


def test_project_reader_is_shared_for_one_root(tmp_path) -> None:
    assert load_project_environment(tmp_path) is load_project_environment(tmp_path)


def test_retired_cooperative_setting_remains_readable_for_migration() -> None:
    from config import settings

    field = next(field for field in settings.declared_environment_fields()
        if field.key == "COOPERATIVE_CHAT_ENABLED")
    assert field.default is True
    assert EnvironmentReader({}).boolean("COOPERATIVE_CHAT_ENABLED", True) is True
    assert EnvironmentReader({"COOPERATIVE_CHAT_ENABLED":"false"}).boolean(
        "COOPERATIVE_CHAT_ENABLED", True) is False


@pytest.mark.parametrize("source", ["environment", "dotenv"])
def test_explicit_retired_false_warns_without_rewriting_or_blocking_startup(source):
    import json
    import subprocess
    import sys

    code = '''
import json
import config.environment as environment
reader = environment.EnvironmentReader(
    {"COOPERATIVE_CHAT_ENABLED": "false"},
    dotenv_keys=frozenset({"COOPERATIVE_CHAT_ENABLED"}) if SOURCE == "dotenv" else frozenset(),
)
environment.load_project_environment = lambda root: reader
from config import settings
print(json.dumps({"facts": settings.retired_settings(), "input": settings.COOPERATIVE_CHAT_ENABLED}))
'''.replace('SOURCE', repr(source))
    result = subprocess.run([sys.executable, "-W", "always", "-c", code],
        capture_output=True, text=True, check=True)
    assert "retired and ignored" in result.stderr
    assert "never prohibited Work" in result.stderr
    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload == {"input": False, "facts": [{
        "key": "COOPERATIVE_CHAT_ENABLED", "value": False,
        "source": source, "effective_behavior": "cooperative_only",
    }]}


def test_environment_source_distinguishes_process_precedence_from_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CONFIG_TEST_SOURCE=dotenv\nCONFIG_TEST_OVERRIDE=dotenv\n", encoding="utf-8")
    monkeypatch.delenv("CONFIG_TEST_SOURCE", raising=False)
    monkeypatch.setenv("CONFIG_TEST_OVERRIDE", "process")
    reader = load_project_environment(tmp_path)
    assert reader.source("CONFIG_TEST_SOURCE") == "dotenv"
    assert reader.source("CONFIG_TEST_OVERRIDE") == "environment"
    assert reader.source("CONFIG_TEST_UNSET") == "default"


def test_obsolete_runtime_choices_are_evidence_only_and_planner_budgets_remain():
    import json
    import subprocess
    import sys

    code = '''
import json
import config.environment as environment
values = {
    "WORK_DELEGATE_REPAIR": "true", "DELEGATE_RESEND_ON_OMISSION": "true", "LLM_DELEGATE_TOOL_CALLS": "true",
    "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED": "true",
    "CONTROL_DECISION_SHADOW_ENABLED": "false", "CONTROL_DECISION_AUTHORITY_ENABLED": "false",
    "COMPOUND_CONTROL_AUTHORITY_ENABLED": "false", "COMPOUND_CONTROL_SHADOW_ENABLED": "true",
    "ACTION_EXISTENCE_COMMITMENT_RECOVERY_MODE": "shadow", "CONTROL_DECISION_AUTHORITY_TIMEOUT_S": "12.5",
    "DEEPSEEK_API_KEY": "synthetic-secret-never-project",
}
reader = environment.EnvironmentReader(values)
environment.load_project_environment = lambda root: reader
from config import settings
assert all(not hasattr(settings, key) for key in values if key != "DEEPSEEK_API_KEY")
assert settings.CONTROL_DECISION_PROJECT_LIMIT == 200
assert settings.CONTROL_DECISION_WORK_ITEM_LIMIT == 200
assert settings.CONTROL_DECISION_EXHAUSTIVE_CANDIDATE_LIMIT == 64
assert settings.CONTROL_DECISION_MAX_TOKENS == 900
assert settings.CONTROL_DECISION_TIMEOUT_S == 45
print(json.dumps(settings.retired_settings()))
'''
    result = subprocess.run([sys.executable, "-W", "always", "-c", code], capture_output=True, text=True, check=True)
    facts = json.loads(result.stdout.splitlines()[-1])
    assert len(facts) == 10
    assert all(row["source"] == "environment" and row["effective_behavior"] for row in facts)
    assert "synthetic-secret-never-project" not in result.stdout + result.stderr
    assert "retired and ignored" in result.stderr


def test_professional_work_planner_defaults_on_with_explicit_rollback() -> None:
    from config import settings

    field = next(field for field in settings.declared_environment_fields()
        if field.key == "COOPERATIVE_WORK_PLANNER_ENABLED")
    assert field.default is True
    model = next(field for field in settings.declared_environment_fields()
        if field.key == "COOPERATIVE_WORK_PLANNER_MODEL")
    assert model.default == ""
    assert EnvironmentReader({}).boolean("COOPERATIVE_WORK_PLANNER_ENABLED", field.default) is True
    assert EnvironmentReader({"COOPERATIVE_WORK_PLANNER_ENABLED":"false"}).boolean(
        "COOPERATIVE_WORK_PLANNER_ENABLED", field.default) is False
