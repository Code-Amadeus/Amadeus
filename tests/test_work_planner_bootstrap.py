"""Exercise production planner assembly without booting devices or Providers."""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

def assemble(*, enabled, query, coordinator, model="", cooperative=True):
    path = Path(__file__).resolve().parents[1] / "server/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    branch = next(node for node in ast.walk(tree)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Attribute)
        and node.test.attr == "COOPERATIVE_WORK_PLANNER_ENABLED")
    outer = next(node for node in ast.walk(tree)
        if isinstance(node, ast.If) and branch in tuple(ast.walk(node))
        and isinstance(node.test, ast.Name) and node.test.id == "cooperative_chat_enabled")
    authorities = [node for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
            and target.id in {"control_authority_enabled", "compound_control_authority_enabled"}
            for target in node.targets)]
    scope = {"asyncio":asyncio, "provider_id":"codex", "work_ledger":coordinator,
        "cooperative_chat":SimpleNamespace(work_planner=None),
        "cooperative_chat_enabled":cooperative,
        "_llm_client_mod":SimpleNamespace(remote_llm_messages_query=query),
        "settings":SimpleNamespace(COOPERATIVE_WORK_PLANNER_ENABLED=enabled,
            CONTROL_DECISION_AUTHORITY_ENABLED=True,
            COMPOUND_CONTROL_AUTHORITY_ENABLED=True,
            COOPERATIVE_WORK_PLANNER_MODEL=model,
            CONTROL_DECISION_MAX_TOKENS=777, CONTROL_DECISION_TIMEOUT_S=13,
            CONTROL_DECISION_PROJECT_LIMIT=20, CONTROL_DECISION_WORK_ITEM_LIMIT=30,
            CONTROL_DECISION_EXHAUSTIVE_CANDIDATE_LIMIT=40)}
    # Keep the actual production outer guard: an enabled planner flag must not
    # install a planner when the user restores original Chat routing.
    guarded = ast.copy_location(ast.If(test=outer.test, body=[branch], orelse=[]), outer)
    exec(compile(ast.Module(body=[guarded, *authorities], type_ignores=[]), str(path), "exec"), scope)
    return scope


def test_default_route_enables_both_cooperative_and_professional():
    from config import settings

    fields = {field.key: field for field in settings.declared_environment_fields()}
    assert fields["COOPERATIVE_CHAT_ENABLED"].default is True
    assert fields["COOPERATIVE_WORK_PLANNER_ENABLED"].default is True
    assert fields["WORK_CODING_PROVIDER"].default == "codex"
    assert fields["WORK_EXECUTION_PROVIDER"].default == "pi"
    assert fields["PI_PROVIDER_ENABLED"].default is True
    assert json.loads(fields["COOPERATIVE_CHAT_ADDITIONAL_REQUIREMENTS_JSON"].default) == {}


@pytest.mark.parametrize("additional,registered,expected", [
    ({"codex": {}}, {"codex"}, {"pi", "codex"}),
    ({"codex": {}}, set(), {"pi"}),
    ({"pi": {}}, {"pi"}, {"pi"}),
    ({"openclaw": {"workspace_access": "none"}}, {"openclaw"}, {"pi", "openclaw"}),
])
def test_additional_provider_assembly_preserves_primary_and_tolerates_missing_runtime(additional, registered, expected):
    from agent_host.provider_roles import work_context_requirements
    from agent_host.provider_catalog import PI_MANIFEST, CODEX_APP_SERVER_MANIFEST, OPENCLAW_MANIFEST

    manifests = {m.provider_id: m for m in (PI_MANIFEST, CODEX_APP_SERVER_MANIFEST, OPENCLAW_MANIFEST)}
    runtime = SimpleNamespace(get_manifest=lambda name: manifests[name] if name in registered else None,
        provider_manifests=lambda: tuple(manifests[name] for name in registered))
    contexts = work_context_requirements(runtime, roles={"coding":"codex", "execution":"pi"},
        primary_policy={"workspace_access":"read"}, additional_policies=additional)
    assert set(contexts) == expected
    assert contexts["pi"].workspace_access == "read"


@pytest.mark.parametrize("enabled", [False, True])
def test_original_route_restores_both_authorities_and_never_constructs_planner(enabled):
    query = Mock(side_effect=AssertionError("original route must not invoke cooperative planner"))
    with patch(
        "server.work_planner.RuntimeWorkPlanner",
        side_effect=AssertionError("original route must not construct cooperative planner"),
    ):
        scope = assemble(cooperative=False, enabled=enabled, query=query, coordinator=object())
    assert scope["control_authority_enabled"] is True
    assert scope["compound_control_authority_enabled"] is True
    assert scope["cooperative_chat"].work_planner is None
    assert "_query_cooperative_work" not in scope
    query.assert_not_called()


def test_disabled_assembly_keeps_existing_route_without_creating_a_query():
    query = Mock(side_effect=AssertionError("disabled planner must not query"))
    scope = assemble(enabled=False, query=query, coordinator=object())
    assert scope["cooperative_chat"].work_planner is None
    assert scope["control_authority_enabled"] is False
    assert scope["compound_control_authority_enabled"] is False
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
    assert scope["control_authority_enabled"] is False
    assert scope["compound_control_authority_enabled"] is False
    kwargs = constructor.call_args.kwargs
    assert kwargs["coordinator"] is coordinator and kwargs["provider"] == "codex"
    assert (kwargs["project_limit"], kwargs["work_item_limit"], kwargs["candidate_limit"]) == (20,30,40)
    query.assert_not_called()
    messages = [{"role":"system", "content":"専門の判断契約"},
        {"role":"user", "content":"帮我做个清单页吧。"}]
    assert await kwargs["query"](messages) == '{"decisions":[]}'
    query.assert_called_once_with(messages, json_output=True, temperature=0.0,
        model=model or None, max_tokens=777, timeout=13.0)
