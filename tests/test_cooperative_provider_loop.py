"""Interaction lifecycle without manufacturing Work operations."""
import asyncio
from dataclasses import replace
import json
import re
import uuid

import pytest

from agent_host.provider_contract import ProviderCapabilities, ProviderManifest, ProviderRequirements
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import ProviderRunResult, ProviderSessionHandle, ProviderInputDelivery
from server.cooperative_provider_loop import (
    CooperativeProviderLoop,
    LoopConflict,
    RoleDecisionUnavailable,
)
from server.auip_control_decision import AuipControlDecision

POLICY = ProviderRequirements(workspace_access="write", workspace_ownership="caller", resume="attach")


def install_stop_owner(ingress):
    from server.cooperative_chat_ingress import CooperativeChatManager
    from server.attention_request import AttentionRequestCoordinator

    manager = object.__new__(CooperativeChatManager)
    manager.ledger, manager.runtime = ingress.loop._effects.ledger, ingress.loop.runtime
    manager.work_executor = None
    manager.attention = AttentionRequestCoordinator()
    async def query(_messages):
        candidates, _, _, _ = manager.task_stop_candidates(ingress)
        return json.dumps({"references":[candidate.token for candidate in candidates]})
    manager.query = query
    ingress.work_request = manager.handle_work_action


def test_japanese_contracts_keep_protocol_examples_and_source_clauses():
    from server.cooperative_provider_loop import COORDINATION_CONTRACT, PRESENTATION_CONTRACT

    def examples(contract):
        result = []
        for match in re.finditer(r"\{", contract):
            try:
                value, _ = json.JSONDecoder().raw_decode(contract[match.start():])
            except json.JSONDecodeError:
                continue
            result.append(value)
        return result

    coordination, presentation = examples(COORDINATION_CONTRACT), examples(PRESENTATION_CONTRACT)
    assert "JSONオブジェクト" in COORDINATION_CONTRACT
    assert "自然な役割応答そのもの" in PRESENTATION_CONTRACT
    assert list(coordination[0]) == ["action", "say"] and coordination[0]["action"] is None
    assert list(coordination[1]) == ["action", "say"]
    assert coordination[1]["action"] == {"op":"work", "intent":"execute"}
    assert isinstance(coordination[1]["say"], str) and coordination[1]["say"]
    assert presentation == []
    declared_actions = {(op, intent) for op, intent in re.findall(
        r"action\.op=([a-z_]+)(?:、action\.intent=([a-z_]+))?", COORDINATION_CONTRACT)}
    assert declared_actions | {(row["op"], row.get("intent", ""))
            for row in coordination if "op" in row} == {
        ("send", ""), ("interrupt", ""), ("browser", "continue"), ("browser", "close"),
        ("browser", "open"), ("delegate", ""), ("send_to", ""), ("work", "execute"),
        ("work", "amend"), ("work", "retract"), ("report", ""), ("batch", ""),
        ("auip", ""), ("auip_after_work", ""), ("scope_change", ""),
    }
    assert {row["mode"] for row in coordination if "mode" in row} == {"collaborate"}
    assert "modeはobserve、collaborate、delegateのいずれか" in COORDINATION_CONTRACT
    assert {"把 alpha.md 的标题改成‘修订版’", "顺便告诉我 beta.md 对应任务现在什么状态。",
        "创建一个计数器应用", "完成后打开它，我们一起试一下。"} <= {
            row["source"] for row in coordination if "source" in row}
    assert set(re.findall(r"state=([a-z_]+)", PRESENTATION_CONTRACT)) == {
        "scope_change_required", "scope_bound", "work_started", "work_amend_selection_required",
        "auip_step_pending", "auip_read", "auip_applied", "auip_rejected", "auip_after_work_deferred",
        "browser_unknown", "browser_rejected", "browser_closed", "address_selection_required",
        "work_auip_independent", "not_active", "not_accepted", "auip_entry_pending",
    }


@pytest.mark.parametrize("action", [
    {"op":"work"},
    {"op":"send"},
])
async def test_nonnull_action_with_extra_root_field_never_reaches_effects(action):
    async def query(_messages):
        return json.dumps({"action":action, "say":"進めるわ。",
            "type":"json_object"}, ensure_ascii=False)

    loop = CooperativeProviderLoop(ProviderRuntime(), query,
        lambda *_args:pytest.fail("invalid root cannot allocate"),
        provider="unavailable", context_requirements={}, owns_runtime=False,
        work_proposals_only=True)
    try:
        with pytest.raises(RoleDecisionUnavailable) as raised:
            await loop.submit("do it", turn_id="nonnull-extra")
        assert isinstance(raised.value.__cause__, LoopConflict)
        assert str(raised.value.__cause__) == "invalid coordination shape"
        assert loop.children == {} and loop.receipts == {}
    finally:
        await loop.close()


@pytest.mark.parametrize("target", ["自己紹介ページ", "work_item:invented"])
async def test_work_amend_reference_reaches_owner_without_execution(loop_host, target):
    loop, adapter, controls, _, delivered = loop_host
    text = "加一个头像，简单的 K 字母图标就好。"
    controls[text] = {"op":"work", "intent":"amend", "target":target}
    receipt = await loop.submit(text)
    assert receipt["state"] == "work_amend_resolution_required"
    assert receipt["target"] == target and receipt["text"] == text
    assert adapter.requests == [] and loop.children == {} and delivered == []


@pytest.mark.parametrize(("text", "action", "state"), [
    ("创建正式报告", {"op":"work", "intent":"execute",
        "source":"创建正式报告"}, "work_required"),
    ("修改 alpha.md", {"op":"work", "intent":"amend", "target":"alpha.md",
        "source":"修改 alpha.md"}, "work_amend_resolution_required"),
])
async def test_single_work_discards_only_an_exact_redundant_source(
        loop_host, text, action, state):
    loop, _, controls, _, _ = loop_host
    controls[text] = action
    receipt = await loop.submit(text)
    assert receipt["state"] == state
    assert receipt["coordination_say"] == "角色表达"
    assert "source" not in receipt


async def test_single_work_rejects_a_rewritten_redundant_source(loop_host):
    loop, _, controls, _, _ = loop_host
    controls["创建正式报告"] = {"op":"work", "intent":"execute",
        "source":"创建报告"}
    with pytest.raises(LoopConflict, match="Work intent"):
        await loop.submit("创建正式报告")


@pytest.mark.parametrize(("text", "guessed_target"), [
    ("打开一下关于你自己的维基百科。", "https://ja.wikipedia.org/wiki/牧瀬紅莉栖"),
    ("帮我打开一下哔哩哔哩。", "https://www.bilibili.com"),
])
async def test_professional_addressless_browser_open_becomes_source_only_work(
        loop_host, text, guessed_target):
    loop, adapter, controls, _, _ = loop_host
    loop.work_proposals_only = True
    controls[text] = {"op":"browser", "intent":"open", "target":guessed_target}

    receipt = await loop.submit(text)

    assert receipt["state"] == "work_plan_required"
    assert receipt["text"] == text
    assert "target" not in receipt
    decision_trace = next(row for row in reversed(loop.trace)
        if row.get("kind") == "decision")
    assert guessed_target in decision_trace["raw"]
    assert not adapter.requests and not loop.children

    pending = asyncio.get_running_loop().create_future()
    pending.set_result(AuipControlDecision(status="ok", action="engage",
        reason="entry_target_not_found"))
    release = asyncio.Event()
    decided = await loop._decide({"source":"user", "text":text,
        "input_id":"browser-auip-race", "turn_id":"browser-auip-race"},
        auip_entry={"pending":pending, "owns":lambda _decision:True,
            "focused":False, "prompt":"", "release":release})
    assert decided["action"] == {"op":"work"}
    assert release.is_set()


