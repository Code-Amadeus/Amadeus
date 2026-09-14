"""Exercise production planner assembly without booting devices or Providers."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

def assemble(*, enabled, query, coordinator, model=""):
    path = Path(__file__).resolve().parents[1] / "server/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    branch = next(node for node in ast.walk(tree)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Attribute)
        and node.test.attr == "COOPERATIVE_WORK_PLANNER_ENABLED")
    scope = {"asyncio":asyncio, "provider_id":"codex", "work_ledger":coordinator,
        "cooperative_chat":SimpleNamespace(work_planner=None),
        "_llm_client_mod":SimpleNamespace(remote_llm_messages_query=query),
        "settings":SimpleNamespace(COOPERATIVE_WORK_PLANNER_ENABLED=enabled,
            COOPERATIVE_WORK_PLANNER_MODEL=model,
            CONTROL_DECISION_MAX_TOKENS=777, CONTROL_DECISION_TIMEOUT_S=13,
            CONTROL_DECISION_PROJECT_LIMIT=20, CONTROL_DECISION_WORK_ITEM_LIMIT=30,
            CONTROL_DECISION_EXHAUSTIVE_CANDIDATE_LIMIT=40)}
    exec(compile(ast.Module(body=[branch], type_ignores=[]), str(path), "exec"), scope)
    return scope


def test_disabled_assembly_keeps_existing_route_without_creating_a_query():
    query = Mock(side_effect=AssertionError("disabled planner must not query"))
    scope = assemble(enabled=False, query=query, coordinator=object())
    assert scope["cooperative_chat"].work_planner is None
    assert "_query_cooperative_work" not in scope
    query.assert_not_called()


@pytest.mark.parametrize("model", ["", "backend-supported-specialist"])
async def test_enabled_assembly_registers_real_component_with_existing_transport(monkeypatch, model):
    from server import work_planner

    real_planner = work_planner.RuntimeWorkPlanner
    constructor = Mock(side_effect=real_planner)
    monkeypatch.setattr(work_planner, "RuntimeWorkPlanner", constructor)
    query = Mock(return_value='{"decisions":[]}')
    coordinator = object()
    scope = assemble(enabled=True, query=query, coordinator=coordinator, model=model)
    assert isinstance(scope["cooperative_chat"].work_planner, real_planner)
    kwargs = constructor.call_args.kwargs
    assert kwargs["coordinator"] is coordinator and kwargs["provider"] == "codex"
    assert (kwargs["project_limit"], kwargs["work_item_limit"], kwargs["candidate_limit"]) == (20,30,40)
    query.assert_not_called()
    messages = [{"role":"system", "content":"専門の判断契約"},
        {"role":"user", "content":"帮我做个清单页吧。"}]
    assert await kwargs["query"](messages) == '{"decisions":[]}'
    query.assert_called_once_with(messages, json_output=True, temperature=0.0,
        model=model or None, max_tokens=777, timeout=13.0)
