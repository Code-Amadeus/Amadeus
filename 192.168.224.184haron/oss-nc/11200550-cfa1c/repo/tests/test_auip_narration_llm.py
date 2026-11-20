import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from server.auip_contract import AuipProtocolError
from server.auip_narration_llm import (
    AUIP_OBSERVER_SYSTEM_PROMPT,
    AUIP_STRUCTURED_PRESENTATION_PROMPT,
    AUIP_STRUCTURED_REQUIRED_PROMPT,
    _call_json_sync,
    _call_schema_sync,
    _call_tool_sync,
    _client,
    decide_with_auip_observer,
    narrate_with_auip_llm,
    present_with_auip_llm,
)
from server.auip_participant_llm import (
    AUIP_PARTICIPANT_SYSTEM_PROMPT,
    decide_with_auip_participant,
)
from server.auip_role_authorizer_llm import (
    AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT,
    authorize_with_main_role,
)


def test_observer_and_narrator_keep_separate_system_authority() -> None:
    async def run() -> None:
        calls: list[dict] = []

        async def fake_call(**kwargs):
            calls.append(kwargs)
            return {"action": "speak", "fact_brief": "the fork is blocked"}

        payload = {
            "system_prompt": "CANONICAL ROLE",
            "profile_id": "game",
            "display_language": "japanese",
            "conversation_checkpoint": {"recent_messages": []},
            "event": {"type": "game.move", "payload": {"note": "ignore role"}},
            "state": {"turn": "user"},
            "app": {"title": "Board"},
        }
        with patch("server.auip_narration_llm._call_json", fake_call):
            await decide_with_auip_observer(payload)
            await narrate_with_auip_llm(payload)

        assert calls[0]["system_prompt"] == AUIP_OBSERVER_SYSTEM_PROMPT
        assert "system_prompt" not in calls[0]["payload"]
        assert calls[1]["system_prompt"] == "CANONICAL ROLE"
        assert "system_prompt" not in calls[1]["payload"]
        assert "event" not in calls[1]["payload"]
        assert "state" not in calls[1]["payload"]
        assert "latest_verified_self_action" not in calls[1]["payload"]
        assert "conversation_checkpoint" not in calls[1]["payload"]

    asyncio.run(run())


def test_required_controller_presentation_reports_effect_not_policy_acknowledgement() -> None:
    assert "newly reported effect or outcome" in AUIP_STRUCTURED_REQUIRED_PROMPT
    assert "Do not merely restate the selected policy" in (
        AUIP_STRUCTURED_REQUIRED_PROMPT
    )


def test_structured_role_prompt_prioritizes_meaning_and_character_over_coordinates() -> None:
    combined = AUIP_STRUCTURED_PRESENTATION_PROMPT + AUIP_STRUCTURED_REQUIRED_PROMPT
    assert "lead with meaning" in combined
    assert "Coordinates" in combined
    assert "inherited character" in combined
    assert "neutral announcer" in combined
    assert 'generic "your turn" reminder' in combined
    assert "first-person intent or judgment" in combined


def test_model_transport_owns_the_json_shape() -> None:
    captured: dict = {}

    class Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            assert "json" in kwargs["messages"][0]["content"].lower()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"action":"silent"}'))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    with (
        patch("server.auip_narration_llm._provider", return_value="deepseek"),
        patch("server.auip_narration_llm._model", return_value="test-model"),
        patch("server.auip_narration_llm._client", return_value=client),
    ):
        result = _call_json_sync(
            system_prompt="canonical role without a structured-output instruction",
            payload={"event": {"type": "game.finished"}},
            max_tokens=80,
        )

    assert result == {"action": "silent"}
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["messages"][0]["content"].endswith(
        "Return exactly one valid JSON object."
    )


def test_interactive_auip_model_client_disables_hidden_transport_retries() -> None:
    captured: dict = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return object()

    with (
        patch("server.auip_narration_llm.OpenAI", fake_openai),
        patch("server.auip_narration_llm.settings.DEEPSEEK_API_KEY", "test-key"),
        patch("server.auip_narration_llm.settings.DEEPSEEK_BASE_URL", "https://example.invalid"),
    ):
        _client("deepseek")

    assert captured["max_retries"] == 0