async def test_unbound_missing_app_entry_cannot_supersede_an_ordinary_work_proposal(
        loop_host):
    loop, _, controls, _, _ = loop_host
    loop.work_proposals_only = True
    text = "调查一个新的共识算法。"
    controls[text] = {"op":"work"}
    pending = asyncio.get_running_loop().create_future()
    pending.set_result(AuipControlDecision(status="ok", action="engage",
        reason="entry_target_not_found"))
    release = asyncio.Event()

    decided = await loop._decide({"source":"user", "text":text,
        "input_id":"missing-entry-work", "turn_id":"missing-entry-work"},
        auip_entry={"pending":pending, "owns":lambda _decision:True,
            "focused":False, "prompt":"", "release":release})

    assert decided["action"] == {"op":"work"}


@pytest.mark.parametrize(("decision", "expected"), [
    (AuipControlDecision(status="ok", action="launch"), {"op":"auip"}),
    (AuipControlDecision(status="ok", action="prepare"), {"op":"auip"}),
    (AuipControlDecision(status="ok", action="launch", timing="after_work"),
        {"op":"work"}),
])
async def test_compiled_app_entry_and_after_work_keep_existing_supersession(
        loop_host, decision, expected):
    loop, _, controls, _, _ = loop_host
    loop.work_proposals_only = True
    text = "打开已经找到的应用。"
    controls[text] = {"op":"work"}
    pending = asyncio.get_running_loop().create_future()
    pending.set_result(decision)
    release = asyncio.Event()

    decided = await loop._decide({"source":"user", "text":text,
        "input_id":"known-entry", "turn_id":"known-entry"},
        auip_entry={"pending":pending, "owns":lambda _decision:True,
            "focused":False, "prompt":"", "release":release})

    assert decided["action"] == expected


@pytest.mark.parametrize(("text", "model_target", "expected"), [
    ("打开 https://example.test/wiki。", "https://example.test/wiki",
        "https://example.test/wiki"),
    ("打开 bilibili.com。", "https://bilibili.com", "https://bilibili.com"),
])
async def test_professional_source_address_keeps_browser_open(
        loop_host, text, model_target, expected):
    loop, adapter, controls, _, _ = loop_host
    loop.work_proposals_only = True
    controls[text] = {"op":"browser", "intent":"open", "target":model_target}

    receipt = await loop.submit(text, browser_routing_scope={
        "state":"absent", "parent_session_id":"browser-source-test"})

    assert receipt["state"] == "browser_required"
    assert receipt["intent"] == "open"
    assert receipt["target"] == expected
    assert receipt["text"] == text
    assert not adapter.requests and not loop.children


async def test_professional_source_address_rejects_a_different_model_url(loop_host):
    loop, adapter, controls, _, _ = loop_host
    loop.work_proposals_only = True
    text = "打开 https://example.test/right。"
    controls[text] = {"op":"browser", "intent":"open",
        "target":"https://example.test/wrong"}

    with pytest.raises(LoopConflict, match="invalid cooperative Browser intent"):
        await loop.submit(text, browser_routing_scope={
            "state":"absent", "parent_session_id":"browser-source-test"})

    assert not adapter.requests and not loop.children and not loop.receipts


@pytest.mark.parametrize("intent", ["continue", "close"])
async def test_professional_browser_existing_branch_actions_stay_unchanged(
        loop_host, intent):
    loop, _, controls, _, _ = loop_host
    loop.work_proposals_only = True
    text = "继续当前网页。" if intent == "continue" else "关闭当前网页。"
    controls[text] = {"op":"browser", "intent":intent}

    decided = await loop._decide({"source":"user", "text":text,
        "input_id":"browser-" + intent, "turn_id":"browser-" + intent},
        browser_context={"model":{"status":"active"}})

    assert decided["action"] == {"op":"browser", "intent":intent}


async def test_work_report_batch_requires_two_exact_ordered_source_clauses(
        loop_host):
    loop, adapter, controls, _, delivered = loop_host
    text = "修改 alpha.md；顺便告诉我 beta.md 的状态。"
    controls[text] = {"op":"batch", "actions":[
        {"op":"work", "intent":"amend", "target":"alpha.md",
            "source":"修改 alpha.md"},
        {"op":"report", "target":"beta.md",
            "source":"顺便告诉我 beta.md 的状态。"}]}
    receipt = await loop.submit(text)
    assert receipt["state"] == "work_report_batch_required"
    assert delivered == []  # The domain owner has not accepted the plan yet.
    assert [(row["source_start"], row["source_end"])
        for row in receipt["batch_actions"]] == [(0, 11), (12, len(text))]
    assert adapter.requests == []

    controls["重复 alpha.md 然后 alpha.md"] = {"op":"batch", "actions":[
        {"op":"work", "intent":"amend", "target":"alpha.md",
            "source":"alpha.md"},
        {"op":"report", "target":"alpha.md", "source":"alpha.md"}]}
    with pytest.raises(LoopConflict, match="exact source clause"):
        await loop.submit("重复 alpha.md 然后 alpha.md")

    controls["状态 beta.md；修改 alpha.md"] = {"op":"batch", "actions":[
        {"op":"work", "intent":"amend", "target":"alpha.md",
            "source":"修改 alpha.md"},
        {"op":"report", "target":"beta.md", "source":"状态 beta.md"}]}
    with pytest.raises(LoopConflict, match="supported ordered"):
        await loop.submit("状态 beta.md；修改 alpha.md")

    same_turn = "创建一个计数器应用；完成后打开它，我们一起试一下。"
    controls[same_turn] = {"op":"batch", "actions":[
        {"op":"work", "intent":"execute", "source":"创建一个计数器应用"},
        {"op":"auip_after_work", "mode":"collaborate",
            "source":"完成后打开它，我们一起试一下。"}]}
    receipt = await loop.submit(same_turn)
    assert receipt["state"] == "work_auip_batch_required"
    assert delivered == []  # The accepted Work/AUIP owner publishes the line once.
    assert [row["op"] for row in receipt["batch_actions"]] == [
        "work", "auip_after_work"]
    assert [(row["source_start"], row["source_end"])
        for row in receipt["batch_actions"]] == [(0, 9), (10, len(same_turn))]

    invalid = "修改应用；完成后打开它。"
    controls[invalid] = {"op":"batch", "actions":[
        {"op":"work", "intent":"amend", "target":"应用",
            "source":"修改应用"},
        {"op":"auip_after_work", "mode":"observe",
            "source":"完成后打开它。"}]}
    with pytest.raises(LoopConflict, match="supported ordered"):
        await loop.submit(invalid)


async def test_scope_change_keeps_only_a_bounded_target_hint(loop_host):
    loop, _, controls, _, _ = loop_host
    controls["换到 OpenClaw"] = {"op":"scope_change", "target":"OpenClaw"}
    receipt = await loop.submit("换到 OpenClaw")
    assert receipt["state"] == "scope_change_required"
    assert receipt["target"] == "OpenClaw"

    controls["坏目标"] = {"op":"scope_change", "target":"x" * 161}
    with pytest.raises(LoopConflict, match="scope target"):
        await loop.submit("坏目标")


async def test_unconfigured_additional_provider_is_rejected_without_context(loop_host):
    loop, adapter, controls, _, _ = loop_host
    controls["另外让未知 Provider 做。"] = {
        "op":"delegate", "provider":"missing-provider"}
    receipt = await loop.submit("另外让未知 Provider 做。")
    assert receipt["state"] == "rejected"
    assert receipt["reason"] == "delegate_provider_unavailable"
    assert not loop.children and not adapter.requests


