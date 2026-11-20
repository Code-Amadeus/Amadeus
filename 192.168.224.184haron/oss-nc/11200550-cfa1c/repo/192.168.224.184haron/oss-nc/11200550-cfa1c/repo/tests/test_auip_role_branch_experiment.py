from __future__ import annotations

import json

import pytest

from server.auip_contract import AuipProtocolError
from server.auip_role_branch_experiment import (
    AppSessionBranchProposal,
    AppSessionRoleBranch,
    parse_branch_tool_decision,
    participant_first_input,
    participant_first_tools,
    role_executor_tools,
    role_presentation_payload,
)


def _actions() -> dict:
    return {
        "game.place": {
            "description": "Place one stone on an empty intersection.",
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
    }


def _structural_keys(value) -> set[str]:
    if isinstance(value, dict):
        return {
            str(key).lower()
            for key in value
        } | set().union(*(_structural_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_structural_keys(item) for item in value), set())
    return set()


def test_appsession_branch_keeps_dialogue_and_verified_receipts_bounded() -> None:
    branch = AppSessionRoleBranch(
        app_session_id="app-1",
        app_title="Board",
        max_messages=4,
        max_chars=500,
    )
    branch.record_user("你来下黑棋。")
    branch.record_assistant("好，我先选黑。")
    branch.record_receipt(
        accepted=True,
        action_type="game.bind",
        payload={"side": "black"},
        resulting_revision=2,
    )
    branch.record_user("那你开始吧。")
    branch.record_assistant("我下在中央。")

    messages = branch.messages()
    assert len(messages) == 4
    assert messages[-1] == {"role": "assistant", "content": "我下在中央。"}
    assert any("Verified AUIP receipt" in row["content"] for row in messages)
    assert branch.recent_user_directives() == []


def test_participant_first_keeps_user_downlink_but_not_role_persona() -> None:
    branch = AppSessionRoleBranch(app_session_id="app-1", app_title="Board")
    branch.record_strategy_directive("先守住右边。")
    context = participant_first_input(
        {
            "app": {"title": "Board"},
            "revision": 4,
            "state": {"turn": "black", "board": {"rows": ["..."]}},
            "available_actions": _actions(),
            "global_conversation_context": "persona prose that must not pass",
            "controller": {"status": "idle"},
        },
        user_instruction="往右边防守",
        branch=branch,
    )

    assert context["user_instruction"] == "往右边防守"
    assert context["strategy_directives"] == ["先守住右边。"]
    assert context["trigger"] == "participant_opportunity"
    assert context["state"]["turn"] == "black"
    assert "global_conversation_context" not in context
    assert "controller" not in context


def test_ordinary_user_turn_is_not_promoted_to_persistent_strategy() -> None:
    branch = AppSessionRoleBranch(app_session_id="app-1", app_title="Board")
    branch.record_user("再来一盘。")
    branch.record_strategy_directive("优先守右路。")

    assert branch.recent_user_directives() == ["优先守右路。"]


def test_participant_first_tool_preserves_exact_payload_and_choice_reason() -> None:
    tools, mapping = participant_first_tools(_actions())
    action_tool = next(tool for tool in tools if tool["function"]["name"] in mapping)
    schema = action_tool["function"]["parameters"]
    assert schema["properties"]["payload"] == _actions()["game.place"][
        "inputSchema"
    ]
    proposal = parse_branch_tool_decision(
        action_tool["function"]["name"],
        {
            "payload": {"x": 2, "y": 1},
            "instruction_relation": "safe_alternative",
            "choice_reason": "The requested point is occupied; this blocks the line.",
        },
        action_by_tool=mapping,
        require_speech=False,
    )
    assert proposal.action_type == "game.place"
    assert proposal.payload == {"x": 2, "y": 1}
    assert proposal.instruction_relation == "safe_alternative"


def test_role_executor_binds_speech_and_payload_in_one_tool_result() -> None:
    tools, mapping = role_executor_tools(_actions())
    action_tool = next(tool for tool in tools if tool["function"]["name"] in mapping)
    proposal = parse_branch_tool_decision(
        action_tool["function"]["name"],
        {
            "payload": {"x": 1, "y": 1},
            "instruction_relation": "follows",
            "choice_reason": "The user requested the center.",
            "speech": "いいわ、中央に置く。",
        },
        action_by_tool=mapping,
        require_speech=True,
        user_instruction="下中央",
    )
    assert proposal.speech == "いいわ、中央に置く。"
    assert proposal.payload == {"x": 1, "y": 1}


def test_role_executor_allows_no_downlink_relation_for_automatic_turn() -> None:
    tools, mapping = role_executor_tools(_actions())
    action_tool = next(tool for tool in tools if tool["function"]["name"] in mapping)
    proposal = parse_branch_tool_decision(
        action_tool["function"]["name"],
        {
            "payload": {"x": 1, "y": 1},
            # A compatible provider may echo the wrong relation even though
            # this automatic turn has no human downlink. The Host owns that fact.
            "instruction_relation": "follows",
            "choice_reason": "This is an autonomous participant opportunity.",
            "speech": "ここに置くわ。",
        },
        action_by_tool=mapping,
        require_speech=True,
        user_instruction="",
    )

    assert proposal.instruction_relation == "not_applicable"


def test_choice_tools_lock_exact_host_payload_instead_of_asking_model_to_copy_it() -> None:
    tools, mapping = participant_first_tools(
        {
            "defense.set_mode": {
                "description": "Set one defense mode.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}},
                    "required": ["mode"],
                    "additionalProperties": False,
                },
            }
        },
        available_choice_options=[
            {
                "label": "Defend right lane",
                "action": "defense.set_mode",
                "payload": {"mode": "defend_right"},
                "available": True,
            }
        ],
        choice_action_types=["defense.set_mode"],
    )
    choice_tool = next(
        tool for tool in tools if tool["function"]["name"].startswith("auip_branch_choice_")
    )
    parameters = choice_tool["function"]["parameters"]
    assert "payload" not in parameters["properties"]
    proposal = parse_branch_tool_decision(
        choice_tool["function"]["name"],
        {
            "instruction_relation": "safe_alternative",
            "choice_reason": "The right lane is critical.",
            # Even a forged payload-like extra is not the payload authority.
            "payload": {"mode": "defend_left"},
        },
        action_by_tool=mapping,
        require_speech=False,
    )
    assert proposal.payload == {"mode": "defend_right"}
    assert proposal.semantic_label == "Defend right lane"


