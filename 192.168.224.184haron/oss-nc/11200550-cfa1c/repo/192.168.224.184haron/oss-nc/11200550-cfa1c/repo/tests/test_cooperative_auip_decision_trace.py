"""Focused and inactive AUIP decisions retain one source-local diagnostic record."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from server.auip_control_decision import AuipControlDecision
from server.cooperative_chat_ingress import CooperativeChatManager


class _Decider:
    def __init__(self, *, focused=None, inactive=None):
        self.focused = focused
        self.inactive = inactive
        self.calls = []
        self.resolutions = 0

    def capture(self, **kwargs):
        self.calls.append(dict(kwargs))
        if kwargs.get("active_required") and self.focused is None:
            return None
        decision = self.focused if kwargs.get("active_required") else self.inactive

        async def resolve():
            self.resolutions += 1
            return decision

        return resolve()


def _subject(decider, *, work_planner=None, entry_context=None):
    manager = CooperativeChatManager.__new__(CooperativeChatManager)
    manager.auip_decider = decider
    manager.auip_router = object()
    manager.work_planner = work_planner
    manager.auip_entry_context = entry_context
    loop = SimpleNamespace(_binding=object(), history=[], prior_messages=lambda _turn:[],
        trace=[], _monitors=set())
    ingress = SimpleNamespace(session_id="session-trace", loop=loop)
    admission = SimpleNamespace(utterance_id="utterance-trace")
    return manager, ingress, admission


@pytest.mark.parametrize(
    ("decision", "work_planner", "expected_result"),
    [
        (
            AuipControlDecision(
                status="ok",
                action="none",
                work_relation="subsumed",
                app_session_id="app-focused",
                raw_reply='{"action":"none","work_relation":"subsumed"}',
            ),
            None,
            None,
        ),
        (
            AuipControlDecision(
                status="ok",
                action="launch",
                timing="after_work",
                mode="collaborate",
                work_relation="independent",
                app_session_id="app-focused",
                raw_reply=(
                    '{"action":"launch","timing":"after_work",'
                    '"mode":"collaborate","target":"Focused app",'
                    '"work_relation":"independent"}'
                ),
            ),
            object(),
            "entry",
        ),
        (
            AuipControlDecision(
                status="invalid",
                reason="not exact JSON",
                raw_reply="not-json",
            ),
            None,
            None,
        ),
    ],
    ids=["none", "after_work", "invalid"],
)
def test_focused_decision_is_recorded_once_without_recapture(
    caplog, decision, work_planner, expected_result
):
    async def run():
        decider = _Decider(focused=decision)
        manager, ingress, admission = _subject(decider, work_planner=work_planner)
        user_text = "完成后打开预览。"
        caplog.set_level(logging.INFO, logger="server.cooperative_chat_ingress")

        result = await manager.handle_auip_action(
            ingress, "turn-focused", user_text, admission, lambda: admission
        )

        if expected_result == "entry":
            entry = result["entry"]
            assert await entry["pending"] is decision
            assert entry["focused"] is True
        else:
            assert result is expected_result
        assert len(decider.calls) == decider.resolutions == 1
        assert decider.calls[0]["active_required"] is True
        observation, = ingress.loop.trace
        assert observation == {
            "kind":"auip_entry_decision",
            "turn_id":"turn-focused",
            "work_followup":False,
            "status":decision.status,
            "action":decision.action,
            "timing":decision.timing,
            "work_relation":decision.work_relation,
            "reason":decision.reason,
            "raw":decision.raw_reply,
            "source":"focused_active",
        }
        assert user_text not in str(observation)
        records = [record for record in caplog.records
            if "[COOPERATIVE-AUIP-ENTRY]" in record.getMessage()]
        assert len(records) == 1
        assert '"source":"focused_active"' in records[0].getMessage()

    asyncio.run(run())


def test_inactive_decision_keeps_existing_record_shape_and_one_query(caplog):
    async def run():
        decision = AuipControlDecision(
            status="ok",
            action="none",
            raw_reply='{"action":"none"}',
        )
        decider = _Decider(inactive=decision)
        manager, ingress, admission = _subject(
            decider, entry_context=lambda _session_id:"entry candidates"
        )
        caplog.set_level(logging.INFO, logger="server.cooperative_chat_ingress")

        result = await manager.handle_auip_action(
            ingress, "turn-inactive", "普通聊天。", admission, lambda: admission
        )
        entry = result["entry"]
        entry["release"].set()
        assert await entry["pending"] is decision

        assert len(decider.calls) == 2
        assert decider.calls[0]["active_required"] is True
        assert decider.calls[1].get("active_required") is None
        assert decider.resolutions == 1
        assert ingress.loop.trace == [{
            "kind":"auip_entry_decision",
            "turn_id":"turn-inactive",
            "work_followup":False,
            "status":"ok",
            "action":"none",
            "timing":"now",
            "work_relation":"",
            "reason":"",
            "raw":'{"action":"none"}',
        }]
        records = [record for record in caplog.records
            if "[COOPERATIVE-AUIP-ENTRY]" in record.getMessage()]
        assert len(records) == 1
        assert '"source"' not in records[0].getMessage()

    asyncio.run(run())
