"""Runtime professional Work planning uses shared owners without dispatching itself."""

import asyncio
import json
from types import SimpleNamespace
import threading

import pytest

from core import session_manager as sm
from server import whole_turn_control, work_planner as work_planner_module
from server.control_decision import (
    CONTROL_PAYLOAD_GROUNDING_ATTR,
    CONTROL_REFERENCE_CANDIDATES_ATTR,
)
from server.reference_catalog import (
    TypedReferenceCandidate,
    candidate_catalog_from_coordinator,
)
from server.turn_admission import capture_turn_admission
from server.work_planner import (
    APP_WORK_SCOPE,
    CURRENT_WORK_FACTS_SCOPE,
    ENTRY_DISCOVERY_SCOPE,
    PROVIDER_MESSAGE_SCOPE,
    RuntimeWorkPlanner,
)
from server.work_planner_examples import (
    EXAMPLES,
    NONWORK_PARAGRAPH,
    NONWORK_PARAGRAPH_WITH_GATE,
)
from server.work_planner_prompt import (
    CURRENT_USER_MARKER,
    get_work_planner_prompt,
    project_work_planner_messages,
)
from test_cooperative_pending_turn import pending_host as pending_host
from tools.probes import probe_joint_control_reference, probe_whole_turn_control


def decision(source, provider, intent, *, token=None,
             continuity="current_turn", display_title=""):
    row = {"proposal_index":0, "source_clause":source, "provider":provider,
        "intent":intent, "work_placement":"draft" if intent == "execute" else "not_applicable",
        "session_context":"unchanged",
        "workspace_effect":"write" if intent in {"execute", "amend"} else "none",
        "payload_continuity":continuity,
        "reference_mode":"none" if token is None else "candidates",
        "references":None if token is None else [token]}
    if display_title:
        row["display_title"] = display_title
    if token is not None:
        row["subject"] = "work_item"
    return json.dumps({"decisions":[row]}, ensure_ascii=False)


def message_decision(source, provider, references, *, continuity=None):
    row = {"proposal_index":0, "source_clause":source, "provider":provider,
        "intent":"message", "work_placement":"not_applicable",
        "session_context":"unchanged", "workspace_effect":"none",
        "reference_mode":"none" if references is None else "candidates",
        "references":None if references is None else list(references)}
    if continuity is not None:
        row["payload_continuity"] = continuity
    if references is not None:
        row["subject"] = "work_item"
    return json.dumps({"decisions":[row]}, ensure_ascii=False)


def current_work_from_request(messages):
    system = messages[0]["content"]
    marker = "[今回のcurrent_work Host事実]\n"
    assert system.count(marker) == 1
    return json.loads(system.split(marker, 1)[1].split("\n", 1)[0])


def test_probe_compatibility_imports_are_the_shared_runtime_implementation():
    assert probe_joint_control_reference.build_joint_messages is whole_turn_control.build_joint_messages
    assert probe_joint_control_reference.parse_joint_reply is whole_turn_control.parse_joint_reply
    assert probe_whole_turn_control.build_whole_turn_messages is whole_turn_control.build_whole_turn_messages
    assert probe_whole_turn_control.parse_whole_turn_reply is whole_turn_control.parse_whole_turn_reply
    assert probe_whole_turn_control.whole_turn_owner is whole_turn_control.whole_turn_owner
    messages = ({"role":"system", "content":"semantic contract"},
        {"role":"user", "content":"给 Game 加暂停功能，然后补上快捷键。"})
    candidate = TypedReferenceCandidate("work_item", "work_game", "Game",
        "session_draft", state="open", execution="running", session_current=True)
    request = whole_turn_control.build_whole_turn_messages(messages, (candidate,))
    assert [row["role"] for row in request] == ["system", "user"]
    assert request[-1]["content"].startswith(messages[-1]["content"] + "\n\n")
    assert candidate.token in request[-1]["content"]


