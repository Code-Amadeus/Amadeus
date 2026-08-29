from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest

from agent_host.codex_desktop_provider import (
    CodexDesktopProviderConflict,
    build_codex_desktop_provider_bridge,
    provider_auth_overrides,
)
from tools.codex_provider_auth import provider_token


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _bridge(config_path: Path, env_file: Path):
    return build_codex_desktop_provider_bridge(
        provider_id="deepseek",
        base_url="https://api.deepseek.com/",
        api_key_env="DEEPSEEK_API_KEY",
        project_root=PROJECT_ROOT,
        env_file=env_file,
        python_executable="C:/Python/python.exe",
        config_path=config_path,
    )


def test_bridge_adds_command_auth_without_copying_the_secret() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-provider-bridge-") as temp_dir:
        root = Path(temp_dir)
        env_file = root / ".env"
        env_file.write_text("DEEPSEEK_API_KEY=private-test-token\n", encoding="utf-8")
        bridge = _bridge(root / "config.toml", env_file)

        assert bridge is not None
        assert bridge.needs_write is True
        assert bridge.definition["auth"]["command"] == "C:/Python/python.exe"
        assert bridge.definition["auth"]["args"][-1] == "DEEPSEEK_API_KEY"
        assert "private-test-token" not in repr(bridge.definition)
        overrides = provider_auth_overrides(bridge)
        assert any("auth.command" in value for value in overrides)
        assert all("env_key=" not in value for value in overrides)


def test_bridge_does_not_replace_a_conflicting_user_provider() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-provider-conflict-") as temp_dir:
        root = Path(temp_dir)
        config_path = root / "config.toml"
        config_path.write_text(
            "[model_providers.deepseek]\n"
            'name = "My provider"\n'
            'base_url = "https://different.example/v1"\n'
            'env_key = "MY_KEY"\n',
            encoding="utf-8",
        )

        with pytest.raises(CodexDesktopProviderConflict):
            _bridge(config_path, root / ".env")


def test_bridge_accepts_a_compatible_user_owned_provider() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-provider-compatible-") as temp_dir:
        root = Path(temp_dir)
        config_path = root / "config.toml"
        config_path.write_text(
            "[model_providers.deepseek]\n"
            'name = "My DeepSeek"\n'
            'base_url = "https://api.deepseek.com"\n'
            'env_key = "DEEPSEEK_API_KEY"\n',
            encoding="utf-8",
        )
        bridge = _bridge(config_path, root / ".env")

        assert bridge is not None
        assert bridge.needs_write is False
        assert bridge.definition["name"] == "My DeepSeek"


def test_managed_provider_refresh_replaces_the_complete_table() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-provider-refresh-") as temp_dir:
        root = Path(temp_dir)
        config_path = root / "config.toml"
        config_path.write_text(
            "[model_providers.deepseek]\n"
            'name = "deepseek (managed by Amadeus)"\n'
            'base_url = "https://old.example/v1"\n'
            'env_key = "STALE_KEY"\n',
            encoding="utf-8",
        )
        bridge = _bridge(config_path, root / ".env")

        assert bridge is not None and bridge.needs_write is True
        assert bridge.config_edit()["mergeStrategy"] == "replace"
        assert "env_key" not in bridge.definition


def test_builtin_codex_provider_needs_no_desktop_bridge() -> None:
    bridge = build_codex_desktop_provider_bridge(
        provider_id="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        project_root=PROJECT_ROOT,
        env_file=PROJECT_ROOT / ".env",
    )
    assert bridge is None


def test_provider_token_prefers_process_environment_then_dotenv() -> None:
    with tempfile.TemporaryDirectory(prefix="amadeus-provider-token-") as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text('DEEPSEEK_API_KEY="test-dotenv-token"\n', encoding="utf-8")
        previous = os.environ.get("DEEPSEEK_API_KEY")
        try:
            os.environ["DEEPSEEK_API_KEY"] = "test-process-token"
            assert provider_token(
                env_key="DEEPSEEK_API_KEY",
                env_file=env_file,
            ) == "test-process-token"
            os.environ.pop("DEEPSEEK_API_KEY")
            assert provider_token(
                env_key="DEEPSEEK_API_KEY",
                env_file=env_file,
            ) == "test-dotenv-token"
        finally:
            if previous is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = previous
