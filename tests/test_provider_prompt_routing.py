from __future__ import annotations

import os
import re
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.provider_runtime import runtime as provider_runtime
from llm import prompts


def test_delegate_prompts_follow_live_provider_registration() -> None:
    with patch.object(provider_runtime, "list_providers", return_value=[]):
        assert prompts.registered_provider_ids() == (), (
            "prompt construction must not invent providers before registration"
        )

    with (
        patch("tts.pipeline.TTS_OUTPUT_LANGUAGE", "英文"),
        patch(
            "llm.prompts.registered_provider_ids",
            return_value=("browser", "codex", "openclaw"),
        ),
    ):
        codex_only = {
            "runtime_with_delegate": prompts.get_system_prompt("with_delegate"),
            "runtime_bedrock": prompts.get_system_prompt("bedrock"),
        }
    for name, prompt in codex_only.items():
        assert "Currently registered provider ids: browser, codex, openclaw" in prompt, name
        assert 'provider="codex"' in prompt, name
        assert 'provider="locus"' not in prompt, name
        assert 'Coding role: provider="codex"' in prompt, name

    representative = codex_only["runtime_with_delegate"]
    assert 'Coding role: provider="codex"' in representative
    assert 'force_provider="user"' in representative
    assert "explicitly chooses one registered provider" in representative
    assert "Never infer force_provider" in representative
    assert "live page state must be retained or manipulated" in representative
    assert "Host-configured role assignments" in representative
    assert "Never invent a URL merely to select Browser" in representative
    assert "without that evidence is Agent research" in representative
    assert "continues the export-owning WorkItem" in representative
    assert "not the Session's current Project source" in representative

    variants = codex_only
    for name, prompt in variants.items():

        # The persona used to describe delegation as "you have OpenClaw
        # connected; use it only when an external tool is needed" — a world
        # model that predates Codex, stated first, at length, in the persona's
        # own language. It beat the English routing addon appended after it:
        # first-turn creation emitted the tag 6/6 while an anaphoric follow-up
        # edit emitted 1/6 (2026-07-31 A/B). Delegation must stay a default
        # with one enumerated read-only exception.
        for banned in (
            "外部ツールが必要な時だけ",
            "Only when an external tool is needed",
            "AIアシスタント「OpenClaw」が接続されており",
            "You have an AI assistant called 'OpenClaw' connected",
        ):
            assert banned not in prompt, (name, banned)

        # Examples are imitated more reliably than the rule that governs them,
        # so every worked example must carry a provider attribute.
        for example in re.findall(r"\[DELEGATE[^\]]*\]", prompt):
            assert "provider=" in example, (name, example)

    # The failing code/edit case must appear as a worked example, not only as a rule.
    assert "theme.txt" in codex_only["runtime_with_delegate"]


def test_provider_routing_wording_tracks_the_output_language() -> None:
    providers = ("browser", "codex", "openclaw")
    with (
        patch("tts.pipeline.TTS_OUTPUT_LANGUAGE", "日文"),
        patch("llm.prompts.registered_provider_ids", return_value=providers),
    ):
        japanese = prompts.get_system_prompt("with_delegate")
    with (
        patch("tts.pipeline.TTS_OUTPUT_LANGUAGE", "英文"),
        patch("llm.prompts.registered_provider_ids", return_value=providers),
    ):
        english = prompts.get_system_prompt("with_delegate")

    assert "現在登録されている provider id" in japanese
    assert 'Coding 担当: provider="codex"' in japanese
    assert "legacy code provider" not in japanese
    assert "Currently registered provider ids" not in japanese
    assert "Currently registered provider ids" in english
    assert 'Coding role: provider="codex"' in english
    assert "現在登録されている provider id" not in english
    for prompt in (japanese, english):
        assert 'provider="codex"' in prompt
        assert 'provider="locus"' not in prompt
        assert 'force_provider="user"' in prompt
