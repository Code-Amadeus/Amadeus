"""Accepted Work identity survives role changes without acquiring new authority."""

from contextlib import contextmanager
from dataclasses import fields, replace
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_host.provider_types import ProviderRunIntakeAuthority
from agent_host.work_ledger_store import WorkLedgerConflict, WorkLedgerStore
from server.control_ledger import ControlEffect, ControlLedgerConflict, ControlLedgerStore
from server.work_control import WorkAmendPayloadV4, WorkControl, WorkEffectPayloadV3, _batch_target_key
from test_work_control_batch import ALPHA, BETA, _admission, _open, _payload
from test_work_effect_executor import _host


KURISU = {"character_id": "kurisu", "display_name": "Makise Kurisu (牧瀬紅莉栖)"}
MIRA = {"character_id": "testchar", "display_name": "Mira"}


@pytest.fixture
def case(tmp_path):
    ledger, work, control, project, admission = _open(tmp_path)
    try:
        yield SimpleNamespace(ledger=ledger, work=work, control=control,
            project=project, admission=admission,
            payload=_payload(admission, project.project_id, ALPHA))
    finally:
        ledger.close()
        work.close()


@contextmanager
def reopened(database, resolver):
    work = WorkLedgerStore(database)
    ledger = ControlLedgerStore(database)
    try:
        yield ledger, work, WorkControl(ledger, work, source_role_identity_resolver=resolver)
    finally:
        ledger.close()
        work.close()


def _plan(ledger, admission):
    return json.loads(ledger.get_admission(admission.root_id)["plan_json"])


def _legacy_accept(control, admission, payloads, evidence):
    """Seed an unmodified pre-identity plan through the existing Ledger port."""
    ids = control._effect_ids(admission.root_id, len(payloads))
    batch = len(payloads) > 1
    control.ledger.accept(admission.root_id, chat_epoch=admission.chat_epoch,
        plan_id=control._plan_id(admission.root_id, payloads),
        effects=tuple(ControlEffect(effect_id, "work",
            _batch_target_key(admission.root_id, ordinal, payload) if batch else
                f"work-source:{admission.session_id}:{admission.utterance_id}",
            payload.to_payload()) for ordinal, (effect_id, payload) in enumerate(zip(ids, payloads))),
        evidence={"adapter": "proposal_gated_current_turn_work_batch:v1" if batch else
            "proposal_gated_current_turn_work:v3", "transcript_hash": admission.transcript_hash,
            **evidence})
    return ids


def _bind_request(control, effect_id, request):
    work = request.metadata["work"]
    return control.bind_runtime_dispatch_intent(ProviderRunIntakeAuthority(effect_id), request,
        provider_run_id="inert-provider_run-" + effect_id, project_id=work["project_id"],
        title=work["title"], goal=request.task, workspace_mode=work["workspace_mode"],
        workspace_path=work["workspace_path"], branch="", base_revision="",
        work_metadata={}, operation_metadata={}, attempt_metadata={})


