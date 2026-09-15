"""Both routing strategies retain the existing persistent-focus audit contract."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from server.focus_policy import current_focus_modifier_audit
from server.work_planner import RuntimeWorkPlanner
from test_cooperative_pending_turn import pending_host as pending_host


@pytest.mark.parametrize("modifier", ["set", "clear"])
@pytest.mark.parametrize("audit_reply", ["SET", "CLEAR", "NONE", RuntimeError("offline")])
async def test_runtime_planner_reuses_focus_audit_and_preserves_work_on_denial(
        pending_host, monkeypatch, audit_reply, modifier):
    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda: (context.manager.provider,))
    clause = ("以后就用这个项目吧，顺便做个清单页。" if modifier == "set"
        else "先回草稿吧，再做个清单页。")
    source = "关于刚才那个，" + clause
    project = context.host.project.project_id
    audit = Mock(**({"side_effect": audit_reply} if isinstance(audit_reply, Exception)
        else {"return_value": audit_reply}))
    monkeypatch.setattr("llm.client.remote_llm_query", audit)

    async def query(_messages):
        if "[Independent candidate verdict - FINAL]" in _messages[0]["content"]:
            return json.dumps({"evidence":"contextual"
                if "project:" + project in _messages[-1]["content"] else "none"})
        return json.dumps({"decisions": [{"proposal_index": 0,
            "source_clause": clause, "provider": context.manager.provider,
            "intent": "execute", **({"subject": "project"} if modifier == "set" else {}),
            "work_placement": "project" if modifier == "set" else "draft",
            "session_context": "bind" if modifier == "set" else "clear",
            "workspace_effect": "write", "payload_continuity": "current_turn",
            "reference_mode": "candidates" if modifier == "set" else "none",
            "references": ["project:" + project] if modifier == "set" else None}]},
            ensure_ascii=False)

    ingress = await context.manager._ingress_for(context.session_id)
    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=context.manager.provider)
    plan = await planner(ingress, "focus-plan", {"text": source},
        SimpleNamespace(utterance_id="focus-plan"))
    assert plan.status == "ok" and len(plan.operations) == 1
    action = plan.operations[0].action
    assert action["task"] == clause
    if modifier == "set":
        assert action["project_id"] == project
    else:
        assert action["one_off"] is True
    assert action["intent"] == "execute"
    audit.assert_called_once()
    assert json.loads(audit.call_args.args[0])["user_message"] == source
    if audit_reply == modifier.upper():
        assert action["focus"] == modifier
        accepted = current_focus_modifier_audit(action)
        assert accepted is not None and accepted.allowed
        assert current_focus_modifier_audit({**action,
            "_host_source_user_text": "只是改这个文件。"}) is None
        assert current_focus_modifier_audit({**action,
            "focus": "clear" if modifier == "set" else "set"}) is None
    else:
        assert "focus" not in action
        assert action["_host_focus_guard"] in {"removed", "audit_unavailable"}
        if modifier == "clear":
            assert action["one_off"] is True
    assert context.host.adapter.calls == 0
