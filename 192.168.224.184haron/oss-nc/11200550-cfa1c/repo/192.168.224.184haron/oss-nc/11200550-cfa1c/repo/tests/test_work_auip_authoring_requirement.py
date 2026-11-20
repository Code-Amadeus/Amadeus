"""Accepted result-entry intent reaches the existing AUIP authoring owner."""
from dataclasses import fields, replace
import hashlib
from pathlib import Path

import pytest

from agent_host.adapters.codex_app_server import CodexAppServerAdapter
from agent_host.provider_types import ProviderRunIntakeAuthority, ProviderSessionHandle
from agent_host.work_ledger_store import WorkLedgerConflict
from server.control_ledger import ControlLedgerConflict
from server.provider_session_binding import ProviderSessionAttachment
from server.work_control import WorkAmendPayloadV4, WorkCooperativeContextPayloadV6
from test_work_effect_executor import _host, _admission, _payload


SOURCE = "做个简单的 2048 吧，做好了咱们玩两把。"
WORK = "做个简单的 2048 吧"
ENTRY = "做好了咱们玩两把。"


def accepted_handoff():
    return {"cooperative_batch":{"version":1, "kind":"work_then_auip_after_work",
        "actions":[{"index":index, "op":op, **details,
            "source_start":SOURCE.index(source), "source_end":SOURCE.index(source) + len(source),
            "source_sha256":hashlib.sha256(source.encode("utf-8")).hexdigest()}
            for index, (op, source, details) in enumerate([
                ("work", WORK, {"intent":"execute"}),
                ("auip_after_work", ENTRY, {"mode":"collaborate"})])]}}


def accepted_full_source_handoff():
    evidence = accepted_handoff()
    entry = evidence["cooperative_batch"]["actions"][1]
    entry.update(source_start=0, source_end=len(SOURCE),
        source_sha256=hashlib.sha256(SOURCE.encode("utf-8")).hexdigest())
    return evidence


@pytest.mark.parametrize("evidence_version,mode", [(1, "collaborate"), (2, "observe"),
    (2, "collaborate"), (2, "delegate")])
async def test_new_entry_requirement_keeps_mode_and_old_admissions_replay(tmp_path, evidence_version, mode):
    async with _host(tmp_path) as host:
        admission = _admission(suffix="entry-mode", epoch=2, text=SOURCE)
        host.control.admit(admission, fence_scope="foreground-chat")
        evidence = accepted_handoff()
        evidence["cooperative_batch"]["version"] = evidence_version
        evidence["cooperative_batch"]["actions"][1]["mode"] = mode
        payload = _payload(host.project.project_id, host.adapter.provider_id,
            suffix="entry-mode", source=SOURCE, task=WORK)
        accepted = host.control.seal(admission, payload, plan_evidence=evidence)
        expected = {"current_attempt_contribution": True}
        if evidence_version == 2:
            expected["engagement_mode"] = mode
        request = host.control.provider_request(accepted["effect_id"])
        assert request.metadata["host_outcome_requirement"]["expected"] == expected
        assert request.mode == "agent"
        assert request.task == WORK
        replay = host.control.provider_request(accepted["effect_id"])
        assert replay.metadata["host_outcome_requirement"]["expected"] == expected
        assert host.adapter.calls == 0


@pytest.mark.parametrize("version", [4, 6])
@pytest.mark.parametrize("wrong_intent", [False, True])
async def test_after_work_requirement_preserves_an_existing_amendment(
        tmp_path, version, wrong_intent):
    async with _host(tmp_path) as host:
        item = host.work.create_work_item(host.project.project_id,
            title="Existing counter", workspace_path=host.workspace)
        predecessor = host.work.create_attempt(item.work_item_id,
            provider=host.adapter.provider_id, task="Create the counter")
        host.work.update_attempt(predecessor.attempt_id, execution_status="succeeded")
        clause = "把刚才那个计数器改成深色"
        source = clause + "，改好再打开咱们试试。"
        admission = _admission(suffix="amend-entry", epoch=2, text=source)
        host.control.admit(admission, fence_scope="foreground-chat")
        base = _payload(host.project.project_id, host.adapter.provider_id,
            suffix="amend-entry", source=source, task=clause)
        attrs = {field.name: getattr(base, field.name) for field in fields(base)}
        if version == 6:
            payload = WorkCooperativeContextPayloadV6(**attrs,
                work_item_id=item.work_item_id,
                cooperative_context_id="retained-counter",
                cooperative_binding_token="counter-binding",
                cooperative_context_revision=1)
            host.control.cooperative_context_resolver = lambda *_args, **_kwargs: ProviderSessionAttachment(
                session=ProviderSessionHandle(provider=host.adapter.provider_id,
                    session_id="counter-native", scope="interaction"),
                audit={"workspace_path": str(host.workspace)})
        else:
            payload = WorkAmendPayloadV4(**attrs, work_item_id=item.work_item_id)
        evidence = {"cooperative_batch": {"version": 1,
            "kind": "work_then_auip_after_work", "actions": [
                {"index": 0, "op": "work", "intent": "execute" if wrong_intent else "amend",
                    "source_start": 0, "source_end": len(clause),
                    "source_sha256": hashlib.sha256(clause.encode()).hexdigest()},
                {"index": 1, "op": "auip_after_work", "mode": "collaborate",
                    "source_start": 0, "source_end": len(source),
                    "source_sha256": hashlib.sha256(source.encode()).hexdigest()},
            ]}}
        if wrong_intent:
            with pytest.raises(ControlLedgerConflict, match="AUIP handoff"):
                host.control.seal(admission, payload, plan_evidence=evidence)
            assert host.control_store.get_admission(admission.root_id)["plan_id"] is None
        else:
            accepted = host.control.seal(admission, payload, plan_evidence=evidence)
            request = host.control.provider_request(accepted["effect_id"])
            assert request.task == clause
            assert request.cwd == item.workspace_path
            assert request.metadata["intent"] == "amend"
            assert request.metadata["work"]["work_item_id"] == item.work_item_id
            assert request.metadata["host_outcome_requirement"]["facet"] == "auip.application"
        assert len(host.work.list_work_items()) == 1
        assert len(host.work.list_attempts(item.work_item_id)) == 1
        assert host.adapter.calls == 0


