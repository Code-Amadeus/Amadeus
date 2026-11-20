"""Letting the model say which turns change existing work.

Which task a follow-up extends is a question about what the user meant. The
host had been inferring it from phrasing, and had the gate backwards: it
grounded pronouns ("that file") and skipped the easier case where the filename
is written out. Measured 2026-08-01, that cost every explicitly named
follow-up: B3 and E1 routed to a new task 10 times out of 10, while the
anaphoric A5 bound 4 times in 5. Turning the roster's candidate rows back on
changed nothing (0/10 either way), which is what ruled the roster out.

Naming the target instead of guessing it is not enough on its own: the model
will not quote a work_item_id (0 of 18, then 0 of 10 more), because that asks
it to transcribe an identifier rather than make a judgement. Declaring `amend`
asks for the judgement it is already making, which is the same reason the
report declaration worked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config.settings as settings
from llm.prompts import get_system_prompt

ROSTER = [
    {"work_item_id": "w_old", "title": "create old.txt and write old"},
    {"work_item_id": "w_cur", "title": "create current.txt and write current"},
]

# What the ledger registered, rather than how the task happened to be worded.
ROSTER_BY_ARTIFACT = [
    {"work_item_id": "w_old", "title": "这是路由协议测试；不要在主对话中直接执行任务…", "files": ["old.txt"]},
    {"work_item_id": "w_cur", "title": "", "files": ["current.txt"]},
]






def test_amend_requires_identifiable_continuity_and_does_not_contradict_report() -> None:
    with (
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
        patch.object(settings, "DELEGATE_AMEND_INTENT", True),
    ):
        prompt = get_system_prompt("with_delegate")
    assert 'intent="amend"' in prompt
    # Two different axes, so both tie-breaks have to survive side by side.
    assert "既存台帳だけで足りるなら report" in prompt or "ledger facts are report" in prompt
    assert (
        "特定の既存 WorkItem" in prompt
        or "identifies a specific prior WorkItem" in prompt
    )
    assert "その連続性を指せない新しい依頼は execute" in prompt or (
        "If no such continuity is identifiable, use execute" in prompt
    )
    assert "迷ったら amend" not in prompt
    assert "continues an existing one, choose amend" not in prompt
    assert "要約・分析・監査・検証" in prompt or "summarizing, analyzing, auditing" in prompt
    assert "プロジェクトやリポジトリ自体" in prompt or "existing project or repository alone" in prompt

    with patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True):
        with patch.object(settings, "DELEGATE_AMEND_INTENT", False):
            without = get_system_prompt("with_delegate")
    assert 'intent="amend"' not in without
    assert 'intent="report"' in without
