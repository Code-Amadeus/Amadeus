from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from llm.local_backends import (
    hybrid_local_status,
    local_backend_status,
    local_chat_url,
    ollama_chat_url,
    openai_chat_url,
    should_manage_local_server,
)


def _settings(**overrides):
    values = {
        "LLM_PROVIDER": "deepseek",
        "LOCAL_LLM_TYPE": "llama_server",
        "LOCAL_LLM_LAUNCH_MODE": "external",
        "LOCAL_LLM_URL": "http://127.0.0.1:8080/v1",
        "LOCAL_LLM_LM_STUDIO_URL": "http://127.0.0.1:1234",
        "LOCAL_LLM_OLLAMA_URL": "http://127.0.0.1:11434",
        "LOCAL_LLM_CLI_PATH": "",
        "LOCAL_LLM_MODEL_PATH": "",
        "HYBRID_LOCAL_LLM_URL": "http://127.0.0.1:8080/v1",
        "HYBRID_LOCAL_LLM_MODEL": "head-model",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_local_backend_profiles_build_type_specific_urls() -> None:
    assert openai_chat_url("http://127.0.0.1:8080") == (
        "http://127.0.0.1:8080/v1/chat/completions"
    )
    assert openai_chat_url("http://127.0.0.1:8080/v1/") == (
        "http://127.0.0.1:8080/v1/chat/completions"
    )
    assert ollama_chat_url("http://127.0.0.1:11434/v1") == (
        "http://127.0.0.1:11434/api/chat"
    )
    assert ollama_chat_url("http://127.0.0.1:11434/api/chat") == (
        "http://127.0.0.1:11434/api/chat"
    )
    assert local_chat_url(
        "ollama",
        llama_server_url="http://127.0.0.1:8080/v1",
        lmstudio_url="http://127.0.0.1:1234",
        ollama_url="http://127.0.0.1:11434",
    ) == "http://127.0.0.1:11434/api/chat"


def test_managed_server_is_owned_only_by_the_pure_local_profile() -> None:
    assert should_manage_local_server(
        _settings(
            LLM_PROVIDER="local",
            LOCAL_LLM_TYPE="llama_server",
            LOCAL_LLM_LAUNCH_MODE="managed",
        )
    )
    assert not should_manage_local_server(
        _settings(
            LLM_PROVIDER="hybrid",
            LOCAL_LLM_TYPE="llama_server",
            LOCAL_LLM_LAUNCH_MODE="managed",
        )
    )
    assert not should_manage_local_server(
        _settings(
            LLM_PROVIDER="local",
            LOCAL_LLM_TYPE="ollama",
            LOCAL_LLM_LAUNCH_MODE="managed",
        )
    )


def test_local_status_distinguishes_reachable_and_managed_missing_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class Response:
        status_code = 200

    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: Response())
    reachable = local_backend_status(_settings(), project_root=tmp_path)
    assert reachable["state"] == "available"
    assert reachable["available"] is True

    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", unavailable)
    missing = local_backend_status(
        _settings(LOCAL_LLM_LAUNCH_MODE="managed"),
        project_root=tmp_path,
    )
    assert missing["state"] == "not_configured"
    assert "executable" in missing["detail"]

    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: Response())
    hybrid = hybrid_local_status(_settings())
    assert hybrid["available"] is True
    assert "Hybrid local head" in hybrid["detail"]


def test_cli_installation_does_not_claim_cooperative_message_support(tmp_path: Path) -> None:
    executable = tmp_path / "llama-cli.exe"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"exe")
    model.write_bytes(b"gguf")
    values = _settings(LOCAL_LLM_TYPE="cli", LOCAL_LLM_CLI_PATH=str(executable),
                       LOCAL_LLM_MODEL_PATH=str(model), COOPERATIVE_CHAT_ENABLED=True)
    status = local_backend_status(values, project_root=tmp_path)
    assert status["configured"] is True
    assert status["available"] is False
    assert status["state"] == "unsupported"
    assert "llama_server" in status["detail"]
    values.COOPERATIVE_CHAT_ENABLED = False
    assert local_backend_status(values, project_root=tmp_path)["state"] == "unsupported"


@pytest.mark.parametrize("explicit,local_context,hybrid_context", [
    (None, "16384", "4096"), ("4096", "4096", "4096"),
    ("8192", "8192", "8192"),
])
def test_managed_context_defaults_are_profile_specific(
    tmp_path, explicit, local_context, hybrid_context,
):
    import json
    import subprocess
    import sys

    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    executable.touch()
    model.touch()
    values = {"LOCAL_LLM_CLI_PATH": str(executable), "LOCAL_LLM_CLI_MODEL_PATH": str(model)}
    if explicit is not None:
        values["LOCAL_LLM_CLI_CONTEXT"] = explicit
    # Fresh imports and an explicit reader exclude the developer's .env. Test
    # real settings -> BAT command construction, not a patched argument list.
    code = '''
import json
import config.environment as environment
reader = environment.EnvironmentReader(VALUES)
environment.load_project_environment = lambda root: reader
from llm.llama_server import build_llama_server_command
commands = {profile: build_llama_server_command(profile) for profile in ("local", "hybrid")}
print(json.dumps({key: args[args.index("-c") + 1] for key, args in commands.items()}))
'''.replace("VALUES", repr(values))
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
        capture_output=True, text=True, check=True)
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "local": local_context, "hybrid": hybrid_context,
    }


def test_llama_server_command_uses_profile_endpoint_and_no_bat_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from llm import llama_server

    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"exe")
    model.write_bytes(b"gguf")
    monkeypatch.setattr(llama_server, "LOCAL_LLM_CLI_PATH", str(executable))
    monkeypatch.setattr(llama_server, "LOCAL_LLM_MODEL_PATH", str(model))
    monkeypatch.setattr(llama_server, "LOCAL_LLM_URL", "http://127.0.0.1:9000/v1")
    monkeypatch.setattr(llama_server, "LOCAL_LLM_MODEL", "local-model")
    monkeypatch.setattr(
        llama_server,
        "HYBRID_LOCAL_LLM_URL",
        "http://127.0.0.1:9100/v1",
    )
    monkeypatch.setattr(llama_server, "HYBRID_LOCAL_LLM_MODEL", "hybrid-head")
    monkeypatch.setattr(
        llama_server,
        "LOCAL_LLM_SERVER_ARGS",
        ["-m", "old.gguf", "--port", "8080", "-a", "old-model"],
    )

    local = llama_server.build_llama_server_command("local")
    hybrid = llama_server.build_llama_server_command("hybrid")

    assert local[0] == str(executable)
    assert local[local.index("-m") + 1] == str(model)
    assert local[local.index("--port") + 1] == "9000"
    assert local[local.index("-a") + 1] == "local-model"
    assert local[local.index("--parallel") + 1] == "1"
    assert "--no-context-shift" in local
    assert hybrid[hybrid.index("--port") + 1] == "9100"
    assert hybrid[hybrid.index("-a") + 1] == "hybrid-head"
    assert "--parallel" not in hybrid
    assert "--no-context-shift" not in hybrid

    project_root = Path(__file__).resolve().parents[1]
    local_bat = (project_root / "start_llm_server.bat").read_text(encoding="utf-8")
    hybrid_bat = (project_root / "start_hybrid_llm.bat").read_text(encoding="utf-8")
    assert "--profile local" in local_bat
    assert "--profile hybrid" in hybrid_bat
    assert "D:\\" not in local_bat + hybrid_bat
    assert "F:\\" not in local_bat + hybrid_bat