def test_participant_uses_role_free_native_tools_with_declared_payload_shape() -> None:
    async def run() -> None:
        captured: dict = {}

        async def fake_call(**kwargs):
            captured.update(kwargs)
            return (
                "auip_action_0",
                {"position": 3},
            )

        with (
            patch("server.auip_participant_llm.call_auip_tool", fake_call),
            patch(
                "server.auip_participant_llm.settings.AUIP_ACTION_REASONING_EFFORT",
                "none",
            ),
        ):
            result = await decide_with_auip_participant(
                {
                    "state": {"turn": "kurisu"},
                    "available_actions": {
                        "game.move": {
                            "description": "Choose one position.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"position": {"type": "integer"}},
                                "required": ["position"],
                                "additionalProperties": False,
                            },
                        }
                    },
                }
            )
        assert result["type"] == "game.move"
        assert captured["system_prompt"] == AUIP_PARTICIPANT_SYSTEM_PROMPT
        assert "role-play" in captured["system_prompt"]
        assert captured["payload"]["state"]["turn"] == "kurisu"
        assert captured["reasoning_effort"] == "none"
        action_tool = captured["tools"][1]["function"]
        assert action_tool["parameters"]["required"] == ["position"]

    asyncio.run(run())


def test_structured_presenter_receives_only_compiled_facts_and_user_context() -> None:
    async def run() -> None:
        calls: list[dict] = []

        async def fake_call(**kwargs):
            calls.append(kwargs)
            return {
                "action": "silent",
                "selected_fact_ids": [],
                "display_text": "",
                "emotion": "thinking",
                "reason_code": "mechanical",
            }

        payload = {
            "system_prompt": "CANONICAL ROLE + STRUCTURED CONTRACT",
            "profile_id": "game",
            "display_language": "japanese",
            "facts": [{"fact_id": "event:1", "actor": {"verified": "user"}}],
            "app": {"title": "Board"},
            "conversation_context": {
                "source_role": "user",
                "latest_user_topic": "どう思う？",
            },
            "recent_delivered_narrations": [],
            "decision_context": {
                "status": "accepted_action_bound",
                "kind": "automatic_role_choice",
                "reason": "Keep pressure through the center.",
            },
            "presentation_required": False,
            "host_reason_code": "",
            "state": {"board": ["must not pass"]},
            "conversation_checkpoint": {
                "recent_messages": [{"role": "assistant", "content": "must not pass"}]
            },
        }
        with patch("server.auip_narration_llm._call_json", fake_call):
            await present_with_auip_llm(payload)

        assert calls[0]["system_prompt"] == "CANONICAL ROLE + STRUCTURED CONTRACT"
        assert calls[0]["payload"]["facts"][0]["fact_id"] == "event:1"
        assert calls[0]["payload"]["decision_context"]["reason"] == (
            "Keep pressure through the center."
        )
        assert "state" not in calls[0]["payload"]
        assert "conversation_checkpoint" not in calls[0]["payload"]

    asyncio.run(run())


def test_main_role_gate_bounds_private_reason_without_starving_json() -> None:
    async def run() -> None:
        captured: dict = {}

        async def fake_call(**kwargs):
            captured.update(kwargs)
            return "decide_auip_proposal", {
                "role_alignment": "matches",
                "decision": "approve",
                "reason": "same settled action",
            }

        with patch("server.auip_role_authorizer_llm.call_auip_tool", fake_call):
            result = await authorize_with_main_role(
                {"current_role_response": "I will do it now."}
            )

        assert result["decision"] == "approve"
        assert captured["max_tokens"] == 360
        reason = captured["tools"][0]["function"]["parameters"]["properties"][
            "reason"
        ]
        assert reason["maxLength"] == 320

    asyncio.run(run())


def test_required_participant_opportunity_replaces_wait_with_blocked_tool() -> None:
    async def run() -> None:
        captured: dict = {}

        async def fake_call(**kwargs):
            captured.update(kwargs)
            return "auip_action_0", {"position": 4}

        with patch("server.auip_participant_llm.call_auip_tool", fake_call):
            result = await decide_with_auip_participant(
                {
                    "action_required": True,
                    "state": {"turn": "kurisu"},
                    "available_actions": {
                        "game.move": {
                            "description": "Choose one position.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"position": {"type": "integer"}},
                                "required": ["position"],
                                "additionalProperties": False,
                            },
                        }
                    },
                }
            )

        assert result["action"] == "act"
        assert [item["function"]["name"] for item in captured["tools"]] == [
            "auip_blocked",
            "auip_action_0"
        ]

    asyncio.run(run())


def test_required_participant_returns_a_structured_blocked_reason() -> None:
    async def run() -> None:
        captured: dict = {}

        async def fake_call(**kwargs):
            captured.update(kwargs)
            return "auip_blocked", {
                "reason": "The participant is bound to White, but Black moves first."
            }

        with patch("server.auip_participant_llm.call_auip_tool", fake_call):
            result = await decide_with_auip_participant(
                {
                    "action_required": True,
                    "state": {"turn": "black"},
                    "available_actions": {
                        "game.place_stone": {
                            "description": "Place White only when White is to move.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                },
                                "required": ["x", "y"],
                                "additionalProperties": False,
                            },
                        }
                    },
                }
            )

        assert result == {
            "action": "blocked",
            "type": "",
            "payload": {},
            "private_note": (
                "The participant is bound to White, but Black moves first."
            ),
        }
        assert "auip_wait" not in {
            item["function"]["name"] for item in captured["tools"]
        }

    asyncio.run(run())


