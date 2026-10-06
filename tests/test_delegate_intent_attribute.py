"""Declaring what the user asked for, instead of being told to refrain.

The read-only invariant — a status question must never create work — was
stated as prose asking the model to hold back, and holding back kept losing:
on 2026-07-31 a step that said "just report its status" still created an
attempt in 20% of tag-path runs and 58% of tool-path runs. Filling a required
slot is a classification rather than an inhibition, and a declaration is
something the host can act on, which a rule about restraint never was.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config.settings as settings
from llm.prompts import get_system_prompt






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
