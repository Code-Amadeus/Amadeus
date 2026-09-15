"""An opaque output identifier cannot establish an exact Project reference."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from server.control_decision import (
    _candidate_has_exact_handle,
    reconcile_control_decision,
    resolve_control_decision,
)
from server.reference_catalog import TypedReferenceCandidate


PROJECT = TypedReferenceCandidate("project", "project_current", "amadeus", "persistent")
ACTIVE = TypedReferenceCandidate(
    "work_item", "work_active", "OpenClaw web research", "session_draft", execution="running",
)


def test_exact_handle_never_comes_from_inside_an_output_identifier():
    for marker in (
        "OPENCLAW_STEER_AMADEUS_E2E_TOKEN", "prefixAmadeusSuffix",
        "output-amadeus-token", "output.amadeus.token", "amadeus2", "2amadeus",
        "project_current_suffix", "PREFIX_PROJECT_CURRENT",
        "ＯＵＴＰＵＴ＿ＡＭＡＤＥＵＳ＿ＴＯＫＥＮ",
    ):
        assert not _candidate_has_exact_handle(f"把结果写成 `{marker}`。", PROJECT), marker


def test_complete_names_keep_normalization_and_natural_language_boundaries():
    for text in (
        "回到 amadeus 项目。", "回到amadeus项目。", "Switch to Amadeus.",
        "切换到`AMADEUS`。", "打开ＡＭＡＤＥＵＳ。", "使用 project_current。",
    ):
        assert _candidate_has_exact_handle(text, PROJECT), text
    for label, text in (
        ("国际象棋游戏", "切回那个国际象棋游戏。"),
        ("Game Lab", "switch to Game Lab!"),
        ("route-note.txt", "修改 src/route-note.txt。"),
        ("my-project", "切回my-project项目。"),
    ):
        candidate = TypedReferenceCandidate("project", "project_x", label, "persistent")
        assert _candidate_has_exact_handle(text, candidate), text


def test_output_marker_cannot_upgrade_none_or_activate_a_project_scope_fence():
    async def run(marker, subject, proposed_project):
        text = "继续刚才那个 OpenClaw 任务，点开 Detail 链接，把结果告诉我。"
        if marker:
            text += "结果加 `OPENCLAW_STEER_AMADEUS_E2E_TOKEN`。"
        raw = {"provider": "openclaw", "intent": "amend"}
        if proposed_project:
            raw["project_id"] = PROJECT.entity_id

        async def query(messages):
            joined = "\n".join(message["content"] for message in messages)
            if "[Independent candidate verdict - FINAL]" in joined:
                return '{"evidence":"none"}' if PROJECT.token in joined else '{"evidence":"contextual"}'
            return json.dumps({"decisions": [{
                "proposal_index": 0, "provider": "openclaw", "intent": "amend",
                "subject": subject, "work_placement": "not_applicable",
                "session_context": "unchanged", "reference_mode": "candidates",
            }]})

        decision = await resolve_control_decision(
            ({"role": "system", "content": "control"}, {"role": "user", "content": text}),
            ({"task": "continue"},), (ACTIVE, PROJECT), complete=True,
            query=query, proposal_controls=(raw,),
        )
        actions, _ = reconcile_control_decision(
            ({"task": "continue"},), decision, provider_ids={"openclaw"},
            proposal_controls=(raw,), source_user_text=text,
        )
        assert len(actions) == 1
        assert actions[0]["workspace_ref"] == ACTIVE.entity_id
        assert actions[0]["subject"] == "work_item"

    for marker, subject, proposed_project in (
        (False, "open", False), (True, "open", False),
        (True, "work_item", False), (True, "work_item", True),
    ):
        asyncio.run(run(marker, subject, proposed_project))


def test_structured_query_diagnostic_observer_preserves_the_native_boundary():
    from llm import client

    native_content = ["not", "a string"]
    response = SimpleNamespace(
        id="response-test", model="model-test",
        choices=[SimpleNamespace(message=SimpleNamespace(content=native_content), finish_reason="length")],
    )
    seen = []
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: response)))
    with patch.object(client, "LLM_PROVIDER", "deepseek"), patch.object(client, "llm_client", fake_client):
        reply = client.remote_llm_messages_query(
            [{"role": "system", "content": "diagnostic"}, {"role": "user", "content": "test"}],
            response_observer=seen.append,
        )
    assert reply == str(native_content)  # Observation must not change transport behavior.
    assert seen[0]["content"] is native_content
    assert seen[0]["content_type"] == "list"
    assert seen[0]["finish_reason"] == "length"


def test_compound_reply_reaches_explicit_sink_but_not_routine_logs():
    from server.compound_control import CompoundControlPlan
    from server.control_adjudication import ControlDecisionAdjudicator

    async def run():
        raw = "[NOT_JSON_PRIVATE_REPLY]"
        sink = []
        adjudicator = ControlDecisionAdjudicator(query=AsyncMock(), compound_sink=sink.append)
        batch = SimpleNamespace(
            turn_id="turn", session_id="session", proposals=(), decision_payloads=lambda: (),
        )
        context = SimpleNamespace(
            messages=(), candidates=(), catalog_complete=True,
            provider_ids=frozenset(), exhaustive_candidate_limit=64,
        )
        with patch("server.control_adjudication.resolve_compound_control_plan", AsyncMock(
            return_value=CompoundControlPlan(status="invalid", raw_reply=raw),
        )):
            evidence = await adjudicator.observe_compound(batch, context)
        assert sink == [evidence]
        assert evidence.decomposition_reply == raw
        record = evidence.as_log_record()
        assert record["decompositionReplyChars"] == len(raw)
        assert len(record["decompositionReplySha256"]) == 64
        assert raw not in json.dumps(record)

    asyncio.run(run())
