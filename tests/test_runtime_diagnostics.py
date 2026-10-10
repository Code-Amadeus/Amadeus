"""Production diagnostics describe events while retaining level and privacy."""
import logging
from types import SimpleNamespace
import sys

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
