"""One input-bound modifier audit precedes canonical history and Host dispatch."""
import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest

from server.focus_policy import audit_focus_modifier, finalize_work_focus_modifiers


SOURCE = "把 Atlas 项目当前源码里的导航栏改成深色。"


def action():
    return {"type": "DELEGATE", "attrs": {"intent": "amend", "focus": "set", "project_id": "project_a",
        "task": "Update navigation", "_host_source_user_text": "切到 A 项目，并修改导航栏。"}}


def test_same_modifier_input_reuses_the_actual_typed_audit():
    async def run():
        item = action()
        with patch("llm.client.remote_llm_messages_query", return_value="SET") as query:
            await finalize_work_focus_modifiers([item])
            audit = await audit_focus_modifier(deepcopy(item["attrs"]))
        assert audit.allowed and query.call_count == 1
        assert audit.request_fingerprint
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"_host_source_user_text": "仅修改 A 项目的导航栏。"}, {"intent": "report"},
    {"focus": "clear"}, {"project_id": ""},
])
def test_changed_audit_input_cannot_reuse_an_earlier_confirmation(change):
    async def run():
        item = action()
        with patch("llm.client.remote_llm_messages_query", return_value="SET"):
            await finalize_work_focus_modifiers([item])
        item["attrs"].update(change)
        with patch("llm.client.remote_llm_messages_query", return_value="NONE") as query:
            audit = await audit_focus_modifier(item["attrs"])
        assert not audit.allowed and query.call_count == 1
    asyncio.run(run())


def test_model_shaped_audit_data_is_not_a_host_receipt():
    async def run():
        item = action()
        item["attrs"]["_host_focus_modifier_audit"] = {"allowed": True, "outcome": "confirmed"}
        with patch("llm.client.remote_llm_messages_query", return_value="NONE") as query:
            await finalize_work_focus_modifiers([item])
        assert "focus" not in item["attrs"] and query.call_count == 1
    asyncio.run(run())


def test_ordinary_work_and_pure_focus_keep_their_existing_owners():
    async def run():
        ordinary = action()
        ordinary["attrs"].pop("focus")
        pure = action()
        pure["attrs"]["intent"] = "focus"
        with patch("llm.client.remote_llm_messages_query", side_effect=AssertionError("unexpected audit")):
            await finalize_work_focus_modifiers([ordinary, pure])
        assert pure["attrs"]["focus"] == "set"
    asyncio.run(run())
