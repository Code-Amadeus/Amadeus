from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx

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
    assert hybrid[hybrid.index("--port") + 1] == "9100"
    assert hybrid[hybrid.index("-a") + 1] == "hybrid-head"

    project_root = Path(__file__).resolve().parents[1]
    local_bat = (project_root / "start_llm_server.bat").read_text(encoding="utf-8")
    hybrid_bat = (project_root / "start_hybrid_llm.bat").read_text(encoding="utf-8")
    assert "--profile local" in local_bat
    assert "--profile hybrid" in hybrid_bat
    assert "D:\\" not in local_bat + hybrid_bat
    assert "F:\\" not in local_bat + hybrid_bat