def test_professional_prompt_adds_canonical_message_without_changing_default_gate():
    prompt = get_work_planner_prompt(("codex",))
    assert "answers, evaluations, and corrections" in NONWORK_PARAGRAPH
    assert prompt.count(NONWORK_PARAGRAPH_WITH_GATE) == 1
    assert NONWORK_PARAGRAPH not in prompt
    assert PROVIDER_MESSAGE_SCOPE not in prompt
    assert "Workまたは既存Providerとのコミュニケーション" in prompt
    assert "交付を進めない依頼はmessage" in prompt
    assert "work_placement=not_applicable" in prompt
    assert "workspace_effect=none" in prompt
    assert "payload_continuityは判断せず各行から省略" in prompt
    assert "display_title" in prompt
    assert "payload_continuity=current_turn" not in prompt
    assert "confirmed_prior_request" not in prompt
    assert "既存Providerとのコミュニケーションも現在求められていない場合だけ空配列" in prompt
    assert "同じ交付を続ける同意" in prompt
    assert "求める行為はintent=message" in PROVIDER_MESSAGE_SCOPE
    assert "求める行為は空配列" not in PROVIDER_MESSAGE_SCOPE
    assert "有効なmessage結果だけ" in PROVIDER_MESSAGE_SCOPE


async def test_runtime_planner_keeps_context_title_separate_from_source_evidence(
        pending_host):
    context = pending_host
    ingress = await context.manager._ingress_for(context.session_id)
    source = "就按刚才讨论的方案另做一个。"
    display_title = "实现会话恢复方案"
    raw = decision(source, context.manager.provider, "execute",
        display_title=display_title)
    generic = whole_turn_control.parse_whole_turn_reply(raw, source=source,
        candidates=(), provider_ids=(context.manager.provider,))
    assert generic.status == "invalid"

    async def query(_messages):
        return raw

    plan = await RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=context.manager.provider)(ingress, "context-title", {
            "text":source}, SimpleNamespace(utterance_id="context-title"))
    assert plan.status == "ok" and len(plan.operations) == 1
    action = plan.operations[0].action
    assert action["task"] == source
    assert action["_host_display_title"] == display_title
    assert display_title in plan.raw_reply

    invalid_value = json.loads(raw)
    invalid_value["decisions"][0]["display_title"] = {"not":"a label"}
    invalid = whole_turn_control.parse_whole_turn_reply(
        json.dumps(invalid_value, ensure_ascii=False), source=source,
        candidates=(), provider_ids=(context.manager.provider,),
        allow_display_title=True)
    assert invalid.status == "ok"
    assert "_host_display_title" not in invalid.operations[0].action


@pytest.mark.parametrize(
    ("references", "expected_references", "selected_tokens", "reference_queries",
        "continuity"),
    [
        (None, None, (), 0, None),
        ((), (), (), 2, None),
        (("work_item:work-one", "work_item:work-two"),
            ("work_item:work-one", "work_item:work-two"),
            ("work_item:work-one", "work_item:work-two"), 2,
            "confirmed_prior_request"),
    ],
)
async def test_runtime_planner_preserves_canonical_message_reference_states(
        pending_host, monkeypatch, references, expected_references,
        selected_tokens, reference_queries, continuity):
    context = pending_host
    provider = context.manager.provider
    candidates = (
        TypedReferenceCandidate("work_item", "work-one", "First Work",
            "session_draft", session_current=True),
        TypedReferenceCandidate("work_item", "work-two", "Second Work",
            "session_draft"),
    )
    monkeypatch.setattr("server.control_adjudication.candidate_catalog_from_coordinator",
        lambda *_args, **_kwargs:(candidates, True, ""))
    ingress = await context.manager._ingress_for(context.session_id)
    source = "Ask the existing provider to explain the implementation."
    planning_requests, candidate_requests = [], []

    async def query(messages):
        if "[Independent candidate verdict - FINAL]" in messages[0]["content"]:
            candidate = next(row for row in candidates
                if row.token in messages[-1]["content"])
            candidate_requests.append(candidate.token)
            return json.dumps({"evidence":
                "exact" if candidate.token in selected_tokens else "none"})
        planning_requests.append(messages)
        return message_decision(source, provider, references,
            continuity=continuity)

    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=provider)
    plan = await planner(ingress, "canonical-message-refs", {
        "text":source, "provider_message_action":{"op":"send"}},
        SimpleNamespace(utterance_id="canonical-message-refs"))
    assert plan.status == "ok" and len(plan.operations) == 1
    action = plan.operations[0].action
    assert action["intent"] == "message" and action["provider"] == provider
    assert action["task"] == source
    assert action["_host_payload_source"] == "exact_current_user_clause"
    assert CONTROL_PAYLOAD_GROUNDING_ATTR not in action
    assert action["_host_workspace_access"] == "none"
    actual = action[CONTROL_REFERENCE_CANDIDATES_ATTR]
    assert (None if actual is None else tuple(row.token for row in actual)) == (
        expected_references)
    assert len(planning_requests) == 1
    assert len(candidate_requests) == reference_queries
    if continuity is not None:
        assert continuity in plan.raw_reply
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []


