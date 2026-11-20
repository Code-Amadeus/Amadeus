"""Model configuration remains authoritative for every lifecycle/status reader."""

import pytest

from config import settings
from llm import client
from llm.local_backends import should_manage_local_server
from server import auip_narration_llm, work_observer_llm


@pytest.fixture(autouse=True)
def restore_configuration(monkeypatch):
    for module in (settings, client):
        for name in ("LLM_PROVIDER", "LOCAL_LLM_TYPE"):
            monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(client, "llm_client", None)
    for name, value in {
        "LOCAL_LLM_LAUNCH_MODE": "managed", "WORK_OBSERVER_PROVIDER": "",
        "AUIP_NARRATION_PROVIDER": "", "DEEPSEEK_API_KEY": "", "OPENAI_API_KEY": "test",
    }.items():
        monkeypatch.setattr(settings, name, value)


def test_configuration_reaches_lifecycle_and_auxiliary_model_readers(monkeypatch):
    client.configure(llm_provider="local", local_llm_type="llama_server")
    assert should_manage_local_server(settings)
    assert client.LLM_PROVIDER == work_observer_llm._provider() == auip_narration_llm._provider() == "local"
    assert settings.LLM_PROVIDER == "local" and settings.LOCAL_LLM_TYPE == "llama_server"
    client.configure(llm_provider="openai", local_llm_type="lmstudio")
    assert not should_manage_local_server(settings)
    assert client.LLM_PROVIDER == work_observer_llm._provider() == auip_narration_llm._provider() == "openai"
    assert settings.LLM_PROVIDER == "openai" and settings.LOCAL_LLM_TYPE == "lmstudio"
    monkeypatch.setattr(settings, "WORK_OBSERVER_PROVIDER", "deepseek")
    monkeypatch.setattr(settings, "AUIP_NARRATION_PROVIDER", "bedrock")
    assert work_observer_llm._provider() == "deepseek"
    assert auip_narration_llm._provider() == "bedrock"


def test_invalid_local_type_cannot_partially_change_provider():
    before = (client.LLM_PROVIDER, settings.LLM_PROVIDER, client.LOCAL_LLM_TYPE)
    with pytest.raises(ValueError, match="unsupported local LLM type"):
        client.configure(llm_provider="local", local_llm_type="invalid")
    assert (client.LLM_PROVIDER, settings.LLM_PROVIDER, client.LOCAL_LLM_TYPE) == before