async def test_initial_destination_change_during_decision_creates_no_context(loop_host, tmp_path):
    loop, adapter, _, queries, _ = loop_host
    first = tmp_path/"initial-project"
    second = tmp_path/"replacement-project"
    first.mkdir()
    second.mkdir()
    selected = {"workspace":str(first.resolve())}

    def initial_destination(_provider, requirements):
        return {"requirements":replace(requirements, workspace_access="read"),
            "workspace":selected["workspace"],
            "workspace_route":{"status":"resolved", "source":"session_project",
                "projectId":selected["workspace"], "workItemId":"",
                "cwd":selected["workspace"]}}

    async def changing_query(messages):
        frame = json.loads(messages[-1]["content"])
        queries.append(frame)
        assert frame["context"]["workspace"] is None
        assert frame["context"]["initial_destination"]["workspace"] == (
            str(first.resolve()))
        assert frame["context"]["requirements"]["workspace_access"] == "write"
        assert frame["context"]["initial_destination"]["requirements"][
            "workspace_access"] == "read"
        selected["workspace"] = str(second.resolve())
        return json.dumps({"say":"確認する。", "action":{"op":"send"}},
            ensure_ascii=False)

    loop.initial_destination = initial_destination
    loop.query = changing_query
    with pytest.raises(LoopConflict, match="initial context destination changed"):
        await loop.submit("检查项目。")
    assert not loop.children and not adapter.requests


class Adapter:
    provider_id = "loop-test"
    manifest = ProviderManifest(provider_id=provider_id, display_name="Loop test",
        capabilities=ProviderCapabilities(workspace_access="write", workspace_ownership="caller",
            resume="attach", append_input=True, cancellation="confirmed"))

    def __init__(self):
        self.requests = []
        self.inputs = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.cancelled = set()
        self.stop_confirmed = True

    async def run(self, request, run_id, emit):
        self.requests.append((request, run_id))
        self.started.set()
        await self.release.wait()
        return ProviderRunResult(status="cancelled" if run_id in self.cancelled else "done",
            result="需要选哪一种？", session=request.session or ProviderSessionHandle(
                provider=self.provider_id, session_id="native-" + run_id, scope="interaction"))

    async def append_input(self, run_id, text):
        self.inputs.append((run_id, text))
        return ProviderInputDelivery("delivered")

    async def cancel(self, run_id):
        if not self.stop_confirmed:
            return {"confirmed":False, "cancelled":False, "reason":"unknown"}
        self.cancelled.add(run_id)
        self.release.set()
        return {"confirmed":True, "cancelled":True}


@pytest.fixture
async def loop_host(tmp_path):
    runtime = ProviderRuntime()
    adapter = Adapter()
    runtime.register(adapter)
    queries, delivered = [], []
    controls = {}
    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        queries.append(frame)
        control = controls.get(frame["current"].get("text")) if frame["source_kind"] == "user" else controls.get("provider")
        action = control(frame) if callable(control) else control
        if frame["source_kind"] != "user" and action is None:
            return "角色表达"
        return json.dumps({"say":"角色表达", "action":action}, ensure_ascii=False)
    def allocate(label, child_id):
        path = tmp_path / child_id
        path.mkdir()
        return path
    def publish(event):
        delivered.append(event)
        return True
    loop = CooperativeProviderLoop(runtime, query, allocate, provider=adapter.provider_id, publish=publish,
        context_requirements={adapter.provider_id:POLICY})
    try:
        yield loop, adapter, controls, queries, delivered
    finally:
        adapter.stop_confirmed = True
        adapter.release.set()
        await loop.close()


def send_first(frame):
    return {"op":"send"}


@pytest.mark.parametrize("expression_fails", [False, True])
async def test_native_result_settles_shared_presentation_once_even_on_expression_failure(
        loop_host, expression_fails):
    loop, adapter, controls, queries, delivered = loop_host
    controls["开始"] = {"op":"send"}
    settled = []
    async def finish_execution(run_id):
        settled.append(run_id)
        if not expression_fails:
            assert sum(row["cause"] == run_id for row in delivered) == 1
    loop.publish.finish_execution = finish_execution
    original_query = loop.query
    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        if expression_fails and frame["source_kind"] == "provider":
            raise RuntimeError("expression unavailable")
        return await original_query(messages)
    loop.query = query
    receipt = await loop.submit("开始")
    if expression_fails:
        with pytest.raises(RuntimeError, match="expression unavailable"):
            await loop.wait()
    else:
        await loop.wait()
        assert sum(row["source_kind"] == "provider" for row in queries) == 1
    assert settled == [receipt["run_id"]]
    assert len(adapter.requests) == 1


async def test_native_context_exposes_reported_progress_and_exact_pending_permission(loop_host):
    from types import SimpleNamespace
    from unittest.mock import Mock
    loop, adapter, controls, _, _ = loop_host
    adapter.release.clear()
    controls["开始"] = {"op":"send"}
    receipt = await loop.submit("开始")
    run = loop.runtime.get_run(receipt["run_id"])
    run.metadata["session_id"] = "current-session"
    run.events.append({"type":"semantic.progress", "payload":{
        "summary":"The game file passed a structural check; visual preview unavailable."}})
    permissions = Mock(return_value=[SimpleNamespace(reason="Copy the game to Desktop?",
        action="execute_command", scope_paths=["game.html"])])
    loop.workspace_leases = SimpleNamespace(list_cooperative_permission_requests=permissions)
    facts = loop.context_facts(receipt["child_id"])
    assert "structural check" in facts["last_run"]["reported_progress"]
    assert facts["last_run"]["pending_permissions"] == [{
        "reason":"Copy the game to Desktop?", "action":"execute_command", "scope":["game.html"]}]
    permissions.assert_called_once_with("current-session", context_id=receipt["child_id"],
        provider_run_id=run.run_id, status="pending")
    run.status = "done"
    assert "pending_permissions" not in loop.context_facts(receipt["child_id"])["last_run"]
    assert permissions.call_count == 1
    loop.workspace_leases = None
    adapter.release.set()
    await loop.wait()


async def test_idle_question_answer_keeps_child_and_native_context(loop_host):
    loop, adapter, controls, queries, delivered = loop_host
    controls["开始"] = {"op":"send"}
    controls["第二种"] = send_first
    first = await loop.submit("开始", input_id="u1")
    await loop.wait()
    assert len(delivered) == 2  # accepted acknowledgement, then Provider expression
    second = await loop.submit("第二种", input_id="u2")
    await loop.wait()
    assert first["child_id"] == second["child_id"] and first["run_id"] != second["run_id"]
    assert len(loop.children) == 1 and len(adapter.requests) == 2
    assert adapter.requests[1][0].task == "第二种"
    assert adapter.requests[1][0].session.session_id == "native-" + first["run_id"]
    assert adapter.requests[0][0].cwd == adapter.requests[1][0].cwd
    assert all("work" not in request.metadata for request, _ in adapter.requests)
    before = len(queries)
    assert await loop.submit("第二种", input_id="u2") == second
    assert len(queries) == before and len(adapter.requests) == 2
    with pytest.raises(LoopConflict, match="different text"):
        await loop.submit("第一种", input_id="u2")


async def test_active_messages_append_without_new_run_or_latest_wins(loop_host):
    loop, adapter, controls, _, _ = loop_host
    adapter.release.clear()
    controls["开始"] = {"op":"send"}
    controls["只有CPU"] = controls["不能联网"] = send_first
    first = await loop.submit("开始")
    await asyncio.wait_for(adapter.started.wait(), 2)
    a = await loop.submit("只有CPU")
    b = await loop.submit("不能联网")
    assert a["state"] == b["state"] == "delivered"
    assert adapter.inputs == [(first["run_id"], a["delivered_text"]), (first["run_id"], b["delivered_text"])]
    assert a["text"] == "只有CPU" and b["text"] == "不能联网"
    assert a["delivered_text"].startswith("只有CPU") and b["delivered_text"].startswith("不能联网")
    assert len(adapter.requests) == len(loop.runtime.list_runs()) == 1
    assert adapter.requests[0][0].task == "开始"
    adapter.release.set()
    await loop.wait()