async def test_runtime_planner_keeps_required_condition_amendment_and_zero_request(
        pending_host, monkeypatch):
    context = pending_host
    provider = context.manager.provider
    candidate = TypedReferenceCandidate("work_item", "work-required-condition",
        "Accepted delivery", "session_draft", session_current=True,
        state="open", execution="succeeded", relation="needs_attention")
    monkeypatch.setattr("server.control_adjudication.candidate_catalog_from_coordinator",
        lambda *_args, **_kwargs:((candidate,), True, ""))
    ingress = await context.manager._ingress_for(context.session_id)
    ingress.loop.recipient_work = lambda _context_id:{
        "work_item_id":candidate.entity_id,
        "goal":"Create the accepted deliverable after its required choice is known.",
        "provider":provider,
        "execution_status":"succeeded",
        "work_state":"open",
        "completeness":"partial",
        "attention":"input",
    }
    condition = "Use the smaller supported option."
    requests = []

    async def amend_query(messages):
        requests.append(messages)
        if "[Independent candidate verdict - FINAL]" in messages[0]["content"]:
            return '{"evidence":"contextual"}'
        return decision(condition, provider, "amend", token=candidate.token)

    amend = await RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=amend_query, provider=provider)(ingress, "required-condition",
        {"text":condition}, SimpleNamespace(utterance_id="required-condition"))
    assert amend.status == "ok" and len(amend.operations) == 1
    amend_action = amend.operations[0].action
    assert amend_action["intent"] == "amend"
    assert amend_action["_host_workspace_access"] == "write"
    assert amend_action[CONTROL_REFERENCE_CANDIDATES_ATTR] == (candidate,)
    assert amend_action["task"] == condition
    assert len(requests) == 2

    zero_requests = []

    async def zero_query(messages):
        zero_requests.append(messages)
        return '{"decisions":[]}'

    zero = await RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=zero_query, provider=provider)(ingress, "ordinary-zero",
        {"text":"A plain conversational reaction."},
        SimpleNamespace(utterance_id="ordinary-zero"))
    assert zero.status == "ok" and zero.operations == () and zero.clauses == ()
    assert len(zero_requests) == 1
    assert context.host.adapter.calls == 0
    assert context.host.work.list_work_items() == []


@pytest.mark.parametrize("source", ["一百块左右吧。", "就按刚才说的继续吧。"])
async def test_current_source_policy_keeps_existing_work_payload_for_short_amendments(
        pending_host, source):
    context = pending_host
    provider = context.manager.provider
    item = context.host.work.create_work_item(
        context.host.project.project_id,
        title="Accepted page",
        goal="Create the already accepted page.",
        workspace_path=context.host.workspace,
    )
    _operation, attempt = context.host.work.create_operation_attempt(
        item.work_item_id,
        intent="execute",
        instruction=item.goal,
        provider=provider,
        task=item.goal,
        provider_run_id="run-existing-short-amendment",
        attempt_metadata={"session_id":context.session_id},
    )
    context.host.work.update_attempt(attempt.attempt_id,
        execution_status="succeeded")
    candidates, complete, reason = candidate_catalog_from_coordinator(
        context.host.coordinator, context.session_id)
    assert complete, reason
    candidate = next(row for row in candidates
        if row.entity_id == item.work_item_id)
    ingress = await context.manager._ingress_for(context.session_id)

    async def query(messages):
        if "[Independent candidate verdict - FINAL]" in messages[0]["content"]:
            return json.dumps({"evidence":
                "contextual" if candidate.token in messages[-1]["content"] else "none"})
        return decision(source, provider, "amend", token=candidate.token,
            continuity="confirmed_prior_request")

    plan = await RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=provider)(ingress, "short-amendment", {"text":source},
        SimpleNamespace(utterance_id="short-amendment"))
    assert plan.status == "ok" and len(plan.operations) == 1
    assert "confirmed_prior_request" in plan.raw_reply
    assert CONTROL_PAYLOAD_GROUNDING_ATTR not in plan.operations[0].action

    current_raw = decision(source, provider, "amend", token=candidate.token)
    current_plan = whole_turn_control.parse_whole_turn_reply(
        current_raw, source=source, candidates=candidates,
        provider_ids=(provider,), proposals=({"task":source},),
        proposal_controls=({"provider":provider, "task":source},),
        payload_policy="current_source")
    assert current_plan.status == "ok"
    admission = capture_turn_admission(
        utterance_id="short-amendment",
        turn_id="short-amendment",
        session_id=context.session_id,
        transcript=source,
        input_source="text",
        chat_epoch=1,
        pending=False,
    )
    assert admission is not None
    receipt = {
        "text":source,
        "source_user_text":source,
        "child_id":ingress.loop.bound_context_id,
        "source_binding_token":ingress.loop._binding.token,
        "context_revision":-1,
        "input_id":admission.utterance_id,
    }
    compiled = [context.manager._compile_planned_work_plan(
        ingress, admission.turn_id, receipt, admission, candidate_plan, {})
        for candidate_plan in (plan, current_plan)]
    assert compiled[0]["status"] == compiled[1]["status"] == "ready"
    assert compiled[0]["payloads"] == compiled[1]["payloads"]
    payload = compiled[0]["payloads"][0]
    assert payload.work_item_id == item.work_item_id
    assert payload.provider == provider
    assert payload.task == payload.source_user_text == source
    assert payload.requirements.workspace_access == "write"
    assert context.host.work.get_work_item(item.work_item_id).goal == item.goal