def test_acceptance_copies_source_identity_once_and_role_rename_cannot_change_it(case):
    identity = dict(KURISU)
    resolver = Mock(return_value=identity)
    control = WorkControl(case.ledger, case.work, source_role_identity_resolver=resolver)
    evidence = {"selection": "accepted source"}
    accepted = control.seal(case.admission, case.payload, plan_evidence=evidence)
    stored = case.ledger.get_admission(case.admission.root_id)["plan_json"]
    original_request = control.provider_request(accepted["effect_id"])
    assert _plan(case.ledger, case.admission)["evidence"]["role_identity"] == KURISU
    resolver.assert_called_once_with(case.admission.session_id)

    identity["display_name"] = "Renamed Kurisu"
    identity["character_id"] = "testchar"
    replay = control.seal(case.admission, case.payload, plan_evidence=evidence)
    assert replay["replayed"] and replay["effect_id"] == accepted["effect_id"]
    assert case.ledger.get_admission(case.admission.root_id)["plan_json"] == stored
    assert resolver.call_count == 1
    request = control.provider_request(accepted["effect_id"])
    assert request.metadata["main_role_name"] == KURISU["display_name"]
    control.validate_runtime_request(ProviderRunIntakeAuthority(accepted["effect_id"]), original_request)
    request.metadata["main_role_name"] = identity["display_name"]
    with pytest.raises(WorkLedgerConflict, match="does not match accepted Work effect"):
        control.validate_runtime_request(ProviderRunIntakeAuthority(accepted["effect_id"]), request)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_reopened_ledger_reseals_and_binds_original_identity_without_role_lookup(tmp_path, legacy, batch):
    ledger, work, _, project, admission = _open(tmp_path)
    payloads = tuple(_payload(admission, project.project_id, task)
        for task in ((ALPHA, BETA) if batch else (ALPHA,)))
    evidence = {"selection": "accepted source"}
    resolver = Mock(return_value=KURISU)
    control = WorkControl(ledger, work, source_role_identity_resolver=resolver)
    try:
        if legacy:
            effect_ids = _legacy_accept(control, admission, payloads, evidence)
            assert resolver.call_count == 0
        else:
            effect_ids = control.seal_many(admission, payloads, plan_evidence=evidence)["effect_ids"]
            resolver.assert_called_once_with(admission.session_id)
        stored = ledger.get_admission(admission.root_id)["plan_json"]
        original_requests = [control.provider_request(effect_id) for effect_id in effect_ids]
    finally:
        ledger.close()
        work.close()

    second_role_resolver = Mock(return_value=MIRA)
    with reopened(tmp_path / "shared.sqlite3", second_role_resolver) as (ledger, work, restored):
        # Re-sealing, not only reading, must preserve legacy field absence.
        replay = restored.seal_many(admission, payloads, plan_evidence=evidence)
        assert replay["replayed"] and replay["effect_ids"] == effect_ids
        assert ledger.get_admission(admission.root_id)["plan_json"] == stored
        assert ("role_identity" in _plan(ledger, admission)["evidence"]) is not legacy
        for effect_id, original_request in zip(effect_ids, original_requests):
            request = restored.provider_request(effect_id)
            assert request.metadata["main_role_name"] == KURISU["display_name"]
            restored.validate_runtime_request(ProviderRunIntakeAuthority(effect_id), original_request)
            bound = _bind_request(restored, effect_id, request)
            assert restored.runtime_binding(effect_id) == bound["binding"]
            assert work.get_attempt(bound["binding"]["attempt_id"]).origin_effect_id == effect_id
        second_role_resolver.assert_not_called()


def test_one_batch_resolves_source_once_and_shares_that_fact_across_effects(case):
    resolver = Mock(return_value=MIRA)
    control = WorkControl(case.ledger, case.work, source_role_identity_resolver=resolver)
    payloads = (case.payload, _payload(case.admission, case.project.project_id, BETA))
    accepted = control.seal_many(case.admission, payloads)
    resolver.assert_called_once_with(case.admission.session_id)
    assert _plan(case.ledger, case.admission)["evidence"]["role_identity"] == MIRA
    for effect_id in accepted["effect_ids"]:
        request = control.provider_request(effect_id)
        assert request.metadata["main_role_name"] == MIRA["display_name"]
        control.validate_runtime_request(ProviderRunIntakeAuthority(effect_id), request)


@pytest.mark.parametrize("provided", [KURISU, MIRA, None])
def test_caller_cannot_supply_identity_even_if_it_matches_host(case, provided):
    resolver = Mock(return_value=KURISU)
    control = WorkControl(case.ledger, case.work, source_role_identity_resolver=resolver)
    with pytest.raises(ControlLedgerConflict, match="cannot replace source authority"):
        control.seal(case.admission, case.payload, plan_evidence={"role_identity": provided})
    resolver.assert_not_called()
    assert case.ledger.get_admission(case.admission.root_id)["plan_id"] is None
    control.seal(case.admission, case.payload)
    with pytest.raises(ControlLedgerConflict, match="cannot replace source authority"):
        control.seal(case.admission, case.payload, plan_evidence={"role_identity": provided})
    assert resolver.call_count == 1