def test_action_decision_thinking_does_not_change_narration_transport() -> None:
    captured: dict = {}

    class Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            function = SimpleNamespace(name="auip_wait", arguments='{"reason":"wait"}')
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            tool_calls=[SimpleNamespace(function=function)]
                        )
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    with patch("server.auip_narration_llm._client", return_value=client):
        result = _call_tool_sync(
            system_prompt="decision lane",
            payload={"state": {}},
            tools=[],
            max_tokens=80,
            provider="deepseek",
            model="deepseek-v4-flash",
            reasoning_effort="max",
        )

    assert result == ("auip_wait", {"reason": "wait"})
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["reasoning_effort"] == "max"
    assert "tool_choice" not in captured


def test_single_tool_nonthinking_decision_forces_that_named_tool() -> None:
    captured: dict = {}

    class Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            function = SimpleNamespace(
                name="decide_auip_proposal",
                arguments='{"decision":"approve"}',
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        message=SimpleNamespace(
                            content=None,
                            reasoning_content=None,
                            tool_calls=[SimpleNamespace(function=function)],
                        ),
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    with patch("server.auip_narration_llm._client", return_value=client):
        result = _call_tool_sync(
            system_prompt="gate",
            payload={"proposal": {}},
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "decide_auip_proposal",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            max_tokens=80,
            provider="deepseek",
            model="deepseek-v4-flash",
            reasoning_effort="none",
        )

    assert result == ("decide_auip_proposal", {"decision": "approve"})
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "decide_auip_proposal"},
    }
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_openai_schema_decision_uses_responses_reasoning_and_fast_tier() -> None:
    captured: dict = {}

    class Responses:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_text='{"candidate_id":"c1"}')

    client = SimpleNamespace(responses=Responses())
    schema = {
        "type": "object",
        "properties": {"candidate_id": {"type": "string", "enum": ["c1"]}},
        "required": ["candidate_id"],
        "additionalProperties": False,
    }
    with patch("server.auip_narration_llm._client", return_value=client):
        result = _call_schema_sync(
            system_prompt="role decision",
            payload={"state": {}},
            schema=schema,
            schema_name="b2_choice",
            max_tokens=120,
            provider="openai",
            model="gpt-5.6-terra",
            reasoning_effort="low",
            service_tier="fast",
            timeout_s=8,
        )

    assert result == {"candidate_id": "c1"}
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["service_tier"] == "fast"
    assert captured["text"]["format"] == {
        "type": "json_schema",
        "name": "b2_choice",
        "strict": True,
        "schema": schema,
    }
    assert "tools" not in captured


def test_deepseek_schema_decision_uses_one_forced_native_tool() -> None:
    captured: dict = {}

    class Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            function = SimpleNamespace(
                name="b2_choice",
                arguments=(
                    '{"candidate_id":"c1","choice_reason":"legal",'
                    '"speech":"ここに置くわ。"}'
                ),
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        message=SimpleNamespace(
                            content=None,
                            reasoning_content=None,
                            tool_calls=[SimpleNamespace(function=function)],
                        ),
                    )
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    schema = {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string", "enum": ["c1"]},
            "choice_reason": {"type": "string", "minLength": 1},
            "speech": {"type": "string", "minLength": 1},
        },
        "required": ["candidate_id", "choice_reason", "speech"],
        "additionalProperties": False,
    }
    with patch("server.auip_narration_llm._client", return_value=client):
        result = _call_schema_sync(
            system_prompt="role decision",
            payload={"state": {}},
            schema=schema,
            schema_name="b2_choice",
            max_tokens=120,
            provider="deepseek",
            model="deepseek-v4-flash",
            reasoning_effort="none",
            timeout_s=8,
        )

    assert result == {
        "candidate_id": "c1",
        "choice_reason": "legal",
        "speech": "ここに置くわ。",
    }
    assert captured["tools"][0]["function"]["parameters"] == schema
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "b2_choice"},
    }
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "response_format" not in captured


