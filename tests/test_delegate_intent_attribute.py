"""Declaring what the user asked for, instead of being told to refrain.

The read-only invariant — a status question must never create work — was
stated as prose asking the model to hold back, and holding back kept losing:
on 2026-07-31 a step that said "just report its status" still created an
attempt in 20% of tag-path runs and 58% of tool-path runs. Filling a required
slot is a classification rather than an inhibition, and a declaration is
something the host can act on, which a rule about restraint never was.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config.settings as settings
from llm.delegate_tool import DELEGATE_TOOL
from llm.prompts import get_system_prompt
from server.app import _delegate_declared_report_only, _handle_delegate


def test_declaration_is_only_honoured_while_it_is_part_of_the_contract() -> None:
    # Off: an absent declaration is indistinguishable from a model that was
    # never asked for one, so nothing may be inferred from it.
    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", False):
        assert _delegate_declared_report_only({"intent": "report"}) is False

    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True):
        assert _delegate_declared_report_only({"intent": "report"}) is True
        assert _delegate_declared_report_only({"intent": "REPORT"}) is True
        assert _delegate_declared_report_only({"intent": "execute"}) is False
        # Silence is not consent: an undeclared delegate still runs, so a model
        # that ignores the attribute degrades to today's behaviour rather than
        # having all its work silently dropped.
        assert _delegate_declared_report_only({}) is False


def test_a_declared_report_never_reaches_the_provider() -> None:
    """The invariant is that nothing is routed, not that nothing happens.

    Until task lookup existed those were the same statement, so this asserted
    a bare None. Now a report is answered from the ledger, and the return
    value says which way that went. What must never change is that the
    provider router is not consulted at all.
    """

    async def run(lookup: bool) -> object:
        with (
            patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
            patch.object(settings, "TASK_LOOKUP_ENABLED", lookup),
            patch("server.app._delegate_provider_for_task") as router,
        ):
            result = await _handle_delegate(
                "report the status of theme.txt",
                {"provider": "locus", "intent": "report", "task": "status"},
            )
            router.assert_not_called(), "a report must not even be routed"
        return result

    # Without lookup the turn ends at the refusal, exactly as it always did.
    assert asyncio.run(run(False)) is None
    # With it, the refusal is still absolute; the difference is that the user
    # now gets told something instead of silence.
    answered = asyncio.run(run(True))
    assert isinstance(answered, str) and answered.startswith("[report]")

    asyncio.run(run(False))


def test_the_contract_appears_in_the_prompt_only_when_enforced() -> None:
    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", False):
        assert "[Delegate intent]" not in get_system_prompt("with_delegate")

    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True):
        prompt = get_system_prompt("with_delegate")
    assert "[Delegate intent]" in prompt
    assert 'intent="report"' in prompt
    assert 'intent="execute"' in prompt
    assert 'subject="work_item"' in prompt
    assert 'subject="project"' in prompt
    assert "Workspace routing candidates" in prompt
    assert "外部状態を観察" in prompt or "observe or operate on external state" in prompt
    assert "読み取り専用" in prompt or "read-only" in prompt
    assert "事実源" in prompt or "source of truth" in prompt
    assert "その配下の複数 WorkItem" in prompt or "several WorkItems under it" in prompt
    assert "1. ホストの既存台帳" in prompt or "1. Existing host-ledger facts" in prompt
    assert "その時はタグを出さずに答える" not in prompt
    assert "result: answer that without a tag" not in prompt


def test_the_schema_offers_the_same_values_as_the_tag() -> None:
    """Both transports must name the same intents or they are not interchangeable."""

    assert DELEGATE_TOOL["function"]["parameters"]["properties"]["intent"]["enum"] == [
        "execute",
        "report",
        "amend",
        "retract",
        "focus",
    ]
    assert DELEGATE_TOOL["function"]["parameters"]["properties"]["subject"]["enum"] == [
        "work_item",
        "project",
    ]
    assert DELEGATE_TOOL["function"]["parameters"]["properties"]["focus"]["enum"] == [
        "set",
        "clear",
    ]
    focus_description = DELEGATE_TOOL["function"]["parameters"]["properties"]["focus"][
        "description"
    ]
    assert "Naming a project or existing task" in focus_description
    assert "does not imply focus" in focus_description


if __name__ == "__main__":
    test_declaration_is_only_honoured_while_it_is_part_of_the_contract()
    print("ok: the declaration is only honoured while it is part of the contract")
    test_a_declared_report_never_reaches_the_provider()
    print("ok: a declared report never reaches the provider")
    test_the_contract_appears_in_the_prompt_only_when_enforced()
    print("ok: the contract appears in the prompt only when enforced")
    test_the_schema_offers_the_same_values_as_the_tag()
    print("ok: the schema offers the same values as the tag")
    print("all delegate intent tests passed")