async def test_current_source_policy_allows_nondefault_provider_after_model_continuity(
        pending_host, monkeypatch):
    context = pending_host
    default_provider = context.manager.provider
    other_provider = "other-work-provider"
    ingress = await context.manager._ingress_for(context.session_id)
    ingress.loop.context_requirements[other_provider] = (
        ingress.loop.context_requirements[default_provider])
    candidate = TypedReferenceCandidate("work_item", "work-other-provider",
        "Existing Work", "session_draft", session_current=True)
    monkeypatch.setattr("server.control_adjudication.candidate_catalog_from_coordinator",
        lambda *_args, **_kwargs:((candidate,), True, ""))
    source = "Continue the existing Work."

    async def query(messages):
        if "[Independent candidate verdict - FINAL]" in messages[0]["content"]:
            return '{"evidence":"contextual"}'
        return decision(source, other_provider, "amend", token=candidate.token,
            continuity="confirmed_prior_request")

    plan = await RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=default_provider)(ingress, "other-provider", {
            "text":source}, SimpleNamespace(utterance_id="other-provider"))
    assert plan.status == "ok" and len(plan.operations) == 1
    action = plan.operations[0].action
    assert action["provider"] == other_provider
    assert action["task"] == source
    assert action["_host_payload_source"] == "exact_current_user_clause"
    assert CONTROL_PAYLOAD_GROUNDING_ATTR not in action
    assert "confirmed_prior_request" in plan.raw_reply


async def test_runtime_planner_excludes_current_early_role_and_bounds_auip_facts(
        pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda:(context.manager.provider,))
    ingress = await context.manager._ingress_for(context.session_id)
    for role, content, turn in (("user", "Prior user", "prior"),
            ("assistant", "Prior role", "prior"),
            ("user", "Current request", "current-turn"),
            ("assistant", "EARLY CURRENT ROLE", "current-turn")):
        assert sm.append_session_message(context.session_id, role=role, content=content, turn_id=turn)
    requests = []

    async def query(messages):
        requests.append(messages)
        return '{"decisions":[]}'

    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator, query=query,
        provider=context.manager.provider)
    receipt = {"text":"Current request", "provider_message_action":{"op":"send"},
        "auip_context":{
        "action":"step", "timing":"now", "instruction":"current app step",
        "app_session_id":"app-current", "work_relation":"independent"}}
    plan = await planner(ingress, "current-turn", receipt,
        SimpleNamespace(utterance_id="current-input"))
    assert plan.status == "ok" and plan.operations == ()
    assert len(requests) == 1
    rendered = json.dumps(requests[0], ensure_ascii=False)
    assert "Prior user" in rendered and "Prior role" in rendered
    assert "EARLY CURRENT ROLE" not in rendered
    assert "work_relation" not in rendered
    assert requests[0][0]["content"].count(APP_WORK_SCOPE) == 1
    assert requests[0][0]["content"].count(PROVIDER_MESSAGE_SCOPE) == 1
    assert APP_WORK_SCOPE not in ingress.loop.system
    assert all(value in rendered for value in (
        "step", "now", "current app step", "app-current"))
    assert NONWORK_PARAGRAPH_WITH_GATE in requests[0][0]["content"]
    assert "[ControlDecision output contract - FINAL]" not in requests[0][0]["content"]
    assert "[Joint control reference - FINAL]" not in requests[0][0]["content"]
    assert any("[Host control frame]" in row["content"] for row in requests[0])
    assert requests[0][-1]["content"] == CURRENT_USER_MARKER + "\nCurrent request"
    assert sum(message["role"] == "assistant" for message in requests[0]) >= len(EXAMPLES)