@pytest.mark.parametrize("tamper", [None, "wrong_work", "execute"])
async def test_preparation_requirement_belongs_to_one_existing_work(tmp_path, tamper):
    async with _host(tmp_path) as host:
        item = host.work.create_work_item(host.project.project_id, title="Existing application",
            workspace_path=host.workspace)
        predecessor = host.work.create_attempt(item.work_item_id, provider=host.adapter.provider_id,
            task="Create the application")
        host.work.update_attempt(predecessor.attempt_id, execution_status="succeeded")
        source = "把刚才那个打开，咱们一起用吧。"
        admission = _admission(suffix="preparation", epoch=2, text=source)
        host.control.admit(admission, fence_scope="foreground-chat")
        base = _payload(host.project.project_id, host.adapter.provider_id,
            suffix="preparation", source=source, task=source)
        payload = base if tamper == "execute" else WorkAmendPayloadV4(
            **{field.name:getattr(base, field.name) for field in fields(base)}, work_item_id=item.work_item_id)
        evidence = {"auip_preparation_work_item_id":"different-work" if tamper == "wrong_work" else item.work_item_id}
        if tamper:
            with pytest.raises(ControlLedgerConflict, match="AUIP preparation"):
                host.control.seal(admission, payload, plan_evidence=evidence)
            assert host.control_store.get_admission(admission.root_id)["plan_id"] is None
        else:
            effect = host.control.seal(admission, payload, plan_evidence=evidence)
            request = host.control.provider_request(effect["effect_id"])
            assert request.task == source
            assert request.metadata["host_outcome_requirement"]["facet"] == "auip.application"
        assert host.adapter.calls == 0


@pytest.mark.parametrize("version", [3, 6])
@pytest.mark.parametrize("after_work", [False, True])
async def test_accepted_handoff_stages_existing_sdk_and_authoring_bundle(tmp_path, version, after_work):
    async with _host(tmp_path) as host:
        admission = _admission(suffix="application", epoch=2, text=SOURCE)
        host.control.admit(admission, fence_scope="foreground-chat")
        payload = _payload(host.project.project_id, host.adapter.provider_id,
            suffix="application", source=SOURCE, task=WORK)
        if version == 6:
            host.adapter.manifest = replace(host.adapter.manifest,
                capabilities=replace(host.adapter.manifest.capabilities, resume="attach"))
            host.runtime.register(host.adapter)
            payload = WorkCooperativeContextPayloadV6(
                **{field.name:getattr(payload, field.name) for field in fields(payload)},
                cooperative_context_id="selected-context", cooperative_binding_token="selected-binding",
                cooperative_context_revision=1)
            host.control.cooperative_context_resolver = lambda *_args, **_kwargs:ProviderSessionAttachment(
                session=ProviderSessionHandle(provider=host.adapter.provider_id,
                    session_id="accepted-native-context", scope="interaction"),
                audit={"workspace_path":str(host.workspace),
                    "cooperative_context_id":"selected-context", "cooperative_context_revision":1})
        accepted = host.control.seal(admission, payload,
            plan_evidence=accepted_handoff() if after_work else None)
        effect_id = accepted["effect_id"]
        request = host.control.provider_request(effect_id)
        assert request.task == WORK and request.metadata["source_user_text"] == SOURCE
        dispatch = await host.executor.dispatch(effect_id)
        assert dispatch.record is not None
        await dispatch.record.task_handle
        captured = host.adapter.requests[-1]["request"]
        attempt = host.work.get_attempt(dispatch.binding["attempt_id"])
        if after_work:
            skill = Path(captured.metadata["auip_authoring_skill_path"])
            assert skill.is_file()
            assert (skill.parent / "assets" / "auip.manifest.json").is_file()
            assert (Path(captured.cwd) / "sdk" / "auip-web" / "auip-v0.js").is_file()
            assert attempt.metadata["auip_host_validates_bundle"] is True
            assert attempt.metadata["auip_authoring_inputs"]["staged_file_count"] > 0
            assert attempt.metadata["host_outcome_requirement"]["facet"] == "auip.application"
            # Exercise the existing adapter formatter without starting a native client.
            prompt = CodexAppServerAdapter._task_text(object.__new__(CodexAppServerAdapter), captured)
            assert "Host-authorized AUIP application prerequisite:" in prompt
            assert str(skill) in prompt
        else:
            assert "host_outcome_requirement" not in captured.metadata
            assert "auip_authoring_skill_path" not in captured.metadata
            assert not (host.workspace / "sdk").exists()


