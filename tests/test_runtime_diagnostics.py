"""Production diagnostics describe events while retaining level and privacy."""
import logging
from types import SimpleNamespace
import sys

import pytest

from tools.maintainability_ratchet import PLACEHOLDER, ROOT, python_sources


def test_no_placeholder_logs_in_runtime_including_bundled_inference():
    found = [p.relative_to(ROOT).as_posix() for p in python_sources(ROOT, include_bundled=True)
             if PLACEHOLDER in p.read_text(encoding="utf-8-sig")]
    assert not found, f"Describe the runtime event and safe metadata: {found}"


def test_bedrock_initialization_failure_keeps_warning_without_exception_payload(monkeypatch, caplog):
    from llm import client
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-secret https://private.invalid/?token=synthetic-token")
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=fail))
    monkeypatch.setattr(client, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(client, "AWS_BEDROCK_AUTH_MODE", "boto3")
    monkeypatch.setattr(client, "bedrock_runtime_client", None)
    caplog.set_level(logging.WARNING, logger=client.__name__)
    assert client.init_llm_client() == "bedrock_client"
    record = next(r for r in caplog.records if "SDK client initialization failed" in r.message)
    assert record.levelno == logging.WARNING
    assert "RuntimeError" in record.message
    assert record.exc_info is None
    assert "synthetic-secret" not in caplog.text and "private.invalid" not in caplog.text


@pytest.mark.parametrize("profile", [False, True])
def test_bedrock_logs_routing_facts_and_configured_not_effective_cache_ttl(monkeypatch, caplog, profile):
    from llm import client
    for key, value in {
        "LLM_PROVIDER": "bedrock", "AWS_BEDROCK_AUTH_MODE": "boto3",
        "bedrock_runtime_client": object(), "AWS_BEDROCK_REGION": "us-test-1",
        "AWS_BEDROCK_MODEL_ID": "synthetic-model", "AWS_BEDROCK_INFERENCE_PROFILE_ID": "synthetic-profile",
        "AWS_BEDROCK_USE_INFERENCE_PROFILE": profile, "AWS_BEDROCK_USE_CACHE": True,
        "AWS_BEDROCK_CACHE_TTL": 3600, "AWS_BEDROCK_BEARER_TOKEN": "synthetic-secret",
    }.items():
        monkeypatch.setattr(client, key, value)
    caplog.set_level(logging.INFO, logger=client.__name__)
    assert client.init_llm_client() == "bedrock_client"
    assert "region=us-test-1" in caplog.text
    assert f"model={'synthetic-profile' if profile else 'synthetic-model'}" in caplog.text
    assert f"route={'inference_profile' if profile else 'model'}" in caplog.text
    assert "configured_ttl_seconds=3600" in caplog.text
    assert "does not send cache controls" in caplog.text
    assert "synthetic-secret" not in caplog.text
