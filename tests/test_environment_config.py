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


def test_default_gemini_model_is_current() -> None:
    from config import settings

    gemini_model = next(
        field
        for field in settings.declared_environment_fields()
        if field.key == "GEMINI_MODEL_NAME"
    )

    assert gemini_model.default == "gemini-3.6-flash"