async def test_unknown_recipient_and_ambiguous_no_action_do_not_spawn(loop_host):
    loop, adapter, controls, _, _ = loop_host
    controls["修改海战"] = {"op":"send", "recipient":"not-a-real-child"}
    with pytest.raises(LoopConflict, match="bound action"):
        await loop.submit("修改海战")
    controls["那个改一下"] = None  # coordinator asks clarification
    receipt = await loop.submit("那个改一下")
    assert receipt["state"] == "no_action" and not loop.children and not adapter.requests


async def test_provider_cannot_authorize_a_new_child(loop_host):
    loop, adapter, controls, queries, delivered = loop_host
    controls["开始"] = {"op":"send"}
    controls["provider"] = {"op":"spawn", "label":"未经用户要求的上下文"}
    receipt = await loop.submit("开始")
    await loop.wait()
    assert len(loop.children) == len(adapter.requests) == 1
    assert any(event["cause"] == receipt["run_id"] for event in delivered)
    assert loop.presentation_system != loop.system
    assert "自然な役割応答そのもの" in loop.presentation_system
    assert "source_kind=provider" in loop.presentation_system
    assert "turn_status" in loop.presentation_system
    assert set(queries[0]) == {"source_kind", "current", "context", "history",
        "available_delegate_providers", "retained_contexts", "retained_contexts_complete"}
    assert set(queries[1]) == {"source_kind", "current"}
    assert not any(row["kind"] == "presentation_action_discarded" for row in loop.trace)
    assert any('"op": "spawn"' in event["text"] for event in delivered)


async def test_legacy_provider_message_intermediates_keep_role_intention(loop_host):
    loop, adapter, controls, _, _ = loop_host
    adapter.release.clear()
    controls["开始"] = {"op":"send"}
    first = await loop.submit("开始")
    await asyncio.wait_for(adapter.started.wait(), 2)
    loop.active_work = lambda context_id:{"work_item_id":"work-current",
        "run_id":first["run_id"]} if context_id == first["child_id"] else None

    controls["补充条件"] = {"op":"send"}
    active = await loop.submit("补充条件")
    assert active["state"] == "work_input_required"
    assert active["coordination_say"] == "角色表达"
    assert adapter.inputs == []

    controls["问先前任务"] = {"op":"send_to", "target":"先前任务"}
    addressed = await loop.submit("问先前任务")
    assert addressed["state"] == "task_address_resolution_required"
    assert addressed["coordination_say"] == "角色表达"
    adapter.release.set()
    await loop.wait()


@pytest.mark.parametrize("source", ["provider", "host_receipt"])
@pytest.mark.parametrize("raw", [
    "準備が整ったわ。今、左へ一度動かすわ。",
    '{"say":"quoted data","action":{"op":"send"}}',
    '[DELEGATE provider="fake" task="create a file"]',
    'コード例: `{"op":"work","intent":"execute"}`',
])
async def test_fact_expression_never_interprets_execution_syntax(loop_host, source, raw):
    loop, adapter, _controls, queries, delivered = loop_host

    async def query(messages):
        queries.append(json.loads(messages[-1]["content"]))
        return raw

    loop.query = query
    result = await loop._decide({"source":source, "text":"accepted fact"})
    assert result == {"say":raw, "action":None}
    await loop._express_and_deliver({"source":source, "text":"accepted fact"}, cause="fact")
    assert delivered[-1]["text"] == raw
    assert adapter.requests == [] and loop.children == {} and loop.receipts == {}


async def test_auip_receipt_expression_adds_only_identity_bound_current_context():
    frames = []
    requested = []

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        frames.append(frame)
        return "今は操作していないわ。"

    def app_context(app_session_id):
        requested.append(app_session_id)
        return "current controller=idle" if app_session_id == "app-current" else ""

    loop = CooperativeProviderLoop(
        ProviderRuntime(),
        query,
        lambda *_args: pytest.fail("presentation cannot allocate"),
        provider="unavailable",
        context_requirements={},
        role_app_context=app_context,
        owns_runtime=False,
    )
    try:
        historical = {
            "source": "host_receipt",
            "state": "auip_applied",
            "app_session_id": "app-current",
            "outcome": {"controller": {"status": "stopping"}},
        }
        result = await loop._decide(historical)
        assert result["say"] == "今は操作していないわ。"
        assert frames[-1] == {
            "source_kind": "host_receipt",
            "current": historical,
            "app_context": "current controller=idle",
        }
        assert requested == ["app-current"]

        await loop._decide({"source": "host_receipt", "state": "work_started",
            "outcome": {"ok": True}})
        assert set(frames[-1]) == {"source_kind", "current"}
        assert requested == ["app-current"]

        await loop._decide({"source": "provider", "app_session_id": "app-current",
            "text": "A Provider report is not an AUIP receipt."})
        assert set(frames[-1]) == {"source_kind", "current"}
        assert requested == ["app-current"]

        nested = {"source": "host_receipt", "state": "auip_applied",
            "outcome": {"app_session_id": "app-current",
                "controller": {"status": "stopping"}}}
        await loop._decide(nested)
        assert frames[-1]["current"] == nested
        assert frames[-1]["app_context"] == "current controller=idle"
        assert requested == ["app-current", "app-current"]
    finally:
        await loop.close()


@pytest.mark.parametrize("raw", ["自然な返信だけ。", '{"op":"work","intent":"execute"}'])
async def test_user_source_still_requires_the_control_json_envelope(loop_host, raw):
    loop, adapter, *_ = loop_host

    async def query(messages):
        return raw

    loop.query = query
    with pytest.raises(LoopConflict, match="invalid coordination"):
        await loop.submit("Create a report")
    assert adapter.requests == [] and loop.children == {} and loop.receipts == {}


async def test_stop_and_close_are_distinct_and_closed_context_stays_closed(loop_host):
    loop, adapter, controls, _, _ = loop_host
    adapter.release.clear()
    controls["开始"] = {"op":"send"}
    controls["先停下"] = lambda f:{"op":"interrupt"}
    controls["接着改"] = send_first
    first = await loop.submit("开始")
    await asyncio.wait_for(adapter.started.wait(), 2)
    stopped = await loop.submit("先停下")
    assert stopped["state"] == "stopped" and not loop.children[first["child_id"]].closed
    await loop.wait()
    closed = await loop._apply({"op":"close", "recipient":first["child_id"]}, "")
    assert closed["state"] == "closed"
    with pytest.raises(LoopConflict, match="closed"):
        await loop.submit("接着改")
    assert len(adapter.requests) == 1


async def test_unconfirmed_stop_does_not_close_context(loop_host):
    loop, adapter, controls, _, _ = loop_host
    adapter.release.clear()
    adapter.stop_confirmed = False
    controls["开始"] = {"op":"send"}
    first = await loop.submit("开始")
    await asyncio.wait_for(adapter.started.wait(), 2)
    result = await loop._apply({"op":"close", "recipient":first["child_id"]}, "")
    assert result["state"] == "unknown" and not loop.children[first["child_id"]].closed


async def test_additional_delegation_does_not_replace_binding_and_host_can_switch(loop_host):
    loop, adapter, controls, _, _ = loop_host
    controls["清单"] = {"op":"send"}
    first = await loop.submit("清单")
    await loop.wait()
    other = Adapter()
    other.provider_id = "other-provider"
    other.manifest = ProviderManifest(provider_id=other.provider_id, display_name="Other",
        capabilities=ProviderCapabilities(workspace_access="write", workspace_ownership="caller", resume="attach"))
    loop.runtime.register(other)
    loop.context_requirements[other.provider_id] = POLICY
    second = await loop._apply({"op":"spawn", "label":"诗歌", "provider":other.provider_id}, "诗歌")
    await loop.wait()
    assert loop.bound_context_id == first["child_id"]
    controls["清单补充"] = {"op":"send"}
    third = await loop.submit("清单补充")
    await loop.wait()
    assert third["child_id"] == first["child_id"] != second["child_id"]
    assert adapter.requests[1][0].session.session_id == "native-" + first["run_id"]
    assert adapter.requests[1][0].cwd != other.requests[0][0].cwd
    assert len(loop.children) == 2
    loop.bind_context(second["child_id"])
    controls["诗歌补充"] = {"op":"send"}
    fourth = await loop.submit("诗歌补充")
    await loop.wait()
    assert fourth["child_id"] == second["child_id"]
    assert other.requests[1][0].provider == other.provider_id
    assert other.requests[1][0].session.provider == other.provider_id
    assert other.requests[1][0].session.session_id == "native-" + second["run_id"]