async def test_missing_entry_projects_lookup_facts_without_claiming_app_execution(
        pending_host, monkeypatch):
    context = pending_host
    provider = context.manager.provider
    monkeypatch.setattr("llm.prompts.registered_provider_ids", lambda:(provider,))
    ingress = await context.manager._ingress_for(context.session_id)
    source = "把上次那个计时器打开看看。"
    requests = []

    async def query(messages):
        requests.append(messages)
        row = json.loads(decision(source, provider, "execute"))["decisions"][0]
        row.pop("payload_continuity")
        row["workspace_effect"] = "none"
        return json.dumps({"decisions":[row]}, ensure_ascii=False)

    plan = await RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=provider)(ingress, "lookup-entry", {
            "text":source, "auip_context":{
                "action":"engage", "timing":"now", "app_session_id":"",
                "target":"上次那个计时器", "project_ref":"",
                "reason":"entry_target_not_found"}},
            SimpleNamespace(utterance_id="lookup-entry"))

    assert plan.status == "ok" and len(plan.operations) == 1
    action = plan.operations[0].action
    assert action["intent"] == "execute" and action["one_off"] is True
    assert action["task"] == source and action["_host_workspace_access"] == "none"
    assert action[CONTROL_REFERENCE_CANDIDATES_ATTR] is None
    assert len(requests) == 1
    system = requests[0][0]["content"]
    assert ENTRY_DISCOVERY_SCOPE in system and APP_WORK_SCOPE not in system
    assert json.dumps({"target":"上次那个计时器", "project_ref":"",
        "reason":"entry_target_not_found"}, ensure_ascii=False) in system
    assert '"action": "engage"' not in system


async def test_runtime_planner_projects_exact_current_recipient_work_facts(
        pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda:(context.manager.provider,))
    ingress = await context.manager._ingress_for(context.session_id)
    current = {
        "work_item_id":"work-current",
        "goal":"Build the accepted page",
        "provider":"original-provider",
        "execution_status":"succeeded",
        "work_state":"open",
        "completeness":"partial",
        "attention":"input",
        "input_requirements":[{
            "input_id":"input-required",
            "text":"Choose the required format",
            "delivery_state":"unknown",
        }],
        "workspace":"private-path",
        "title":"not part of the professional projection",
    }
    recipient_reads = []

    def recipient_work(context_id):
        recipient_reads.append(context_id)
        return current if context_id == "" else None

    ingress.loop.recipient_work = recipient_work
    requests = []

    async def query(messages):
        requests.append(messages)
        return '{"decisions":[]}'

    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator, query=query,
        provider=context.manager.provider)
    plan = await planner(ingress, "current-work-facts", {"text":"Continue."},
        SimpleNamespace(utterance_id="current-work-facts"))
    assert plan.status == "ok" and not plan.operations and len(requests) == 1
    assert recipient_reads == [ingress.loop.bound_context_id] == [""]
    facts = current_work_from_request(requests[0])
    assert facts == {
        "work_item_id":"work-current",
        "goal":"Build the accepted page",
        "provider":"original-provider",
        "execution_status":"succeeded",
        "work_state":"open",
        "completeness":"partial",
        "attention":"input",
        "input_requirements":[{
            "input_id":"input-required",
            "text":"Choose the required format",
            "delivery_state":"unknown",
        }],
    }
    system = requests[0][0]["content"]
    assert CURRENT_WORK_FACTS_SCOPE in system
    assert "private-path" not in system
    assert NONWORK_PARAGRAPH_WITH_GATE in system


