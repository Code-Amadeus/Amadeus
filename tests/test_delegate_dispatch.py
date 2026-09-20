"""Contract tests for the adjudicated delegation dispatch boundary."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.delegate_dispatch import DelegateDispatchPlan, build_delegate_metadata
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


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all delegate dispatch tests passed")


if __name__ == "__main__":
    _main()