async def test_result_from_retained_context_keeps_origin_without_internal_id(loop_host):
    loop, adapter, controls, queries, _ = loop_host
    adapter.release.clear()
    controls["开始 A"] = {"op":"send"}
    started = await loop.submit("开始 A")
    await adapter.started.wait()

    replacement = loop._create_context("另一个上下文", adapter.provider_id)
    loop.bind_context(replacement.child_id)
    adapter.release.set()
    await loop.wait()

    presentation = next(frame for frame in reversed(queries)
        if frame["source_kind"] == "provider")
    assert presentation["current"]["run_id"] == started["run_id"]
    assert presentation["current"]["binding_relation_at_observation"] == "retained"
    assert presentation["current"]["provider"] == adapter.provider_id
    assert "child_id" not in presentation["current"]
    assert loop.bound_context_id == replacement.child_id


async def test_scope_change_is_a_proposal_without_implicit_new_context(loop_host):
    loop, adapter, controls, _, _ = loop_host
    controls["开始"] = {"op":"send"}
    first = await loop.submit("开始")
    await loop.wait()
    controls["换工作区"] = {"op":"scope_change"}
    changed = await loop.submit("换工作区")
    assert changed["state"] == "scope_change_required"
    assert loop.bound_context_id == first["child_id"] and len(loop.children) == 1
    assert len(adapter.requests) == 1
    controls["偷偷新建"] = {"op":"spawn", "label":"new"}
    with pytest.raises(LoopConflict, match="bound action"):
        await loop.submit("偷偷新建")


async def test_rebinding_even_to_same_address_retires_a_pending_decision(loop_host):
    loop, adapter, controls, _, _ = loop_host
    controls["开始"] = {"op":"send"}
    first = await loop.submit("开始")
    await loop.wait()
    entered, release = asyncio.Event(), asyncio.Event()
    async def query(messages):
        entered.set()
        await release.wait()
        return json.dumps({"say":"continue", "action":{"op":"send"}})
    loop.query = query
    pending = asyncio.create_task(loop.submit("继续"))
    await entered.wait()
    loop.bind_context(first["child_id"])
    release.set()
    with pytest.raises(LoopConflict, match="binding changed"):
        await pending
    assert len(adapter.requests) == 1


async def test_binding_change_does_not_retarget_already_accepted_input(loop_host, monkeypatch):
    loop, adapter, controls, _, _ = loop_host
    adapter.release.clear()
    controls["开始"] = controls["补充"] = {"op":"send"}
    first = await loop.submit("开始")
    await adapter.started.wait()
    entered, acknowledge = asyncio.Event(), asyncio.Event()
    async def append(run_id, text):
        adapter.inputs.append((run_id, text))
        entered.set()
        await acknowledge.wait()
        return ProviderInputDelivery("delivered")
    monkeypatch.setattr(adapter, "append_input", append)
    pending = asyncio.create_task(loop.submit("补充"))
    await entered.wait()
    other = loop._create_context("Host-prepared scope", adapter.provider_id)
    loop.bind_context(other.child_id)
    acknowledge.set()
    receipt = await pending
    assert receipt["child_id"] == first["child_id"]
    assert adapter.inputs[0][0] == first["run_id"]
    assert loop.bound_context_id == other.child_id
    assert len(adapter.requests) == 1


async def test_shutdown_during_coordination_does_not_start_a_late_child(loop_host):
    loop, adapter, _, _, _ = loop_host
    entered, release = asyncio.Event(), asyncio.Event()
    async def query(messages):
        entered.set()
        await release.wait()
        return json.dumps({"say":"开始", "action":{"op":"send"}})
    loop.query = query
    pending = asyncio.create_task(loop.submit("开始"))
    await entered.wait()
    closing = asyncio.create_task(loop.close())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(LoopConflict, match="closed"):
        await pending
    await closing
    assert not loop.children and not adapter.requests


@pytest.mark.parametrize("hint", ["typed", "raw", "foreign"])
async def test_hard_cancel_retains_only_a_valid_typed_context(loop_host, monkeypatch, hint):
    loop, adapter, controls, _, _ = loop_host
    adapter.release.clear()
    controls["开始"] = {"op":"send"}
    controls["先停下"] = lambda f:{"op":"interrupt"}
    controls["继续"] = send_first
    async def cancel(run_id):
        session = ProviderSessionHandle(provider="wrong" if hint == "foreign" else adapter.provider_id,
            session_id="native-" + run_id, scope="interaction")
        return {"confirmed":True, "cancelled":True,
                "session":session.to_dict() if hint == "raw" else session}
    monkeypatch.setattr(adapter, "cancel", cancel)
    first = await loop.submit("开始")
    await asyncio.wait_for(adapter.started.wait(), 2)
    stopped = await loop.submit("先停下")
    await loop.wait()  # domain cancellation is an outcome, not a cancelled observer
    assert stopped["state"] == "stopped"
    facts = loop.snapshot()[0]
    assert facts["last_run"]["status"] == "cancelled" and not facts["closed"]
    assert facts["workspace"] == adapter.requests[0][0].cwd
    adapter.release.set()
    continued = await loop.submit("继续")
    if hint == "typed":
        assert continued["state"] == "started" and continued["child_id"] == first["child_id"]
        await loop.wait()
        assert adapter.requests[1][0].session.session_id == "native-" + first["run_id"]
    else:
        assert continued["state"] == "rejected" and len(adapter.requests) == 1


async def test_parent_discussion_can_bootstrap_a_child_from_a_short_answer(loop_host):
    loop, adapter, controls, _, _ = loop_host
    controls["帮我建购物清单"] = None
    await loop.submit("帮我建购物清单")
    controls["牛奶和面包"] = {"op":"send"}
    await loop.submit("牛奶和面包")
    await loop.wait()
    request = adapter.requests[0][0]
    assert request.task == "牛奶和面包"
    assert "帮我建购物清单" in request.metadata["source_user_context"]
    assert "Main Chat" in request.metadata["source_user_context"]


@pytest.mark.parametrize(("visible", "voice_status"), [(True,"queued"), (True,"dropped"), (False,"queued")])
async def test_host_display_and_voice_receipts_are_distinct(loop_host, visible, voice_status):
    from server.cooperative_delivery import CooperativeHostDelivery

    loop, adapter, controls, _, _ = loop_host
    entered, release = asyncio.Event(), asyncio.Event()
    displays, voices = [], []
    async def display(event):
        displays.append(event)
        entered.set()
        await release.wait()
        return visible
    async def voice(payload):
        voices.append(payload)
        return {"status":voice_status, "reason":"test_sink"}
    publication = CooperativeHostDelivery(session_id="host-session", display=display, narration_sink=voice)
    loop.publish = publication
    controls["开始"] = {"op":"send"}
    pending = asyncio.create_task(loop.submit("开始", input_id="host-input"))
    await asyncio.wait_for(entered.wait(), 2)
    assert not pending.done() and not any(row["source"] == "kurisu" for row in loop.history)
    release.set()
    accepted = await pending
    await loop.wait()
    assert len(adapter.requests) == 1  # presentation never dispatches execution
    assert {row["cause"] for row in displays} == {"host-input", accepted["run_id"]}
    assert all(row["session_id"] == "host-session" for row in displays)
    assert len([row for row in loop.history if row["source"] == "kurisu"]) == (2 if visible else 0)
    assert len(voices) == (2 if visible else 0)
    assert all(row["published"] == visible for row in publication.receipts)
    if visible:
        assert all(row["narration"]["accepted"] == (voice_status == "queued") for row in publication.receipts)
        for output, payload in zip(displays, voices):
            assert payload["display_text"] == output["text"]
            assert payload["voice_text_ja"] == output["text"]
            assert payload["display_language"] == "japanese"
            assert payload["source"] == "cooperative_chat"
            assert payload["line_id"] == payload["turn_id"] == output["cause"]
            assert payload["complete_turn"] is True
            assert payload["_narration_delivery"] == {
                "source_kind":"host", "source_id":output["cause"], "session_id":"host-session",
                    "request_id":f"cooperative:host-session:{output['cause']}"}


