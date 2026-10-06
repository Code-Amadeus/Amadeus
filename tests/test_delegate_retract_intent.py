"""Giving withdrawal a verb, so "stop" stops instead of starting.

The prompt named no way to take work back — cancel, stop and retract appeared
nowhere — while the roster still told the model to "stop that task and start
nothing". Handed an instruction with no mechanism, the model used the only
structured action it had and delegated "stop the running task" as work to
execute: measured 2026-07-31 on B1, "把那个停了" created a third WorkItem in 3
of 5 runs, and the intent gate could not catch it because the model genuinely
believed it was executing. Even the harmless variant had the character say it
had stopped something that was never cancelled.

Interruption is host-owned and already built (ProviderRuntime.cancel, plus a
cancel on every adapter), so this is wiring, not capability.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config.settings as settings
from llm import prompts
from llm.prompts import get_system_prompt


def _both_flags(value: bool = True):
    return (
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", value),
        patch.object(settings, "DELEGATE_RETRACT_INTENT", value),
    )
















def test_the_verb_appears_in_the_prompt_only_while_the_host_acts_on_it() -> None:
    intent_flag, retract_flag = _both_flags()
    with intent_flag, retract_flag:
        prompt = get_system_prompt("with_delegate")
    assert 'intent="retract"' in prompt
    # The gated verb carries the shared Host-owned withdrawal contract.
    assert prompts._JA_RETRACT_ADDON in prompt or prompts._EN_RETRACT_ADDON in prompt
    # The tie-break must survive the insertion.
    assert "既存台帳だけで足りるなら report" in prompt or "ledger facts are report" in prompt

    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True):
        with patch.object(settings, "DELEGATE_RETRACT_INTENT", False):
            without = get_system_prompt("with_delegate")
    assert 'intent="retract"' not in without
    assert 'intent="report"' in without, "turning the verb off keeps the rest intact"
