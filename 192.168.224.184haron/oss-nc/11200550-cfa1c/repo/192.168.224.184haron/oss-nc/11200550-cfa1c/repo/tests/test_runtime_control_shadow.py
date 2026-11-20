"""Shared control prompt and message transport preserve source roles."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_control_prompt_augmentation_excludes_reference_rosters() -> None:
    from server.work_context import augment_system_prompt_for_control_decision

    with (
        patch(
            "server.work_context.render_active_provider_context",
            return_value="active provider fact",
        ),
        patch(
            "server.work_context.render_branch_routing_context",
            return_value="[Active browser branch]\nbranch fact",
        ),
        patch(
            "server.work_context.render_workspace_routing_context",
            return_value="Project identities are withheld",
        ) as project_renderer,
        patch(
            "server.work_context.render_conversation_work_context",
            return_value="WorkItem identities are withheld",
        ) as work_renderer,
    ):
        prompt = augment_system_prompt_for_control_decision(
            "base control contract",
            session_id="session-shadow",
        )

    assert "base control contract" in prompt
    assert "active provider fact" in prompt
    assert "branch fact" in prompt
    assert "Project identities are withheld" in prompt
    assert "WorkItem identities are withheld" in prompt
    assert project_renderer.call_args.kwargs["include_candidates"] is False
    assert work_renderer.call_args.kwargs["include_candidates"] is False


def test_message_query_preserves_roles_for_the_control_backend() -> None:
    from llm import client

    calls = []

    class Completions:
        @staticmethod
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"decisions":[]}'))]
            )

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    messages = [
        {"role": "system", "content": "control"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "prior"},
        {"role": "user", "content": "current"},
    ]
    with (
        patch.object(client, "LLM_PROVIDER", "deepseek"),
        patch.object(client, "llm_client", fake_client),
    ):
        reply = client.remote_llm_messages_query(messages, temperature=0.0)
    assert reply == '{"decisions":[]}'
    assert calls[0]["messages"] == messages
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["response_format"] == {"type": "json_object"}