async def test_display_ack_wait_does_not_hold_the_semantic_input_lock(loop_host):
    loop, adapter, controls, queries, _ = loop_host
    display_entered, release_display = asyncio.Event(), asyncio.Event()
    displayed = []

    async def slow_display(event):
        displayed.append(event)
        if len(displayed) == 1:
            display_entered.set()
            await release_display.wait()
        return True

    loop.publish = slow_display
    adapter.release.clear()
    controls["只聊聊"] = None
    controls["开始执行"] = {"op":"send"}
    first = asyncio.create_task(loop.submit("只聊聊", input_id="display-first"))
    await asyncio.wait_for(display_entered.wait(), 2)
    second = asyncio.create_task(loop.submit("开始执行", input_id="display-second"))
    await asyncio.wait_for(adapter.started.wait(), 2)
    assert len(queries) >= 2
    assert loop.bound_context_id and len(loop.runtime.list_runs()) == 1
    assert not first.done() and not second.done()
    release_display.set()
    assert (await first)["state"] == "no_action"
    assert (await second)["state"] == "started"
    adapter.release.set()
    await loop.wait()


async def test_unknown_receipt_expression_does_not_delay_natural_stop(loop_host, monkeypatch):
    loop, adapter, _controls, _queries, delivered = loop_host
    explanation_entered, release_explanation = asyncio.Event(), asyncio.Event()
    cancel_seen = asyncio.Event()

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        current = frame["current"]
        if frame["source_kind"] == "host_receipt":
            explanation_entered.set()
            await release_explanation.wait()
            return "追加是否送达还不能确认。"
        action = None
        if frame["source_kind"] == "user":
            action = {"op":"interrupt" if current["text"] == "先停下" else "send"}
        else:
            return "好的。"
        return json.dumps({"say":"好的。", "action":action}, ensure_ascii=False)

    loop.query = query
    adapter.release.clear()
    original_cancel = adapter.cancel

    async def observed_cancel(run_id):
        outcome = await original_cancel(run_id)
        cancel_seen.set()
        return outcome

    async def unknown_append(run_id, text):
        adapter.inputs.append((run_id, text))
        return ProviderInputDelivery("unknown", "transport_receipt_missing")

    monkeypatch.setattr(adapter, "cancel", observed_cancel)
    monkeypatch.setattr(adapter, "append_input", unknown_append)
    started = await loop.submit("开始", input_id="start")
    await asyncio.wait_for(adapter.started.wait(), 2)
    uncertain = asyncio.create_task(loop.submit("追加", input_id="unknown"))
    await asyncio.wait_for(explanation_entered.wait(), 2)
    stopping = asyncio.create_task(loop.submit("先停下", input_id="stop"))
    await asyncio.wait_for(cancel_seen.wait(), 2)
    assert started["run_id"] in adapter.cancelled
    assert not uncertain.done() and not stopping.done()
    release_explanation.set()
    assert (await uncertain)["state"] == "unknown"
    assert (await stopping)["state"] == "stopped"
    await loop.wait()
    causes = [row["cause"] for row in delivered]
    assert causes.index("unknown") < causes.index("stop")


async def test_provider_result_expression_does_not_delay_next_execution(loop_host):
    loop, adapter, _controls, _queries, delivered = loop_host
    result_expression_entered, release_expression = asyncio.Event(), asyncio.Event()

    async def query(messages):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] == "provider":
            result_expression_entered.set()
            await release_expression.wait()
            return "上一轮结果。"
        return json.dumps({"say":"好的。", "action":{"op":"send"}}, ensure_ascii=False)

    loop.query = query
    adapter.release.clear()
    first = await loop.submit("第一轮", input_id="first")
    await asyncio.wait_for(adapter.started.wait(), 2)
    adapter.release.set()
    await asyncio.wait_for(result_expression_entered.wait(), 2)
    adapter.started.clear()
    adapter.release.clear()
    second_task = asyncio.create_task(loop.submit("第二轮", input_id="second"))
    await asyncio.wait_for(adapter.started.wait(), 2)
    assert len(adapter.requests) == 2 and not second_task.done()
    release_expression.set()
    second = await second_task
    assert second["state"] == "started" and second["run_id"] != first["run_id"]
    causes = [row["cause"] for row in delivered]
    assert causes.index(first["run_id"]) < causes.index("second")
    adapter.release.set()
    await loop.wait()


