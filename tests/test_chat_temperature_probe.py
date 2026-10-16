"""Guard the live probe's history fidelity and treatment independence."""
import asyncio
import json

from tools.probes.compare_chat_temperature import (
    CapturedMessages, capture_messages, ingress_history, transform_system,
)


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
    from llm.character_prompts import text
    from llm.prompts import _JA_LANGUAGE_RULES, _JA_OUTPUT_RULES
    from server.cooperative_provider_loop import _role_coordination_contract

    character_rule = text("ja_character_rule", character_id="kurisu")
    system = (text("ja_identity", character_id="kurisu") + _JA_LANGUAGE_RULES
        + "3) " + character_rule + _JA_OUTPUT_RULES + "\n\n"
        + _role_coordination_contract(True, character_id="kurisu"))
    examples = transform_system(system, "preface_examples_only")
    directions = transform_system(system, "preface_directions_only")
    combined = transform_system(system, "both_no_preface")

    assert examples != system and directions != system and examples != directions
    assert transform_system(examples, "preface_directions_only") == combined
    assert transform_system(directions, "preface_examples_only") == combined
    assert all(character_rule in candidate for candidate in (system, examples, directions, combined))