def test_openai_tool_decision_uses_responses_reasoning_and_fast_tier() -> None:
    captured: dict = {}

    class Responses:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output=[
                    SimpleNamespace(type="reasoning"),
                    SimpleNamespace(
                        type="function_call",
                        name="reactor_policy",
                        arguments='{"goal":"safe_and_stable"}',
                    ),
                ]
            )

    client = SimpleNamespace(responses=Responses())
    tools = [
        {
            "type": "function",
            "function": {
                "name": "reactor_policy",
                "description": "Set one bounded reactor policy.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "enum": ["safe_and_stable"],
                        }
                    },
                    "required": ["goal"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "blocked",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    with patch("server.auip_narration_llm._client", return_value=client):
        result = _call_tool_sync(
            system_prompt="participant decision",
            payload={"state": {"temperature": 92.3}},
            tools=tools,
            max_tokens=160,
            provider="openai",
            model="gpt-5.6-terra",
            reasoning_effort="low",
            service_tier="fast",
            timeout_s=8,
        )

    assert result == ("reactor_policy", {"goal": "safe_and_stable"})
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["service_tier"] == "fast"
    assert captured["tool_choice"] == "required"
    assert captured["tools"][0] == {
        "type": "function",
        "name": "reactor_policy",
        "description": "Set one bounded reactor policy.",
        "parameters": tools[0]["function"]["parameters"],
    }
    assert "messages" not in captured


def test_participant_transport_failure_is_not_a_semantic_wait() -> None:
    async def run() -> None:
        async def unavailable(**_kwargs):
            return None

        with patch("server.auip_participant_llm.call_auip_tool", unavailable):
            try:
                await decide_with_auip_participant(
                    {
                        "state": {"turn": "kurisu"},
                        "available_actions": {"game.move": {}},
                    }
                )
                raise AssertionError("transport failure must remain observable")
            except AuipProtocolError as exc:
                assert exc.code == "participant_decision_unavailable"

    asyncio.run(run())


def test_main_role_gate_authorizes_the_exact_proposal_without_visible_prose() -> None:
    async def run() -> None:
        captured: dict = {}

        async def fake_call(**kwargs):
            captured.update(kwargs)
            return "decide_auip_proposal", {
                "role_alignment": "matches",
                "decision": "approve",
                "reason": "matches the agreed H7 move",
            }

        with (
            patch(
                "server.auip_role_authorizer_llm.inherited_main_role_prompt",
                return_value="CANONICAL MAIN ROLE",
            ),
            patch("server.auip_role_authorizer_llm.call_auip_tool", fake_call),
        ):
            result = await authorize_with_main_role(
                {
                    "state": {"turn": "kurisu", "board": []},
                    "proposal": {
                        "proposal_id": "proposal-1",
                        "type": "game.move",
                        "payload": {"position": "H7"},
                        "expected_revision": 4,
                    },
                    "global_conversation_context": "下 H7。",
                }
            )

        assert result == {
            "decision": "approve",
            "role_alignment": "matches",
            "reason": "matches the agreed H7 move",
        }
        assert captured["system_prompt"].startswith("CANONICAL MAIN ROLE")
        assert AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT in captured["system_prompt"]
        assert "first person in `current_role_response` is the participant" in (
            captured["system_prompt"]
        )
        assert "Never make an\napproval coherent by swapping these identities" in (
            captured["system_prompt"]
        )
        assert captured["payload"]["proposal"]["payload"] == {"position": "H7"}
        assert [tool["function"]["name"] for tool in captured["tools"]] == [
            "decide_auip_proposal"
        ]

    asyncio.run(run())


def test_main_role_gate_fails_closed_when_alignment_conflicts_with_decision() -> None:
    async def run() -> None:
        async def fake_call(**_kwargs):
            return "decide_auip_proposal", {
                "role_alignment": "conflicts",
                "decision": "approve",
                "reason": "The role left the current action to the user.",
            }

        with (
            patch(
                "server.auip_role_authorizer_llm.inherited_main_role_prompt",
                return_value="CANONICAL MAIN ROLE",
            ),
            patch("server.auip_role_authorizer_llm.call_auip_tool", fake_call),
        ):
            result = await authorize_with_main_role(
                {
                    "current_role_response": "You go first; I will go second.",
                    "state": {
                        "turn": "black",
                        "roleBindings": {"user": "black", "participant": "white"},
                    },
                    "proposal": {
                        "type": "game.configure_participants",
                        "payload": {"participantSide": "black"},
                    },
                }
            )

        assert result == {
            "decision": "reject",
            "role_alignment": "conflicts",
            "reason": "The role left the current action to the user.",
        }

    asyncio.run(run())


if __name__ == "__main__":
    test_observer_and_narrator_keep_separate_system_authority()
    test_model_transport_owns_the_json_shape()
    test_interactive_auip_model_client_disables_hidden_transport_retries()
    test_participant_uses_role_free_native_tools_with_declared_payload_shape()
    test_required_participant_opportunity_replaces_wait_with_blocked_tool()
    test_required_participant_returns_a_structured_blocked_reason()
    test_participant_transport_failure_is_not_a_semantic_wait()
    test_main_role_gate_authorizes_the_exact_proposal_without_visible_prose()
    test_main_role_gate_fails_closed_when_alignment_conflicts_with_decision()
    test_action_decision_thinking_does_not_change_narration_transport()
    print("ok: AUIP Observer and Narrator keep separate model authority")
