"""Provider-neutral role-reference handoff contracts."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_identity import (
    MAIN_ROLE_NAME_METADATA_KEY,
    with_main_role_reference,
)
from agent_host.adapters.codex_app_server import CodexAppServerAdapter
from agent_host.provider_types import ProviderRunRequest


def test_missing_role_identity_leaves_the_provider_payload_unchanged() -> None:
    task = "Create a small website."
    assert (
        with_main_role_reference(
            task,
            metadata={},
            execution_provider="codex",
        )
        == task
    )


def test_role_reference_context_preserves_payload_and_separates_identities() -> None:
    task = "你能做一个关于你自己的网页吗？"
    rendered = with_main_role_reference(
        task,
        metadata={MAIN_ROLE_NAME_METADATA_KEY: "Makise Kurisu (牧瀬紅莉栖)"},
        execution_provider="codex",
    )

    assert rendered.startswith(task + "\n\n")
    assert 'main role is "Makise Kurisu (牧瀬紅莉栖)"' in rendered
    assert 'execution Provider is "codex"' in rendered
    assert "'你自己'" in rendered
    assert "never overrides another explicitly named subject" in rendered


def test_role_reference_context_is_provider_neutral() -> None:
    metadata = {MAIN_ROLE_NAME_METADATA_KEY: "Makise Kurisu (牧瀬紅莉栖)"}
    for provider in ("codex", "openclaw", "locus", "future-agent"):
        rendered = with_main_role_reference(
            "Create the requested artifact.",
            metadata=metadata,
            execution_provider=provider,
        )
        assert f'execution Provider is "{provider}"' in rendered


def test_codex_model_context_is_enriched_without_changing_durable_task() -> None:
    task = "你能做一个关于你自己的网页吗？"
    request = ProviderRunRequest(
        provider="codex",
        task=task,
        metadata={MAIN_ROLE_NAME_METADATA_KEY: "Makise Kurisu (牧瀬紅莉栖)"},
    )
    adapter = CodexAppServerAdapter(sync_desktop_provider=False)

    model_context = adapter._task_text(request)

    assert request.task == task
    assert model_context.startswith(task)
    assert "[Amadeus role-reference context]" in model_context
    assert 'execution Provider is "codex"' in model_context
