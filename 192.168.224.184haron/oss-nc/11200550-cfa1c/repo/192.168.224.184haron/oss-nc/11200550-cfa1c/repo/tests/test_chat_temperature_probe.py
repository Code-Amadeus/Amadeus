"""Guard the live probe's history fidelity and treatment independence."""
import asyncio
import json
from pathlib import Path

import pytest

from tools.probes.compare_chat_temperature import (
    CapturedMessages, capture_messages, digest, ingress_history, load_baseline,
    select_system, transform_system,
)

BASELINE = Path(__file__).parent / "fixtures/issue227_prompt_baseline.json"


def test_probe_uses_session_history_projection_without_current_turn_replay():
    from agent_host.provider_runtime import ProviderRuntime
    from server.cooperative_provider_loop import CooperativeProviderLoop

    history = [
        {"role": "user", "content": "前の質問", "turn_id": "prior"},
        {"role": "assistant", "content": "前の回答", "turn_id": "prior"},
        {"role": "user", "content": "今回の質問", "turn_id": "current"},
        {"role": "system", "content": "not public dialogue"},
        {"role": "assistant", "content": ""},
    ]
    projected = ingress_history(history, "current")

    async def capture():
        loop = CooperativeProviderLoop(ProviderRuntime(), capture_messages,
            lambda *_: (_ for _ in ()).throw(AssertionError("execution forbidden")),
            provider="pi", context_requirements={}, persona="probe persona",
            owns_runtime=False, work_proposals_only=True)
        try:
            await loop._decide({"source": "user", "text": "今回の質問", "turn_id": "current"},
                turn_history=projected)
        except CapturedMessages as result:
            return result.messages
        finally:
            await loop.close()
        raise AssertionError("model boundary not captured")

    messages = asyncio.run(capture())
    frame = json.loads(messages[-1]["content"])
    assert frame["history"] == [
        {"source": "user", "text": "前の質問", "input_id": "prior"},
        {"source": "kurisu", "text": "前の回答", "cause": "prior"},
    ]
    assert frame["current"]["text"] == "今回の質問"
    assert len(messages) == 2


def test_preface_factorial_separates_examples_from_directions():
    system = load_baseline(BASELINE)["system"]
    examples = transform_system(system, "preface_examples_only")
    directions = transform_system(system, "preface_directions_only")
    combined = transform_system(system, "both_no_preface")

    assert examples != system and directions != system and examples != directions
    assert transform_system(examples, "preface_directions_only") == combined
    assert transform_system(directions, "preface_examples_only") == combined


def test_adopted_production_matches_evaluated_shared_candidate(monkeypatch):
    from agent_host.provider_runtime import ProviderRuntime
    from llm import prompts
    from server import assistant_language
    from server.cooperative_provider_loop import CooperativeProviderLoop

    monkeypatch.setenv("AMADEUS_CHARACTER_ID", "kurisu")
    monkeypatch.setenv("AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA", "")
    monkeypatch.setattr(prompts, "_character_prompt_ja", "")
    monkeypatch.setattr(assistant_language, "current_assistant_language", lambda: "japanese")

    async def capture():
        loop = CooperativeProviderLoop(ProviderRuntime(), capture_messages,
            lambda *_: (_ for _ in ()).throw(AssertionError("execution forbidden")),
            provider="pi", context_requirements={}, persona=prompts.get_system_prompt("base"),
            owns_runtime=False, work_proposals_only=True)
        try:
            await loop._decide({"source": "user", "text": "3は素数？", "turn_id": "probe"}, turn_history=())
        except CapturedMessages as result:
            return result.messages[0]["content"]
        finally:
            await loop.close()
        raise AssertionError("model boundary not captured")

    production = asyncio.run(capture())
    baseline = load_baseline(BASELINE)
    assert digest(production) == baseline["adopted_system_sha256"]
    assert production == select_system(production, "both_contextual_reaction", baseline)
    assert select_system(production, "production", baseline) == production
    assert select_system(production, "original", baseline) == baseline["system"]
    assert production != baseline["system"]
    with pytest.raises(ValueError, match="require --baseline-system"):
        select_system(production, "original")
