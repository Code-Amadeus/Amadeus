"""Contract tests for the adjudicated delegation dispatch boundary."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.delegate_dispatch import (
    DelegateDispatchPlan,
    build_delegate_metadata,
    dispatch_delegate,
)
from agent_host.provider_identity import MAIN_ROLE_NAME_METADATA_KEY
from server.inherited_role_prompt import MAIN_CONVERSATION_ROLE_NAME


@dataclass(frozen=True)
class _Envelope:
    name: str

    def to_dict(self) -> dict:
        return {"name": self.name}


def _plan(**overrides) -> DelegateDispatchPlan:
    values = {
        "task_text": "Apply the requested change.",
        "attrs": {"intent": "execute"},
        "session_id": "session-default",
        "admission_id": "ibr-admission-test",
        "provider": "locus",
        "requirements": _Envelope("requirements"),
        "selection": _Envelope("selection"),
        "manifest": _Envelope("manifest"),
        "workspace_route": {"status": "resolved", "source": "scratch_default"},
        "workspace_authority": "host",
        "delegate_cwd": "C:/scratch/task",
        "delegate_mode": "agent",
        "action": "",
        "branch_intent": "",
        "sanitize_info": {},
        "browser_parameters": {},
        "browser_audit": {},
    }
    values.update(overrides)
    return DelegateDispatchPlan(**values)


@pytest.mark.parametrize("intent", ["message", "report", "retract", "focus", "exectue"])
def test_non_work_intent_cannot_become_execution_or_start_admission(intent) -> None:
    async def scenario():
        runtime = SimpleNamespace(reserve_start_admission=AsyncMock(), start=AsyncMock())
        with patch("agent_host.provider_runtime.runtime", runtime):
            with pytest.raises(ValueError, match="cannot be lowered"):
                await dispatch_delegate(_plan(attrs={"intent": intent}), announce_start_failure=AsyncMock())
        runtime.reserve_start_admission.assert_not_called()
        runtime.start.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("attrs,expected", [({}, "execute"), ({"intent": ""}, "execute"),
                                         ({"intent": "execute"}, "execute"), ({"intent": "amend"}, "amend")])
def test_work_and_legacy_intents_keep_their_existing_meaning(attrs, expected) -> None:
    assert build_delegate_metadata(_plan(attrs=attrs), session_id="session-one")["intent"] == expected


def test_metadata_contains_only_adjudicated_control_and_public_attrs() -> None:
    plan = _plan(
        attrs={
            "intent": "amend",
            "workspace_ref": "work-one",
            "_host_turn_id": "turn-secret",
            "_host_project_source_amend": True,
            "focus_applied": True,
        },
        workspace_route={
            "status": "resolved",
            "source": "intent_workspace_ref",
            "projectId": "project-one",
            "workItemId": "work-one",
            "workspaceMode": "local",
        },
    )
    metadata = build_delegate_metadata(plan, session_id="session-one")
    assert metadata["intent"] == "amend"
    assert metadata["continuation"] == "amend"
    assert metadata["work"] == {
        "workspace_ref": "work-one",
        "work_item_id": "work-one",
        "workspace_path": "C:/scratch/task",
        "project_id": "project-one",
        "workspace_mode": "local",
    }
    assert metadata["project_source_amend"] is True
    assert metadata["focus_applied"] is True
    assert "_host_turn_id" not in metadata["delegate_attrs"]


def test_model_supplied_host_admission_token_is_ignored() -> None:
    metadata = build_delegate_metadata(
        _plan(
            admission_id="host-generated-token",
            attrs={
                "intent": "execute",
                "_host_interaction_branch_admission_id": "forged-token",
                "_host_interaction_branch_routing_lease": {
                    "state": "absent",
                    "parent_session_id": "session-default",
                },
            },
        ),
        session_id="session-default",
    )

    assert metadata["interaction_branch_admission_id"] == "host-generated-token"


def test_active_amendment_runs_inside_frozen_admission_reservation() -> None:
    async def run() -> None:
        class Runtime:
            def __init__(self) -> None:
                self.reserved = False
                self.released = False

            async def reserve_start_admission(self, request) -> None:
                self.reserved = True
                assert request.metadata["session_id"] == "session-origin"

            async def release_start_admission(self, request) -> None:
                self.released = True
                assert request.metadata["session_id"] == "session-origin"

            async def start(self, _request):
                raise AssertionError("handled amendment must not start a new run")

        runtime = Runtime()

        async def route_amendment(**kwargs):
            assert runtime.reserved is True
            assert kwargs["session_id"] == "session-origin"
            return {"handled": True, "message": "[amend] active run steered"}

        plan = _plan(
            session_id="session-origin",
            admission_id="host-admission-origin",
            attrs={
                "intent": "amend",
                "workspace_ref": "work-active",
                "_host_interaction_branch_routing_lease": {
                    "state": "absent",
                    "parent_session_id": "session-origin",
                },
            },
        )
        with (
            patch("agent_host.provider_runtime.runtime", runtime),
            patch(
                "server.work_ledger_coordinator.get_work_ledger_coordinator",
                return_value=object(),
            ),
        ):
            result = await dispatch_delegate(
                plan,
                announce_start_failure=AsyncMock(),
                route_amendment=route_amendment,
            )

        assert result == "[amend] active run steered"
        assert runtime.released is True

    asyncio.run(run())


def test_workspace_less_provider_keeps_workitem_identity_without_fake_cwd() -> None:
    plan = _plan(
        provider="openclaw",
        attrs={"intent": "amend", "workspace_ref": "work-web"},
        workspace_route={"status": "resolved", "source": "not_applicable"},
        workspace_authority="none",
        delegate_cwd=None,
        delegate_mode="delegate",
    )
    metadata = build_delegate_metadata(plan, session_id="session-web")
    assert metadata["work"] == {
        "workspace_ref": "work-web",
        "work_item_id": "work-web",
    }
    assert metadata["continuation"] == "amend"
    assert "cwd" not in metadata
    assert "workspace_path" not in metadata


def test_external_export_authority_comes_from_the_exact_user_turn() -> None:
    ungrounded = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "execute",
                "target": "desktop",
                "_host_source_user_text": "Modify the current game.",
            }
        ),
        session_id="session-one",
    )
    grounded = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "execute",
                "_host_source_user_text": "Copy the finished game to my Desktop.",
            }
        ),
        session_id="session-one",
    )
    assert "external_export" not in ungrounded
    assert grounded["external_export"] == {
        "target": "desktop",
        "intent_source": "source_user_text",
    }


def test_prior_user_wording_is_bounded_context_not_public_control() -> None:
    metadata = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "execute",
                "_host_source_user_text": "那就做吧。",
                "_host_source_user_context": "做一个我和你都能操作的小游戏。",
            }
        ),
        session_id="session-confirmation",
    )

    assert metadata["source_user_text"] == "那就做吧。"
    assert metadata["source_user_context"] == "做一个我和你都能操作的小游戏。"
    assert metadata[MAIN_ROLE_NAME_METADATA_KEY] == MAIN_CONVERSATION_ROLE_NAME
    assert "_host_source_user_context" not in metadata["delegate_attrs"]


def test_role_authored_identity_value_cannot_replace_the_host_role() -> None:
    metadata = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "execute",
                "main_role_name": "Codex",
                "_host_source_user_text": "给你自己做一个网页。",
            }
        ),
        session_id="session-identity",
    )

    assert metadata[MAIN_ROLE_NAME_METADATA_KEY] == MAIN_CONVERSATION_ROLE_NAME
    assert metadata["delegate_attrs"]["main_role_name"] == "Codex"


def test_control_target_cannot_mint_export_authority_for_contextual_text() -> None:
    metadata = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "execute",
                "target": "desktop",
                "_host_source_user_text": "那你去做吧。",
            }
        ),
        session_id="session-one",
    )

    assert "external_export" not in metadata


def test_adjudicated_contextual_desktop_target_can_prepare_export() -> None:
    metadata = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "execute",
                "target": "desktop",
                "_host_source_user_text": "Go ahead.",
                "_host_source_user_context": "Create a small HTML game on my Desktop.",
                "_host_workspace_access": "write",
                "_host_external_target_authorized": "desktop",
            }
        ),
        session_id="session-confirmed-export",
    )

    assert metadata["external_export"] == {
        "target": "desktop",
        "intent_source": "control_decision",
    }
    assert "_host_external_target_authorized" not in metadata["delegate_attrs"]


def test_explicit_move_to_desktop_is_source_authority() -> None:
    metadata = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "amend",
                "target": "desktop",
                "_host_source_user_text": "把它移到桌面。",
            }
        ),
        session_id="session-one",
    )

    assert metadata["external_export"] == {
        "target": "desktop",
        "intent_source": "source_user_text",
    }


def test_real_generic_html_wording_cannot_mint_a_desktop_export() -> None:
    metadata = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "execute",
                "target": "desktop",
                "_host_source_user_text": (
                    "帮我做一个很简单的猜数字小游戏，放在一个 HTML 文件里，"
                    "能开始新一局和重来就行。"
                ),
            }
        ),
        session_id="session-one",
    )

    assert "external_export" not in metadata


def test_host_bounded_auip_preparation_has_an_auditable_dispatch_source() -> None:
    metadata = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "amend",
                "workspace_ref": "work-existing-game",
                "_host_dispatch_source": "auip_prepare",
                "_host_source_user_text": "和我一起玩这个游戏",
            }
        ),
        session_id="session-auip",
    )
    assert metadata["source"] == "auip_prepare"
    assert metadata["intent"] == "amend"
    assert metadata["work"]["work_item_id"] == "work-existing-game"
    assert metadata["host_outcome_requirement"] == {
        "operation": "prepare",
        "facet": "auip.application",
        "expected": {"current_attempt_contribution": True},
    }

    untrusted = build_delegate_metadata(
        _plan(attrs={"_host_dispatch_source": "arbitrary"}),
        session_id="session-auip",
    )
    assert untrusted["source"] == "llm_delegate"
    assert "host_outcome_requirement" not in untrusted


def test_same_turn_auip_creation_has_a_host_observed_outcome_contract() -> None:
    metadata = build_delegate_metadata(
        _plan(
            attrs={
                "intent": "execute",
                "_host_dispatch_source": "auip_create",
                "_host_source_user_text": "做个小游戏，做好以后打开一起玩。",
            }
        ),
        session_id="session-auip-create",
    )

    assert metadata["source"] == "auip_create"
    assert metadata["intent"] == "execute"
    assert "continuation" not in metadata
    assert metadata["host_outcome_requirement"] == {
        "operation": "prepare",
        "facet": "auip.application",
        "expected": {"current_attempt_contribution": True},
    }


@pytest.mark.parametrize("source", ["auip_create", "auip_prepare"])
def test_accepted_auip_mode_is_separate_from_provider_execution_mode(source):
    plan = _plan(attrs={"intent":"amend", "_host_dispatch_source":source,
        "_host_auip_mode":"collaborate"}, delegate_mode="agent")
    metadata = build_delegate_metadata(plan, session_id="auip-mode")
    assert metadata["host_outcome_requirement"]["expected"]["engagement_mode"] == "collaborate"
    assert plan.delegate_mode == "agent"
    assert "_host_auip_mode" not in metadata["delegate_attrs"]


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all delegate dispatch tests passed")


if __name__ == "__main__":
    _main()