async def test_runtime_planner_freezes_current_work_before_context_capture_await(
        pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda:(context.manager.provider,))
    ingress = await context.manager._ingress_for(context.session_id)
    current = {
        "work_item_id":"work-before",
        "goal":"Original accepted goal",
        "provider":"provider-before",
        "execution_status":"running",
        "work_state":"open",
        "completeness":"unknown",
        "attention":"unknown",
        "input_requirements":[{"text":"original requirement"}],
    }
    ingress.loop.recipient_work = lambda _context_id: current
    entered, release = threading.Event(), threading.Event()
    original_capture = work_planner_module.RuntimeControlDecisionResolver.capture_context

    def held_capture(self, batch, **kwargs):
        entered.set()
        assert release.wait(3)
        return original_capture(self, batch, **kwargs)

    monkeypatch.setattr(work_planner_module.RuntimeControlDecisionResolver,
        "capture_context", held_capture)
    requests = []

    async def query(messages):
        requests.append(messages)
        return '{"decisions":[]}'

    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator, query=query,
        provider=context.manager.provider)
    pending = asyncio.create_task(planner(ingress, "frozen-current-work",
        {"text":"Use the captured facts."},
        SimpleNamespace(utterance_id="frozen-current-work")))
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
    current.update({
        "work_item_id":"work-after",
        "goal":"Later goal",
        "provider":"provider-after",
        "execution_status":"succeeded",
        "attention":"review",
    })
    current["input_requirements"][0]["text"] = "later requirement"
    release.set()
    plan = await asyncio.wait_for(pending, 4)
    assert plan.status == "ok" and not plan.operations and len(requests) == 1
    facts = current_work_from_request(requests[0])
    assert facts["work_item_id"] == "work-before"
    assert facts["goal"] == "Original accepted goal"
    assert facts["provider"] == "provider-before"
    assert facts["execution_status"] == "running"
    assert facts["attention"] == "unknown"
    assert facts["input_requirements"] == [{"text":"original requirement"}]
    assert "work-after" not in json.dumps(requests[0], ensure_ascii=False)
    assert "later requirement" not in json.dumps(requests[0], ensure_ascii=False)


@pytest.mark.parametrize("recipient", ["missing", "none"])
async def test_runtime_planner_does_not_invent_current_work_without_host_fact(
        pending_host, monkeypatch, recipient):
    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda:(context.manager.provider,))
    ingress = await context.manager._ingress_for(context.session_id)
    if recipient == "missing":
        monkeypatch.delattr(ingress.loop, "recipient_work")
    else:
        ingress.loop.recipient_work = lambda _context_id: None
    requests = []

    async def query(messages):
        requests.append(messages)
        return '{"decisions":[]}'

    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator, query=query,
        provider=context.manager.provider)
    plan = await planner(ingress, "no-current-work", {"text":"Ordinary request."},
        SimpleNamespace(utterance_id="no-current-work"))
    assert plan.status == "ok" and not plan.operations and len(requests) == 1
    system = requests[0][0]["content"]
    assert "[今回のcurrent_work Host事実]" not in system
    assert CURRENT_WORK_FACTS_SCOPE not in system


async def test_planner_advertises_only_executable_providers_and_accepts_explicit_choice(
        pending_host, monkeypatch):
    context = pending_host
    provider = context.manager.provider
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda:(provider, "other-registered-provider"))
    text = f"让 {provider} 查一下论文。"
    requests = []

    async def query(messages):
        requests.append(messages)
        assert f"Currently registered provider ids: {provider}." in messages[0]["content"]
        assert "other-registered-provider" not in messages[0]["content"]
        result = json.loads(decision(text, provider, "execute"))
        result["decisions"][0].update(force_provider="user", workspace_effect="none")
        return json.dumps(result)

    context.manager.work_planner = RuntimeWorkPlanner(
        coordinator=context.host.coordinator, query=query, provider=provider)
    async def coarse(messages, **_kwargs):
        return (json.dumps({"action":{"op":"work"}, "say":"調べるわ。"})
            if json.loads(messages[-1]["content"])["source_kind"] == "user" else "確認したわ。")
    context.manager.query = coarse
    await context.handler.send_text(text, session_id=context.session_id, turn_id="provider-scope")
    await asyncio.wait_for(context.handler._stream_task, 4)
    receipt = context.manager.ingresses[context.session_id].receipts["provider-scope"]
    assert receipt["state"] == "work_started"
    await context.finish()
    assert len(requests) == 1
    request = context.host.adapter.requests[0]["request"]
    assert request.provider == provider and request.requirements.workspace_access == "none"