INVALID_IDENTITIES = [None, False, {}, {"character_id": "testchar"},
    {**MIRA, "extra": "not an identity field"},
    {**MIRA, "character_id": 1}, {**MIRA, "character_id": ""},
    {**MIRA, "display_name": " "}, {**MIRA, "display_name": " Mira"},
    {**MIRA, "display_name": "Mi\x00ra"}, {**MIRA, "display_name": "\ud800"}]


@pytest.mark.parametrize("identity", INVALID_IDENTITIES)
def test_invalid_resolved_identity_cannot_be_accepted(case, identity):
    resolver = Mock(return_value=identity)
    control = WorkControl(case.ledger, case.work, source_role_identity_resolver=resolver)
    with pytest.raises(ControlLedgerConflict, match="role_identity"):
        control.seal(case.admission, case.payload)
    assert case.ledger.get_admission(case.admission.root_id)["plan_id"] is None


@pytest.mark.parametrize("identity", INVALID_IDENTITIES)
def test_explicit_malformed_stored_identity_never_becomes_legacy(case, identity):
    accepted = case.control.seal(case.admission, case.payload)
    original_request = case.control.provider_request(accepted["effect_id"])
    plan = _plan(case.ledger, case.admission)
    plan["evidence"]["role_identity"] = identity
    with case.ledger._transaction() as db:
        db.execute("UPDATE control_admissions SET plan_json=? WHERE root_id=?",
            (json.dumps(plan, sort_keys=True, separators=(",", ":")), case.admission.root_id))
    resolver = Mock(side_effect=AssertionError("stored identity must not resolve again"))
    restored = WorkControl(case.ledger, case.work, source_role_identity_resolver=resolver)
    with pytest.raises(ControlLedgerConflict, match="role_identity"):
        restored.seal(case.admission, case.payload)
    with pytest.raises(ControlLedgerConflict, match="role_identity"):
        restored.provider_request(accepted["effect_id"])
    with pytest.raises(ControlLedgerConflict, match="role_identity"):
        restored.validate_runtime_request(ProviderRunIntakeAuthority(accepted["effect_id"]), original_request)
    resolver.assert_not_called()


@pytest.mark.parametrize("tamper", ["changed evidence", "missing evidence", "effect"])
@pytest.mark.parametrize("legacy", [False, True])
def test_replay_still_compares_other_evidence_and_effects(case, tamper, legacy):
    resolver = Mock(return_value=KURISU)
    control = WorkControl(case.ledger, case.work, source_role_identity_resolver=resolver)
    original_evidence = {"selection": "accepted source"}
    if legacy:
        _legacy_accept(control, case.admission, (case.payload,), original_evidence)
    else:
        control.seal(case.admission, case.payload, plan_evidence=original_evidence)
    before = case.ledger.get_admission(case.admission.root_id)["plan_json"]
    evidence = {"selection": "changed source"} if tamper == "changed evidence" else (
        {} if tamper == "missing evidence" else original_evidence)
    payload = replace(case.payload, provider="other-provider") if tamper == "effect" else case.payload
    with pytest.raises(ControlLedgerConflict, match="accepted decision is immutable"):
        control.seal(case.admission, payload, plan_evidence=evidence)
    assert case.ledger.get_admission(case.admission.root_id)["plan_json"] == before
    assert resolver.call_count == (0 if legacy else 1)


def test_source_identity_resolution_failure_accepts_no_plan(case):
    resolver = Mock(side_effect=WorkLedgerConflict("source session does not belong to startup role"))
    control = WorkControl(case.ledger, case.work, source_role_identity_resolver=resolver)
    with pytest.raises(WorkLedgerConflict, match="source session"):
        control.seal(case.admission, case.payload)
    assert case.ledger.get_admission(case.admission.root_id)["plan_id"] is None


