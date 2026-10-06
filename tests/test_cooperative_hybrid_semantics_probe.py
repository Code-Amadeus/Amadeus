"""Instrument contracts, not real-model wording or acceptance scores."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.probes import probe_cooperative_hybrid_semantics as probe


def fixture_row(case):
    fixture = probe.load_fixture()
    return fixture, probe.select_cases(fixture, [case])[0]


def forbidden_query(*_args, **_kwargs):
    raise AssertionError("seed construction must not query a model")


def test_reviewed_fixture_preserves_sources_denominators_and_historical_failures():
    fixture = probe.load_fixture()
    original = json.loads((probe.ROOT / fixture["historical_source"]["path"]).read_text(encoding="utf-8"))
    rows = probe.select_cases(fixture)
    assert len(rows) == 47
    assert sum(row["scored"] for row in rows if row["section"] == "routing") == 26
    assert sum(row["section"] == "new_persona" for row in rows) == 2
    for arm in original["arms"]:
        for old in arm["cases"]:
            current = next(row for row in rows if row["case"] == old["case"])
            assert (current["source"], current["expected"], current["scored"]) == (old["source"], old["expected"], old["scored"])
            historical = next(value for value in current["historical_results"] if value["arm"] == arm["name"])
            assert historical["observed"] == old["observed"] and historical["match"] == old["match"]
    multi = next(row for row in rows if row["case"] == "b_multi_msg_amend")
    assert multi["seed"]["history"][1]["content"] == "Codexが現在、この清单ページを実装しているわ。"
    assert fixture["profiles"]["memo_terminal"]["history"][1]["content"] == "便箋ページの実行は終了しているわ。"


def test_default_dry_run_publishes_every_seed_without_model_import_or_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "experiment", forbidden_query)
    target = tmp_path / "prepared.json"
    assert probe.main(["--output", str(target)]) == 0
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["fixture_sha256"] == probe.FROZEN_SHA256
    assert len(report["cases"]) == 47
    assert report["call_budget"]["user_turn_attempts"] == 188
    assert report["call_budget"]["head_attempts_upper_bound"] == 94
    assert report["acceptance"]["persona"].startswith("pending")
    assert "results" not in report


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_frozen_snapshot_survives_git_line_ending_conversion(tmp_path, newline):
    copied = tmp_path / "fixture.json"
    copied.write_bytes(probe.FIXTURE.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", newline))
    assert probe.load_fixture(copied) == probe.load_fixture()


def test_live_requires_exact_reviewed_manifest_hash(tmp_path):
    with pytest.raises(SystemExit) as error:
        probe.main(["--live", "--output", str(tmp_path / "report.json")])
    assert error.value.code == 2


@pytest.mark.parametrize("case", ["active_amend", "terminal_amend", "b_two_recent", "b_two_named"])
async def test_current_host_seeds_have_equal_identity_context_history_across_four_arms(tmp_path, case):
    fixture, row = fixture_row(case)
    projections = []
    for arm in fixture["arms"]:
        observer = probe.QueryObserver(forbidden_query)
        async with probe.CaseHost(tmp_path, row, arm, observer) as host:
            projections.append(deepcopy(host.seed_projection))
            assert observer.calls == []
            assert len(host.seed_snapshot["work_items"]) == len(row["seed"]["tasks"])
            assert all(event["phase"] == "seed" for event in host.adapter.events)
            for task in row["seed"]["tasks"]:
                binding = host.seed_bindings[task["key"]]
                attempt = host.work.get_attempt(binding["attempt_id"])
                assert attempt.origin_effect_id == binding["effect_id"]
                assert attempt.execution_status == task["status"]
                assert host.control.binding(binding["effect_id"])["provider_run_id"] == binding["provider_run_id"]
            assert list(host.workspace.iterdir()) == []
            if case == "b_two_recent":
                assert not host.ingress.loop.bound_context_id
                assert host.seed_projection["current_work"] is None
                assert len({item["workspace_path"] for item in host.seed_projection["work_items"]}) == 2
    assert projections[1:] == projections[:-1]


def transport_for(action, reference=None, planner=None):
    def transport(messages, **kwargs):
        kind = probe._QUERY_KIND.get()
        if kind == "role":
            result = json.dumps({"say": "確認したわ。", "action": action}, ensure_ascii=False)
        elif kind == "reference":
            result = json.dumps({"references": [reference]})
        elif kind == "planner":
            result = json.dumps({"decisions": planner}, ensure_ascii=False)
        else:
            result = "台帳の受理結果を確認したわ。"
        if kwargs.get("on_text"):
            kwargs["on_text"](result)
        if kwargs.get("response_observer"):
            kwargs["response_observer"]({"requested_model": "synthetic-contract-fixture", "content": result})
        return result
    return transport


@pytest.mark.parametrize("strategy", ["basic", "professional"])
async def test_no_action_uses_real_ingress_and_presentation_without_provider_effect(tmp_path, strategy):
    fixture, row = fixture_row("emotion")
    arm = next(arm for arm in fixture["arms"] if arm["strategy"] == strategy and not arm["head"])
    result = await probe.run_case(tmp_path, row, arm, probe.QueryObserver(transport_for(None)))
    assert result["status"] == "observed", result.get("error")
    assert result["routing"]["observed"] == ["none"]
    assert result["pre_teardown"]["work_items"] == []
    assert result["pre_teardown"]["display"]
    assert [call["kind"] for call in result["calls"]] == ["role"]
    assert result["persona_acceptance"].startswith("pending human")


@pytest.mark.parametrize("strategy", ["basic", "professional"])
async def test_work_runs_through_real_host_acceptance_and_registered_inert_delivery(tmp_path, strategy):
    fixture, row = fixture_row("fresh_draft")
    arm = next(arm for arm in fixture["arms"] if arm["strategy"] == strategy and not arm["head"])
    planner = [{"proposal_index": 0, "provider": "codex", "intent": "execute", "source_clause": row["source"],
        "references": None, "reference_mode": "none", "work_placement": "draft", "session_context": "unchanged",
        "workspace_effect": "write", "payload_continuity": "current_turn"}]
    observer = probe.QueryObserver(transport_for({"op": "work", **({} if strategy == "professional" else {"intent": "execute"})}, planner=planner))
    result = await probe.run_case(tmp_path, row, arm, observer)
    assert result["status"] == "observed", result.get("error")
    assert result["routing"]["observed"] == ["execute"]
    starts = [event for event in result["pre_teardown"]["adapter_events"] if event["phase"] == "evaluation" and event["kind"] == "start"]
    assert len(starts) == 1 and starts[0]["metadata"]["source"] == "control_work_effect"
    attempt, = result["pre_teardown"]["attempts"]
    assert attempt["origin_effect_id"]
    assert starts[0]["run_id"] == attempt["provider_run_id"]
    assert ("planner" in [call["kind"] for call in result["calls"]]) == (strategy == "professional")
    assert not any(event["phase"] == "cleanup" for event in result["pre_teardown"]["adapter_events"])
    assert {entry.name for entry in Path(starts[0]["cwd"]).iterdir()} <= {".git"}


async def test_same_run_amendment_input_retains_requirement_and_does_not_count_cleanup_stop(tmp_path):
    fixture, row = fixture_row("active_amend")
    observer = probe.QueryObserver(forbidden_query)
    async with probe.CaseHost(tmp_path, row, fixture["arms"][0], observer) as host:
        binding = host.seed_bindings["profile"]
        observer.transport = transport_for({"op": "work", "intent": "amend", "target": "个人资料页"},
            reference="work_item:" + binding["work_item_id"])
        snapshot = await host.submit()
        observed = probe.routing_observation(snapshot)
        assert observed["observed"] == ["amend"], snapshot["receipts"]
        assert len(snapshot["attempts"]) == 1
        assert len(snapshot["inputs"]) == 1
        assert snapshot["inputs"][0]["state"] == "delivered"
        assert any(operation["intent"] == "amend" for operation in snapshot["operations"])
    assert "retract" not in observed["observed"]


async def test_malformed_model_reply_remains_an_error_not_a_none_pass(tmp_path):
    fixture, row = fixture_row("emotion")
    result = await probe.run_case(tmp_path, row, fixture["arms"][0],
        probe.QueryObserver(lambda *_args, **_kwargs: "malformed protocol"))
    assert result["status"] == "error"
    assert result["routing"]["match"] is False
    assert result["pre_teardown"]["transport_events"]


async def test_failure_after_acceptance_retains_effects_before_cleanup(tmp_path, monkeypatch):
    fixture, row = fixture_row("fresh_draft")
    submit = probe.CaseHost.submit
    async def fail_after_submission(host):
        await submit(host)
        raise RuntimeError("injected late instrument failure")
    monkeypatch.setattr(probe.CaseHost, "submit", fail_after_submission)
    result = await probe.run_case(tmp_path, row, fixture["arms"][0],
        probe.QueryObserver(transport_for({"op": "work", "intent": "execute"})))
    assert result["status"] == "error"
    assert result["routing"]["observed"] == ["execute"]
    assert result["routing"]["match"] is False
    assert result["pre_teardown"]["effects"] and result["pre_teardown"]["work_items"]
    assert all(event["phase"] != "cleanup" for event in result["pre_teardown"]["adapter_events"])


def test_call_budget_counts_actual_query_invocations_and_keeps_history(monkeypatch):
    from llm import client
    monkeypatch.setattr(client, "LLM_PROVIDER", "hybrid2")
    observer = probe.QueryObserver(transport_for(None), max_calls=1)
    observer.phase = "evaluation"
    messages = [{"role": "system", "content": "contract"}, {"role": "user", "content": "source"}]
    observer.query(messages)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        observer.query(messages)
    assert observer.calls[0]["messages"] == messages
    assert observer.calls[0]["response"]["requested_model"] == "synthetic-contract-fixture"


async def test_report_route_uses_fixture_ledger_and_configured_production_narrator(tmp_path, monkeypatch):
    from server import work_observer_llm
    from server.work_ledger_coordinator import get_work_ledger_coordinator
    queried = []

    def native_client(_provider):
        def create(**kwargs):
            queried.append(kwargs)
            text = json.dumps({"action": "speak", "display_text": "実行は終了しているわ。",
                "append_to_main_chat": True, "speak": True})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=lambda: None)

    monkeypatch.setattr(work_observer_llm, "_client", native_client)
    fixture, row = fixture_row("terminal_question")
    observer = probe.QueryObserver(forbidden_query)
    async with probe.CaseHost(tmp_path, row, fixture["arms"][0], observer) as host:
        assert get_work_ledger_coordinator() is host.coordinator
        binding = host.seed_bindings["search"]
        observer.transport = transport_for({"op": "report", "target": "清单页搜索功能"},
            reference="work_item:" + binding["work_item_id"])
        snapshot = await host.submit()
        assert probe.routing_observation(snapshot)["observed"] == ["report"], snapshot["receipts"]
        assert queried
        facts = json.loads(queried[0]["messages"][-1]["content"])
        assert binding["work_item_id"] in json.dumps(facts)
        assert snapshot["display"]
        assert any(call["transport"] == "production_work_observer_sdk" for call in observer.calls if call["kind"] == "report_presentation")


async def test_actual_head_hook_alone_changes_role_prompt_and_full_remote_reply_is_kept(tmp_path, monkeypatch):
    from llm import client, hybrid_stream
    monkeypatch.setattr(client, "LLM_PROVIDER", "hybrid2")
    async def head(_messages):
        yield "受け取ったわ。"
    monkeypatch.setattr(hybrid_stream, "local_head_tokens", head)
    fixture, row = fixture_row("emotion")
    results = []
    for arm in fixture["arms"][:2]:
        results.append(await probe.run_case(tmp_path, row, arm, probe.QueryObserver(transport_for(None))))
    assert all(result["status"] == "observed" for result in results)
    systems = [next(call["messages"][0]["content"] for call in result["calls"] if call["kind"] == "role") for result in results]
    assert "A separate local voice" not in systems[0]
    assert "A separate local voice" in systems[1]
    assert [call["kind"] for call in results[0]["calls"]] == ["role"]
    assert sum(call["kind"] == "head" for call in results[1]["calls"]) == 1
    assert "確認したわ。" in results[1]["pre_teardown"]["display"][0]["text"]