async def test_context_capture_runs_off_loop_while_coarse_role_publishes(
        pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda:(context.manager.provider,))
    entered, release = threading.Event(), threading.Event()
    original = __import__("server.control_adjudication", fromlist=[
        "RuntimeControlDecisionResolver"]).RuntimeControlDecisionResolver.capture_context

    def held_capture(self, batch, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(self, batch, **kwargs)

    monkeypatch.setattr("server.control_adjudication.RuntimeControlDecisionResolver.capture_context",
        held_capture)
    text = "Build after captured context."

    async def planner_query(_messages):
        return decision(text, context.manager.provider, "execute")

    context.manager.work_planner = RuntimeWorkPlanner(
        coordinator=context.host.coordinator, query=planner_query,
        provider=context.manager.provider)

    async def coarse(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        return (json.dumps({"action":{"op":"work"}, "say":"Starting the plan."})
            if frame["source_kind"] == "user" else "Confirmed.")

    context.manager.query = coarse
    await context.handler.send_text(text, session_id=context.session_id,
        turn_id="held-capture")
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
    assert [row["text"] for row in context.publications] == ["Starting the plan."]
    assert context.host.adapter.calls == 0
    release.set()
    await asyncio.wait_for(context.handler._stream_task, 4)
    assert context.manager.ingresses[context.session_id].receipts[
        "held-capture"]["state"] == "work_started"
    context.host.adapter.release.set()
    await context.finish()


async def test_specialist_captures_existing_app_semantics_without_an_auip_proposal(
        pending_host, monkeypatch):
    from server.auip_runtime import AuipRuntime
    from server.work_context import augment_system_prompt_for_control_decision
    from test_auip_runtime import _manifest

    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda: (context.manager.provider,))
    runtime = AuipRuntime()
    app = runtime.register(manifest=_manifest(), conversation_id=context.session_id)
    runtime.publish_state(app_session_id=app["app_session_id"],
        bridge_token=app["bridge_token"], revision=1, state={"private_marker": "hidden"})
    monkeypatch.setattr("server.auip_runtime.runtime", runtime)
    default = augment_system_prompt_for_control_decision("default semantics",
        session_id=context.session_id)
    assert "game.place_stone" not in default
    ingress = await context.manager._ingress_for(context.session_id)
    requests = []

    async def query(messages):
        requests.append(messages)
        return '{"decisions":[]}'

    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=context.manager.provider)
    plan = await planner(ingress, "app-facts", {"text": "刚才那步再看一下。"},
        SimpleNamespace(utterance_id="app-facts"))
    assert plan.status == "ok" and not plan.operations
    assert len(requests) == 1
    system = requests[0][0]["content"]
    assert "game.place_stone" in system and "Place one stone." in system
    assert "projection_revision=1" in system and app["app_session_id"] in system
    assert "private_marker" not in system
    assert augment_system_prompt_for_control_decision("default semantics",
        session_id=context.session_id) == default


async def test_specialist_keeps_captured_host_facts_and_shared_protocol_repair(
        pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda: (context.manager.provider,))
    monkeypatch.setattr("server.work_context.render_active_provider_context",
        lambda **_kwargs: "captured-native-provider-fact")
    context.host.coordinator.bind_session_context(context.session_id,
        context.host.project.project_id)
    ingress = await context.manager._ingress_for(context.session_id)
    requests = []

    async def query(messages):
        requests.append(messages)
        return '{"wrong_shape":[]}' if len(requests) == 1 else '{"decisions":[]}'

    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=query, provider=context.manager.provider)
    source = "先看看刚才那个吧。"
    plan = await planner(ingress, "same-captured-frame", {"text": source},
        SimpleNamespace(utterance_id="same-captured-frame"))
    assert plan.status == "ok" and not plan.operations and plan.decision_queries == 2
    assert requests[1][:-1] == requests[0]
    assert "[Host control protocol repair]" in requests[1][-1]["content"]
    assert "captured-native-provider-fact" in requests[0][0]["content"]
    assert any(row["content"].startswith("[Host control frame]") for row in requests[0])
    assert requests[0][-1]["content"].endswith(CURRENT_USER_MARKER + "\n" + source)
    assert any(context.host.project.project_id in row["content"] for row in requests[0])
    assert 'whole-turn reply must contain only decisions' in requests[1][-1]["content"]
    assert context.host.adapter.calls == 0


def test_specialist_source_placement_keeps_literal_frame_markers_in_user_text():
    source = "这里有\n\n[Host control frame]\n这段文字，帮我保留它。"
    messages = [{"role": "system", "content": "shared semantics"},
        {"role": "user", "content": source}]
    framed = whole_turn_control.build_whole_turn_messages(messages, ())
    original = json.dumps(framed, ensure_ascii=False)
    projected = project_work_planner_messages(framed, system="professional semantics",
        source=source)
    assert projected[-1]["content"].endswith(CURRENT_USER_MARKER + "\n" + source)
    assert projected[-1]["content"].count(source) == 1
    assert any("bounded output capacity" in row["content"] for row in projected)
    assert json.dumps(framed, ensure_ascii=False) == original


