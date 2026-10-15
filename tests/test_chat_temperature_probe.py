"""Guard the live probe's history fidelity at the production ingress boundary."""
import asyncio
import json

from tools.probes.compare_chat_temperature import CapturedMessages, capture_messages, ingress_history


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