async def test_caller_cancellation_does_not_duplicate_async_publication(loop_host):
    loop, adapter, controls, _, _ = loop_host
    entered, release = asyncio.Event(), asyncio.Event()
    shown = []
    async def display(event):
        entered.set()
        await release.wait()
        shown.append(event)
        return True
    loop.publish = display
    controls["开始"] = {"op":"send"}
    caller = asyncio.create_task(loop.submit("开始", input_id="cancelled-caller"))
    await asyncio.wait_for(entered.wait(), 2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    replay = asyncio.create_task(loop.submit("开始", input_id="cancelled-caller"))
    release.set()
    receipt = await replay
    await loop.wait()
    assert len(adapter.requests) == 1
    assert [event["cause"] for event in shown] == ["cancelled-caller", receipt["run_id"]]


@pytest.mark.parametrize("abort_before_decision", [False, True])
async def test_real_chat_grant_controls_cooperative_action_acceptance(loop_host, tmp_path, monkeypatch, abort_before_decision):
    from unittest.mock import AsyncMock
    from core import session_manager as sm
    import core.turn_coordinator as tc
    from server.cooperative_chat_ingress import CooperativeChatIngress
    from server.control_ledger import ControlLedgerStore

    loop, adapter, controls, _, _ = loop_host
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    sm.create_session("chat-A")
    owner = tc.TurnCoordinator()
    monkeypatch.setattr(tc, "coordinator", owner)
    emit = AsyncMock()
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", emit)
    controls["开始"] = {"op":"send"}
    entered, release = asyncio.Event(), asyncio.Event()
    query = loop.query
    async def paused_query(messages):
        if json.loads(messages[-1]["content"])["source_kind"] == "user":
            entered.set()
            await release.wait()
        return await query(messages)
    loop.query = paused_query
    ledger = ControlLedgerStore(tmp_path/"control.sqlite3")
    host = CooperativeChatIngress(loop, session_id="chat-A", ledger=ledger, fence_scope="foreground")
    request = {"text":"开始", "session_id":"chat-A",
        "turn_id":"chat-input", "utterance_id":"chat-input"}
    try:
        response = await host.handler._handle_send(request)
        assert response["status"] == "ok"
        await asyncio.wait_for(entered.wait(), 2)
        assert owner.snapshot()["active_turn_id"] == "chat-input"
        source = ledger.find_admission("chat:chat-A", "chat-input")
        assert source["authority_mode"] == "turn_decision" and source["plan_id"] is None
        pending_task = host.handler._stream_task
        replay = await host.handler._handle_send({**request, "turn_id":"transport-alias"})
        assert replay["status"] == "replayed"
        assert host.handler._stream_task is pending_task and not pending_task.done()
        with pytest.raises(tc.TurnAuthorityError, match="changed"):
            await host.handler._handle_send({**request, "text":"不同的要求"})
        assert owner.snapshot()["active_turn_id"] == "chat-input"
        if abort_before_decision:
            await host.handler._handle_abort({"turn_id":"chat-input"})
            release.set()
            with pytest.raises(tc.TurnAuthorityError, match="no longer authorizes"):
                await loop._inputs["chat-input"][1]
            assert not loop.children and not adapter.requests
        else:
            release.set()
            await host.handler._stream_task
            await loop.wait()
            assert host.receipts["chat-input"]["state"] == "started"
            assert len(adapter.requests) == 1
            accepted = ledger.find_admission("chat:chat-A", "chat-input")
            assert accepted["plan_id"]
            effect = ledger.get_effect(
                "cooperative-effect-" + uuid.uuid5(
                    uuid.UUID("69a2e98d-c2ae-4a4e-befe-936430045a2a"),
                    "effect:" + source["root_id"],
                ).hex
            )
            assert effect["kind"] == "provider" and effect["state"] == "terminal"
            assert ledger.get_receipt(effect["effect_id"])["external_id"] == (
                host.receipts["chat-input"]["run_id"]
            )
            assert any(call.args[0] == "chat.complete" for call in emit.await_args_list)
        assert host.handler._interaction_branch_router is None
    finally:
        release.set()
        await host.close()
        ledger.close()

    # Reopen the durable source store with entirely new ingress/runtime state.
    # Even a source cancelled before dispatch must not become a fresh execution.
    ledger = ControlLedgerStore(tmp_path/"control.sqlite3")
    resumed_runtime, resumed_adapter = ProviderRuntime(), Adapter()
    owner = tc.TurnCoordinator()
    monkeypatch.setattr(tc, "coordinator", owner)
    resumed_runtime.register(resumed_adapter)
    resumed_queries = []
    async def resumed_query(messages):
        resumed_queries.append(messages)
        return await query(messages)
    resumed_loop = CooperativeProviderLoop(resumed_runtime, resumed_query, loop.allocate,
        context_requirements={resumed_adapter.provider_id:POLICY},
        provider=resumed_adapter.provider_id, publish=lambda event: True)
    resumed = CooperativeChatIngress(resumed_loop, session_id="chat-A", ledger=ledger, fence_scope="foreground")
    try:
        epoch = owner.snapshot()["epochs"]["chat"]
        replay = await resumed.handler._handle_send(request)
        assert replay["status"] == "replayed" and replay["root_id"] == source["root_id"]
        assert owner.snapshot()["epochs"]["chat"] == epoch
        assert not resumed_queries and not resumed_adapter.requests
        if abort_before_decision:
            assert not resumed_loop.children
        else:
            assert resumed_loop.bound_context_id == loop.bound_context_id
            restored = resumed_loop.get_context(resumed_loop.bound_context_id)
            original = loop.children[loop.bound_context_id]
            assert restored.workspace == original.workspace
            assert restored.native_session == original.native_session
        # Source replay is not a global dialogue lock. A new comment can be
        # interpreted; this test does not authorize a replacement native context.
        await resumed.handler._handle_send({**request, "text":"收到", "turn_id":"new-input", "utterance_id":"new-input"})
        await resumed.handler._stream_task
        await resumed_loop.wait()
        assert len(resumed_queries) == 1 and not resumed_adapter.requests
        assert resumed.receipts["new-input"]["state"] == "no_action"
        assert ledger.pending_effects() == []
    finally:
        await resumed.close()
        ledger.close()


async def test_real_chat_outbox_covers_start_append_interrupt_and_zero_effect(
        loop_host, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from core import session_manager as sm
    import core.turn_coordinator as tc
    from server.cooperative_chat_ingress import CooperativeChatIngress
    from server.control_ledger import ControlLedgerStore

    loop, adapter, controls, _, _ = loop_host
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    sm.create_session("chat-effects")
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", AsyncMock())
    controls.update({"开始":{"op":"send"}, "补充":{"op":"send"},
        "只聊聊":None, "先停下":{"op":"interrupt"}})
    adapter.release.clear()
    ledger = ControlLedgerStore(tmp_path/"control.sqlite3")
    host = CooperativeChatIngress(loop, session_id="chat-effects", ledger=ledger,
        fence_scope="foreground")
    install_stop_owner(host)

    async def send(text, source, turn):
        result = await host.handler._handle_send({"text":text,
            "session_id":"chat-effects", "turn_id":turn,
            "utterance_id":source})
        assert result["status"] == "ok"
        await host.handler._stream_task
        return host.receipts[source]

    try:
        started = await send("开始", "source-start", "turn-start")
        await asyncio.wait_for(adapter.started.wait(), 2)
        appended = await send("补充", "source-append", "turn-append")
        discussed = await send("只聊聊", "source-chat", "turn-chat")
        stopped = await send("先停下", "source-stop", "turn-stop")
        await loop.wait()
        assert started["state"] == "started"
        assert appended["state"] == "delivered"
        assert discussed["state"] == "no_action"
        assert stopped["state"] == "stopped"
        assert len(adapter.requests) == 1 and len(adapter.inputs) == 1
        assert "work" not in adapter.requests[0][0].metadata

        with ledger._lock:
            rows = [dict(row) for row in ledger._db.execute("""SELECT
                a.utterance_id,a.plan_id,e.* FROM control_admissions a
                LEFT JOIN control_effect_outbox e ON e.root_id=a.root_id
                WHERE a.source_scope='chat:chat-effects'
                ORDER BY a.chat_epoch""").fetchall()]
        by_source = {row["utterance_id"]:row for row in rows}
        assert by_source["source-chat"]["plan_id"]
        assert by_source["source-chat"]["effect_id"] is None
        assert {source:json.loads(by_source[source]["payload_json"])["operation"]
            for source in ("source-start", "source-append", "source-stop")} == {
                "source-start":"start", "source-append":"append",
                "source-stop":"interrupt"}
        for source in ("source-start", "source-append", "source-stop"):
            effect = by_source[source]
            assert effect["kind"] == "provider" and effect["state"] == "terminal"
            assert ledger.get_receipt(effect["effect_id"])["outcome"] in {
                "succeeded", "cancelled"}
        assert ledger.get_receipt(by_source["source-start"]["effect_id"])[
            "external_id"] == started["run_id"]
        assert ledger.get_receipt(by_source["source-append"]["effect_id"])[
            "external_id"] == started["run_id"] + ":append:source-append"
        assert ledger.get_receipt(by_source["source-stop"]["effect_id"])[
            "external_id"] == started["run_id"] + ":interrupt:source-stop"
    finally:
        adapter.release.set()
        await host.close()
        ledger.close()


async def test_unknown_append_keeps_natural_stop_and_later_turn_available(
        loop_host, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from core import session_manager as sm
    import core.turn_coordinator as tc
    from server.cooperative_chat_ingress import CooperativeChatIngress
    from server.control_ledger import ControlLedgerStore

    loop, adapter, controls, _, _ = loop_host
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    sm.create_session("chat-unknown-append")
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", AsyncMock())
    controls.update({"开始":{"op":"send"}, "可能送达":{"op":"send"},
        "先停下":{"op":"interrupt"}, "继续":{"op":"send"}})
    adapter.release.clear()

    async def unknown_append(run_id, text):
        adapter.inputs.append((run_id, text))
        return ProviderInputDelivery("unknown", "transport_receipt_missing")

    monkeypatch.setattr(adapter, "append_input", unknown_append)
    async def typed_cancel(run_id):
        adapter.cancelled.add(run_id)
        adapter.release.set()
        return {"confirmed":True, "cancelled":True,
            "session":ProviderSessionHandle(provider=adapter.provider_id,
                session_id="native-" + run_id, scope="interaction")}

    monkeypatch.setattr(adapter, "cancel", typed_cancel)
    ledger = ControlLedgerStore(tmp_path/"control.sqlite3")
    host = CooperativeChatIngress(loop, session_id="chat-unknown-append",
        ledger=ledger, fence_scope="foreground")
    install_stop_owner(host)

    async def send(text, source):
        result = await host.handler._handle_send({"text":text,
            "session_id":"chat-unknown-append", "turn_id":"turn-" + source,
            "utterance_id":source})
        assert result["status"] == "ok"
        await host.handler._stream_task
        return host.receipts[source]

    try:
        started = await send("开始", "start")
        await asyncio.wait_for(adapter.started.wait(), 2)
        uncertain = await send("可能送达", "append")
        assert uncertain["state"] == "unknown"
        stopped = await send("先停下", "stop")
        assert stopped["state"] == "stopped"
        await loop.wait()
        continued = await send("继续", "continue")
        await loop.wait()
        assert continued["state"] == "started"
        assert continued["run_id"] != started["run_id"]

        with ledger._lock:
            effects = [dict(row) for row in ledger._db.execute(
                "SELECT * FROM control_effect_outbox")]
        by_operation = {}
        for effect in effects:
            payload = json.loads(effect["payload_json"])
            by_operation[(payload["operation"], payload["source_utterance_id"])] = effect
        assert by_operation[("append", "append")]["state"] == "unknown_reconciling"
        assert by_operation[("interrupt", "stop")]["state"] == "terminal"
        assert by_operation[("start", "continue")]["state"] == "terminal"
    finally:
        adapter.release.set()
        await host.close()
        ledger.close()


async def test_pending_append_releases_child_lock_for_natural_stop(
        loop_host, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from core import session_manager as sm
    import core.turn_coordinator as tc
    from server.cooperative_chat_ingress import CooperativeChatIngress
    from server.control_ledger import ControlLedgerStore

    loop, adapter, controls, _, _ = loop_host
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path/"sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm, "_SESSION_SELECTION_REVISION", 0)
    monkeypatch.setattr(sm, "_activation_guard", None)
    monkeypatch.setattr(sm, "conversation_history", sm.ConversationHistory())
    sm.create_session("chat-pending-append")
    monkeypatch.setattr(tc, "coordinator", tc.TurnCoordinator())
    monkeypatch.setattr("server.handlers.chat_handler.bus.emit", AsyncMock())
    controls.update({"开始":{"op":"send"}, "追加":{"op":"send"},
        "先停下":{"op":"interrupt"}})
    adapter.release.clear()
    append_entered, release_append, cancel_seen = (
        asyncio.Event(), asyncio.Event(), asyncio.Event())

    async def pending_append(run_id, text):
        adapter.inputs.append((run_id, text))
        append_entered.set()
        await release_append.wait()
        return ProviderInputDelivery("unknown", "late_unknown")

    original_cancel = adapter.cancel
    async def observed_cancel(run_id):
        outcome = await original_cancel(run_id)
        cancel_seen.set()
        return outcome

    monkeypatch.setattr(adapter, "append_input", pending_append)
    monkeypatch.setattr(adapter, "cancel", observed_cancel)
    ledger = ControlLedgerStore(tmp_path/"control.sqlite3")
    host = CooperativeChatIngress(loop, session_id="chat-pending-append",
        ledger=ledger, fence_scope="foreground")
    install_stop_owner(host)

    async def begin_send(text, source):
        response = await host.handler._handle_send({"text":text,
            "session_id":"chat-pending-append", "turn_id":"turn-" + source,
            "utterance_id":source})
        assert response["status"] == "ok"
        return host.handler._stream_task

    try:
        await begin_send("开始", "start")
        await host.handler._stream_task
        await asyncio.wait_for(adapter.started.wait(), 2)
        append_stream = await begin_send("追加", "append")
        await asyncio.wait_for(append_entered.wait(), 2)
        stop_stream = await begin_send("先停下", "stop")
        await asyncio.wait_for(cancel_seen.wait(), 2)
        assert loop.runtime.get_run(host.receipts["start"]["run_id"]).status == "cancelled"
        assert not loop._inputs["append"][1].done()
        await stop_stream
        release_append.set()
        await loop._inputs["append"][1]
        await asyncio.gather(append_stream, return_exceptions=True)
        await loop.wait()
        with ledger._lock:
            effects = [dict(row) for row in ledger._db.execute(
                "SELECT * FROM control_effect_outbox")]
        append_effect = next(row for row in effects
            if json.loads(row["payload_json"])["operation"] == "append")
        stop_effect = next(row for row in effects
            if json.loads(row["payload_json"])["operation"] == "interrupt")
        assert append_effect["state"] == "unknown_reconciling"
        assert stop_effect["state"] == "terminal"
    finally:
        release_append.set()
        adapter.release.set()
        await host.close()
        ledger.close()


async def test_work_subscriber_and_cooperative_runtime_keep_separate_ownership(loop_host, tmp_path, monkeypatch):
    from agent_host.provider_contract import ProviderRequirements
    from agent_host.provider_types import ProviderRunRequest
    import agent_host.provider_runtime as runtime_module
    from agent_host.work_ledger_store import WorkLedgerStore
    from server.event_bus import EventBus
    import server.work_ledger_coordinator as work_module

    loop, adapter, controls, _, _ = loop_host
    shared_bus = EventBus()
    monkeypatch.setattr(runtime_module, "bus", shared_bus)
    monkeypatch.setattr(work_module, "bus", shared_bus)
    monkeypatch.setattr(work_module.app_settings, "WORK_WORKTREE_ISOLATION", False)
    monkeypatch.setattr(work_module, "cwd_in_project_registry", lambda path: True)
    work_runtime, work_adapter = ProviderRuntime(), Adapter()
    work_runtime.register(work_adapter)
    workspace = tmp_path/"accepted-work"
    workspace.mkdir()
    with WorkLedgerStore(tmp_path/"work.sqlite3") as store:
        coordinator = work_module.WorkLedgerCoordinator(store)
        coordinator.configure()
        work_runtime.set_request_preparer(coordinator.prepare_request)
        try:
            work_run = await work_runtime.start(ProviderRunRequest(
                provider=work_adapter.provider_id, task="Inspect accepted work", cwd=str(workspace),
                requirements=ProviderRequirements(task_kind="general")))
            await work_run.task_handle
            await coordinator.drain_provider_facts()
            accepted = store.get_attempt_by_provider_run(work_run.run_id)
            assert accepted is not None and accepted.execution_status == "succeeded"
            work_ids = [row.work_item_id for row in store.list_work_items()]
            project_ids = [row.project_id for row in store.list_projects()]
            focus = store.get_focus(work_module.DEFAULT_WORK_SURFACE)

            # Same registered provider, independent Runtime/context; no special
            # provider name or no-Work marker tells the subscriber to ignore it.
            controls.update({"开始":{"op":"send"}, "接着检查":{"op":"send"}})
            first = await loop.submit("开始")
            await loop.wait()
            second = await loop.submit("接着检查")
            await loop.wait()
            await coordinator.drain_provider_facts()
            assert first["child_id"] == second["child_id"]
            assert len(adapter.requests) == 2
            assert adapter.requests[1][0].session.session_id == "native-" + first["run_id"]
            assert all(store.get_attempt_by_provider_run(run_id) is None for _, run_id in adapter.requests)
            assert [row.work_item_id for row in store.list_work_items()] == work_ids
            assert [row.project_id for row in store.list_projects()] == project_ids
            assert store.get_focus(work_module.DEFAULT_WORK_SURFACE) == focus
            assert store.get_attempt(accepted.attempt_id).execution_status == "succeeded"
        finally:
            await loop.close()
            await work_runtime.close()
            await coordinator.drain_provider_facts()
            coordinator.close()
