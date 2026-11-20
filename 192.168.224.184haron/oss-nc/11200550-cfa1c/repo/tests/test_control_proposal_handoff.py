"""T2 control proposals commit at transport completion, not role-turn end."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.control_proposal import seal_control_proposals


def _action(**attrs):
    return {"type": "DELEGATE", "attrs": attrs, "raw": "[DELEGATE ...]"}


def test_snapshot_is_immutable_and_decision_view_hides_proposed_control() -> None:
    action = _action(
        provider="locus",
        intent="amend",
        project_id="role_guess",
        task="edit README",
    )
    batch = seal_control_proposals(
        [action],
        turn_id="turn-1",
        session_id="session-1",
        user_text="修改 README",
        transport="inline_tag",
    )
    action["attrs"]["intent"] = "execute"

    assert batch.commit_point == "delegate_tag_closed"
    assert batch.proposals[0]["intent"] == "amend"
    assert batch.decision_payloads() == ({"task": "edit README"},)
    try:
        batch.proposals[0]["intent"] = "report"
    except TypeError:
        pass
    else:
        raise AssertionError("shadow observer could mutate the dispatch snapshot")


def test_inline_transport_rejects_a_fake_multi_action_batch() -> None:
    try:
        seal_control_proposals(
            [_action(provider="locus", task="one"), _action(provider="browser", task="two")],
            turn_id="turn-many",
            session_id="session-many",
            user_text="do both",
            transport="inline_tag",
        )
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("inline transport pretended to support multiple DELEGATE tags")