def test_c_arm_role_payload_has_branch_memory_and_no_raw_state_json() -> None:
    branch = AppSessionRoleBranch(app_session_id="app-1", app_title="Board")
    branch.record_user("你执黑。")
    branch.record_assistant("好，我选黑。")
    branch.record_receipt(
        accepted=True,
        action_type="game.bind",
        payload={"side": "black"},
        resulting_revision=2,
    )
    proposal = AppSessionBranchProposal(
        action="act",
        action_type="game.place",
        payload={"x": 7, "y": 7},
        instruction_relation="follows",
        choice_reason="The center is legal and supports the opening.",
    )
    payload = role_presentation_payload(
        branch=branch,
        app={
            "title": "Board",
            "objective": "Make a line.",
            "interactionSummary": "Choose one legal move.",
        },
        user_instruction="开始吧",
        proposal=proposal,
        action_description="Place one stone on an empty intersection.",
    )

    encoded = json.dumps(payload, ensure_ascii=False).lower()
    structural_keys = _structural_keys(payload)
    assert "branch_messages" in payload
    assert "verified auip receipt" in encoded
    assert {"state", "current_state", "board", "turn", "binding"}.isdisjoint(
        structural_keys
    )
    assert payload["selected_outcome"]["payload"] == {"x": 7, "y": 7}


def test_c_arm_rejects_accidental_raw_state_inside_receipt() -> None:
    branch = AppSessionRoleBranch(app_session_id="app-1", app_title="Board")
    with pytest.raises(AuipProtocolError, match="role_branch_state_leak"):
        role_presentation_payload(
            branch=branch,
            app={"title": "Board"},
            user_instruction="开始吧",
            proposal=AppSessionBranchProposal(action="wait"),
            receipt={"accepted": True, "effects": {"board": ["..."]}},
        )


def test_action_payload_remains_opaque_even_when_domain_field_is_named_state() -> None:
    branch = AppSessionRoleBranch(app_session_id="app-payload", app_title="Device")
    branch.record_receipt(
        accepted=True,
        action_type="device.set_state",
        payload={"state": "armed", "turn": "clockwise"},
        resulting_revision=2,
    )
    proposal = AppSessionBranchProposal(
        action="act",
        action_type="device.set_state",
        payload={"state": "armed", "turn": "clockwise"},
        instruction_relation="follows",
        choice_reason="The requested device state is declared.",
    )

    role_payload = role_presentation_payload(
        branch=branch,
        app={"title": "Device"},
        user_instruction="arm it",
        proposal=proposal,
    )
    capsule = branch.collapse(close_status="completed")

    assert role_payload["selected_outcome"]["payload"] == {
        "state": "armed",
        "turn": "clockwise",
    }
    assert capsule["verified_actions"][0]["payload"] == {
        "state": "armed",
        "turn": "clockwise",
    }


def test_branch_inherits_checkpoint_and_collapses_without_raw_state() -> None:
    branch = AppSessionRoleBranch(
        app_session_id="app-collapse",
        app_title="Co-op Defense",
        checkpoint_messages=[
            {"role": "user", "content": "我们继续刚才的合作。"},
            {"role": "assistant", "content": "当然。"},
        ],
    )
    branch.record_strategy_directive("优先守左路。")
    branch.record_assistant("左路交给我。")
    branch.record_receipt(
        accepted=True,
        action_type="defense.set_mode",
        payload={"mode": "defend_left"},
        resulting_revision=3,
    )

    capsule = branch.collapse(
        close_status="completed",
        close_reason="match_finished",
        terminal={"type": "match.finished", "winner": "team"},
    )

    assert branch.active is False
    assert branch.messages() == []
    assert capsule["strategy_directives"] == ["优先守左路。"]
    assert capsule["verified_actions"][0]["payload"] == {"mode": "defend_left"}
    assert capsule["terminal"] == {"type": "match.finished", "winner": "team"}
    assert {"state", "current_state", "board", "turn"}.isdisjoint(
        _structural_keys(capsule)
    )
    with pytest.raises(AuipProtocolError, match="role_branch_closed"):
        branch.record_user("这一句不能回写到已关闭分支。")


def test_role_context_distinguishes_verified_receipts_from_dialogue() -> None:
    branch = AppSessionRoleBranch(app_session_id="app-context", app_title="Board")
    branch.record_user("你去右边。")
    branch.record_assistant("好，我往右。")
    branch.record_receipt(
        accepted=False,
        action_type="game.move",
        payload={"direction": "right"},
        resulting_revision=4,
        reason="blocked by obstacle",
    )
    branch.record_narration("前面有障碍，我没有过去。")

    context = branch.render_role_context(max_chars=1800)

    assert "Only rows marked receipt are Host-verified" in context
    assert '"kind":"receipt"' in context
    assert "blocked by obstacle" in context
    assert '"kind":"narration"' in context
