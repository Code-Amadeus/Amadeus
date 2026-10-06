"""A denied persistent modifier preserves the current operation's routing."""

import asyncio
from unittest.mock import patch

import pytest

from server.focus_policy import apply_focus_modifier_audit, audit_focus_modifier


@pytest.mark.parametrize("modifier", ["set", "clear"])
@pytest.mark.parametrize("reply", ["NONE", RuntimeError("audit unavailable")])
def test_denied_focus_preserves_the_current_work(modifier, reply):
    async def run():
        attrs = {"intent": "execute", "focus": modifier, "task": "Update README",
            "project_id": "project-target", "_host_source_user_text": "Update README，先不要切换默认项目。"}
        query = (patch("llm.client.remote_llm_query", side_effect=reply)
            if isinstance(reply, Exception) else patch("llm.client.remote_llm_query", return_value=reply))
        with query:
            audit = await audit_focus_modifier(attrs)
        assert audit.allowed is False
        apply_focus_modifier_audit(attrs, audit)
        assert "focus" not in attrs
        assert attrs["intent"] == "execute"
        assert attrs["task"] == "Update README"
        assert attrs["project_id"] == "project-target"
        assert (attrs.get("one_off") == "true") is (modifier == "clear")
    asyncio.run(run())