async def test_completed_auip_decision_can_bind_its_exact_full_admitted_source(tmp_path):
    async with _host(tmp_path) as host:
        admission = _admission(suffix="full-source", epoch=2, text=SOURCE)
        host.control.admit(admission, fence_scope="foreground-chat")
        payload = _payload(host.project.project_id, host.adapter.provider_id,
            suffix="full-source", source=SOURCE, task=WORK)
        accepted = host.control.seal(
            admission, payload, plan_evidence=accepted_full_source_handoff())

        request = host.control.provider_request(accepted["effect_id"])

        assert request.task == WORK
        assert request.metadata["source_user_text"] == SOURCE
        assert request.metadata["host_outcome_requirement"]["facet"] == "auip.application"
        evidence = accepted_full_source_handoff()["cooperative_batch"]["actions"]
        assert (evidence[0]["source_start"], evidence[0]["source_end"]) == (
            SOURCE.index(WORK), SOURCE.index(WORK) + len(WORK))
        assert (evidence[1]["source_start"], evidence[1]["source_end"]) == (
            0, len(SOURCE))


@pytest.mark.parametrize("tamper", ["partial_overlap", "entry_hash", "mode"])
async def test_full_source_auip_evidence_rejects_partial_or_changed_facts(
        tmp_path, tamper):
    async with _host(tmp_path) as host:
        admission = _admission(suffix="full-invalid", epoch=2, text=SOURCE)
        host.control.admit(admission, fence_scope="foreground-chat")
        evidence = accepted_full_source_handoff()
        entry = evidence["cooperative_batch"]["actions"][1]
        if tamper == "partial_overlap":
            entry["source_start"] = 1
            entry["source_sha256"] = hashlib.sha256(
                SOURCE[entry["source_start"]:entry["source_end"]].encode("utf-8")
            ).hexdigest()
        elif tamper == "entry_hash":
            entry["source_sha256"] = "different"
        else:
            entry["mode"] = "arbitrary"

        with pytest.raises(ControlLedgerConflict, match="AUIP"):
            host.control.seal(admission,
                _payload(host.project.project_id, host.adapter.provider_id,
                    suffix="full-invalid", source=SOURCE, task=WORK),
                plan_evidence=evidence)
        assert host.control_store.get_admission(admission.root_id)["plan_id"] is None


@pytest.mark.parametrize("tamper", ["remove", "change", "inject"])
async def test_runtime_cannot_change_accepted_authoring_requirement(tmp_path, tamper):
    async with _host(tmp_path) as host:
        admission = _admission(suffix="guard", epoch=2, text=SOURCE)
        host.control.admit(admission, fence_scope="foreground-chat")
        effect_id = host.control.seal(admission,
            _payload(host.project.project_id, host.adapter.provider_id,
                suffix="guard", source=SOURCE, task=WORK),
            plan_evidence=None if tamper == "inject" else accepted_handoff())["effect_id"]
        request = host.control.provider_request(effect_id)
        if tamper == "remove":
            request.metadata.pop("host_outcome_requirement", None)
        else:
            request.metadata["host_outcome_requirement"] = {"facet":"auip.application"}
        with pytest.raises(WorkLedgerConflict, match="does not match"):
            await host.runtime.start_accepted(request, ProviderRunIntakeAuthority(effect_id))
        assert host.control.binding(effect_id) is None
        assert host.adapter.calls == 0


@pytest.mark.parametrize("tamper", ["work_span", "entry_hash", "mode"])
async def test_handoff_evidence_must_match_its_admitted_clauses(tmp_path, tamper):
    async with _host(tmp_path) as host:
        admission = _admission(suffix="invalid", epoch=2, text=SOURCE)
        host.control.admit(admission, fence_scope="foreground-chat")
        evidence = accepted_handoff()
        work, entry = evidence["cooperative_batch"]["actions"]
        if tamper == "work_span":
            work["source_end"] += 1
        elif tamper == "entry_hash":
            entry["source_sha256"] = "different"
        else:
            entry["mode"] = "arbitrary"
        with pytest.raises(ControlLedgerConflict, match="AUIP"):
            host.control.seal(admission,
                _payload(host.project.project_id, host.adapter.provider_id,
                    suffix="invalid", source=SOURCE, task=WORK), plan_evidence=evidence)
        assert host.control_store.get_admission(admission.root_id)["plan_id"] is None