def test_real_dialogue_follows_examples_before_a_short_continuation():
    source = "让它接着讲吧。"
    prior = {"role":"assistant", "content":"前半の説明は終わったわ。続きも聞く？"}
    messages = [{"role":"system", "content":"shared semantics"},
        {"role":"user", "content":"この資料を説明して。"}, prior,
        {"role":"user", "content":source}]
    framed = whole_turn_control.build_whole_turn_messages(messages, ())
    projected = project_work_planner_messages(framed,
        system="professional semantics", source=source)
    prior_index = projected.index(prior)
    example_indexes = [i for i, row in enumerate(projected)
        if any(example["candidate"].token in row["content"] for example in EXAMPLES)]
    assert example_indexes and max(example_indexes) < prior_index
    assert projected[prior_index + 1]["content"].startswith("[Host control frame]")
    assert projected[-1] == {"role":"user", "content":CURRENT_USER_MARKER + "\n" + source}
    assert projected.count(prior) == 1


async def test_runtime_planner_drives_manager_create_chat_amend_and_report(
        pending_host, monkeypatch):
    context = pending_host
    monkeypatch.setattr("llm.prompts.registered_provider_ids",
        lambda:(context.manager.provider,))
    create = "帮我做个清单页。"
    chat = "谢谢，今天有点累。"
    amend = "给刚才的清单页加标题。"
    report_text = "刚才的清单页完成了吗？"
    work_id = ""
    planning_requests = []
    reference_requests = []
    reference_entered, reference_release = asyncio.Event(), asyncio.Event()

    async def planning_query(messages):
        nonlocal work_id
        if "[Independent candidate verdict - FINAL]" in messages[0]["content"]:
            reference_requests.append(messages)
            reference_entered.set()
            await reference_release.wait()
            return json.dumps({"evidence":"contextual"
                if "work_item:" + work_id in messages[-1]["content"] else "none"})
        planning_requests.append(messages)
        source = messages[-1]["content"].split(CURRENT_USER_MARKER + "\n", 1)[1]
        assert f"coarse::{source}" not in json.dumps(messages, ensure_ascii=False)
        if source == create:
            return decision(source, context.manager.provider, "execute")
        assert work_id
        return decision(source, context.manager.provider,
            "amend" if source == amend else "report", token="work_item:" + work_id)

    planner = RuntimeWorkPlanner(coordinator=context.host.coordinator,
        query=planning_query, provider=context.manager.provider)
    context.manager.work_planner = planner

    async def coarse(messages, **_kwargs):
        frame = json.loads(messages[-1]["content"])
        if frame["source_kind"] != "user":
            return "Confirmed."
        source = frame["current"]["text"]
        return json.dumps({"action":None if source == chat else {"op":"work"},
            "say":f"coarse::{source}"}, ensure_ascii=False)

    report_calls = []

    async def report(source, attrs, *, publish=None):
        report_calls.append((source, dict(attrs)))
        await publish("清单页已经完成。")
        return "canonical"

    context.manager.query = coarse
    context.manager.configure_work(context.host.control, context.host.executor,
        report_request=report)

    async def send(source, turn_id):
        await context.handler.send_text(source, session_id=context.session_id,
            turn_id=turn_id)
        await asyncio.wait_for(context.handler._stream_task, 4)
        return context.manager.ingresses[context.session_id].receipts[turn_id]

    created = await send(create, "runtime-create")
    assert created["state"] == "work_started"
    work_id = created["work_item_id"]
    await context.finish()
    ordinary = await send(chat, "runtime-chat")
    assert ordinary["state"] == "no_action" and len(planning_requests) == 1
    assert not reference_requests
    amendment = asyncio.create_task(send(amend, "runtime-amend"))
    try:
        await asyncio.wait_for(reference_entered.wait(), 2)

        async def wait_for_early_role():
            while not any(row.get("cause") == "runtime-amend"
                    and row.get("source") == "kurisu" and f"coarse::{amend}" in row.get("text", "")
                    for row in context.manager.ingresses[context.session_id].loop.history):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_early_role(), 2)
        assert context.host.adapter.calls == 1
    finally:
        reference_release.set()
    amended = await amendment
    assert amended["state"] == "work_started" and amended["work_item_id"] == work_id
    await context.finish()
    reported = await send(report_text, "runtime-report")
    assert reported["state"] == "work_reported"
    assert reported["report_work_item_id"] == work_id
    assert len(planning_requests) == 3 and len(report_calls) == 1
    assert reference_requests
    assert context.host.adapter.calls == 2
    assert len(context.host.work.list_work_items()) == 1
    assert len(context.host.work.list_attempts(work_id)) == 2