async def test_cross_role_amend_uses_new_source_and_keeps_original_attempt(tmp_path):
    async with _host(tmp_path, source="You yourself build the accepted artifact.",
            task="You yourself build the accepted artifact.") as host:
        initial = await host.executor.execute(host.effect_id)
        binding = initial["binding"]
        original_attempt = host.work.get_attempt(binding["attempt_id"])
        original_work = host.work.get_work_item(binding["work_item_id"])
        original_plan = host.control_store.get_admission(
            host.control_store.get_effect(host.effect_id)["root_id"])["plan_json"]
        resolver = Mock(return_value=MIRA)
        restored = WorkControl(host.control_store, host.work, source_role_identity_resolver=resolver)
        source = "You yourself append the revised requirement."
        admission = replace(_admission(suffix="mira-amend", source=source),
            session_id="mira-session", dialogue_source_scope="chat:mira-session")
        restored.admit(admission, fence_scope="mira-chat")
        base = _payload(admission, host.project.project_id, source, source=source)
        base = replace(base, provider=host.adapter.provider_id)
        payload = WorkAmendPayloadV4(**{field.name: getattr(base, field.name)
            for field in fields(WorkEffectPayloadV3)}, work_item_id=binding["work_item_id"])
        accepted = restored.seal(admission, payload)
        resolver.assert_called_once_with("mira-session")
        request = restored.provider_request(accepted["effect_id"])
        assert request.task == source and request.metadata["main_role_name"] == MIRA["display_name"]
        restored.validate_runtime_request(ProviderRunIntakeAuthority(accepted["effect_id"]), request)
        old_request = restored.provider_request(host.effect_id)
        assert old_request.metadata["main_role_name"] == KURISU["display_name"]
        assert old_request.task == "You yourself build the accepted artifact."
        restored.validate_runtime_request(ProviderRunIntakeAuthority(host.effect_id), old_request)
        assert host.work.get_attempt(binding["attempt_id"]) == original_attempt
        assert host.work.get_work_item(binding["work_item_id"]) == original_work
        assert host.control_store.get_admission(
            host.control_store.get_effect(host.effect_id)["root_id"])["plan_json"] == original_plan


@pytest.mark.parametrize("legacy", [False, True])
def test_resealing_and_reconstructing_work_needs_no_current_role_or_character_pack(case, legacy):
    effect_id = (_legacy_accept(case.control, case.admission, (case.payload,), {})[0]
        if legacy else case.control.seal(case.admission, case.payload)["effect_id"])
    stored = case.ledger.get_admission(case.admission.root_id)["plan_json"]
    script = """
import sys
from pathlib import Path
from llm import character_prompts
def unavailable(*args, **kwargs):
    raise AssertionError('Work reconstruction cannot look up current role or load a pack')
character_prompts.load = unavailable
character_prompts.active_character_id = unavailable
class UnavailableRoleModules:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'server.inherited_role_prompt':
            raise AssertionError('Work reconstruction cannot load ' + fullname)
sys.meta_path.insert(0, UnavailableRoleModules())
from agent_host.provider_types import ProviderRunIntakeAuthority
from agent_host.work_ledger_store import WorkLedgerStore
from server.control_ledger import ControlLedgerStore
from server.work_control import WorkControl
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_work_control_batch import _admission, _payload, ALPHA
ledger = ControlLedgerStore(sys.argv[1])
work = WorkLedgerStore(sys.argv[1])
try:
    control = WorkControl(ledger, work, source_role_identity_resolver=unavailable)
    request = control.provider_request(sys.argv[2])
    control.validate_runtime_request(ProviderRunIntakeAuthority(sys.argv[2]), request)
    admission = _admission()
    payload = _payload(admission, request.metadata['work']['project_id'], ALPHA)
    assert control.seal(admission, payload)['replayed']
finally:
    ledger.close()
    work.close()
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script,
        str(case.ledger.path), effect_id],
        cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert case.ledger.get_admission(case.admission.root_id)["plan_json"] == stored
