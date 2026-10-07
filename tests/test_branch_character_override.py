"""Persona edits reach branch requests while Host action boundaries stay intact."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm import character_prompts as characters, prompts
from server import auip_b2_role_llm as b2
from server import auip_role_authorizer_llm as authorizer
from server import browser_branch_planner as browser
from server import work_observer_llm as observer
from server.auip_action_candidates import compile_auip_action_candidates
from server.auip_contract import AuipProtocolError
from test_auip_b2 import _runtime
from vn_player.prompt_layers import compose_messages
from vn_player.schemas import VNProfile


OVERRIDE = "あなたは慎重な牧瀬紅莉栖。独自の判断で自然に話してください。PRF_PERSONA"


@pytest.fixture(autouse=True)
def default_role(monkeypatch):
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", characters.load("kurisu"))
    monkeypatch.setattr(prompts, "_character_prompt_ja", "")
    monkeypatch.setattr("tts.pipeline.TTS_OUTPUT_LANGUAGE", "日文")


async def _branch_requests(monkeypatch, app=None):
    """Capture real B2, authorizer and browser assembly at their I/O ports."""
    captured = {}
    runtime, registered = app or _runtime()
    compilation = compile_auip_action_candidates(runtime, registered["app_session_id"])
    selected = next(iter(compilation.candidates))

    async def schema(**kwargs):
        captured["b2"] = kwargs
        return {"candidate_id": selected, "choice_reason": "One legal continuation."}

    async def tool(**kwargs):
        captured["authorizer"] = kwargs
        return "decide_auip_proposal", {"role_alignment": "matches", "decision": "approve",
            "reason": "The exact proposal matches the visible response."}

    def create(**kwargs):
        captured["browser"] = kwargs
        content = json.dumps({"actions": [{"action": "click_ref", "ref": "br_valid"},
            {"action": "click_ref", "ref": "invented"},
            {"action": "fill_ref", "ref": "br_valid", "value": "cannot fill a link"}],
            "final_report": "ページを確認しました。"})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(b2, "call_auip_schema", schema)
    monkeypatch.setattr(authorizer, "call_auip_tool", tool)
    monkeypatch.setattr(browser, "_provider", lambda: "deepseek")
    monkeypatch.setattr(browser, "_client", lambda _provider: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    choice = await b2.choose_b2_role_action(context=compilation.context, candidates=compilation.candidates,
        user_instruction="", branch_messages=[], trigger="explicit_step", speech_required=False)
    authorization = await authorizer.authorize_with_main_role({"current_role_response": "その手を選びます。"})
    decision = browser._decide_sync({"latest_user_instruction": "Open the visible result.",
        "interaction_refs": [{"ref": "br_valid", "fillable": False, "label": "Result"}]})
    assert choice["candidate_id"] in compilation.candidates
    assert authorization["decision"] == "approve"
    assert [action["ref"] for action in decision["actions"]] == ["br_valid"]
    return captured, runtime, registered, compilation.candidates[choice["candidate_id"]]


def _systems(captured):
    return {"b2": captured["b2"]["system_prompt"],
        "authorizer": captured["authorizer"]["system_prompt"],
        "browser": captured["browser"]["messages"][0]["content"]}


async def test_edit_and_clear_reach_real_branch_requests_without_changing_contracts(monkeypatch):
    app = _runtime()
    baseline, *_ = await _branch_requests(monkeypatch, app)
    prompts.set_character_prompt(OVERRIDE)
    edited, runtime, registered, candidate = await _branch_requests(monkeypatch, app)
    for key, system in _systems(edited).items():
        assert system.startswith(OVERRIDE + "\n\n"), key
        assert prompts._JA_LANGUAGE_RULES in system and prompts._JA_OUTPUT_RULES in system
    assert edited["b2"]["schema"] == baseline["b2"]["schema"]
    assert edited["b2"]["payload"] == baseline["b2"]["payload"]
    assert edited["authorizer"]["tools"] == baseline["authorizer"]["tools"]
    assert edited["browser"]["messages"][1] == baseline["browser"]["messages"][1]

    # The role still cannot authorize an occupied cell or stale state. A valid
    # action succeeds under the same runtime checks and fixed wire actor.
    for payload, revision in (({"x": 0, "y": 0}, candidate.revision),
            (candidate.payload, candidate.revision - 1)):
        with pytest.raises(AuipProtocolError):
            runtime.invoke_action(app_session_id=registered["app_session_id"], actor="kurisu",
                type="game.place", payload=payload, expected_revision=revision,
                expected_decision_generation=candidate.decision_generation)
    action = runtime.invoke_action(app_session_id=registered["app_session_id"], actor="kurisu",
        type=candidate.action_type, payload=candidate.payload, expected_revision=candidate.revision,
        expected_decision_generation=candidate.decision_generation)["action"]
    assert action["actor"] == "kurisu" and action["payload"] == candidate.payload

    prompts.set_character_prompt("")
    restored, *_ = await _branch_requests(monkeypatch)
    assert _systems(restored) == _systems(baseline)


@pytest.mark.parametrize("alignment", ["conflicts", "unsettled", "not_applicable"])
async def test_edited_persona_cannot_make_inconsistent_authorization_execute(monkeypatch, alignment):
    prompts.set_character_prompt(OVERRIDE)
    query = AsyncMock(return_value=("decide_auip_proposal", {
        "role_alignment": alignment, "decision": "approve", "reason": "Synthetic conflict."}))
    monkeypatch.setattr(authorizer, "call_auip_tool", query)
    result = await authorizer.authorize_with_main_role({"current_role_response": "あなたが先に打ってください。"})
    assert query.await_args.kwargs["system_prompt"].startswith(OVERRIDE + "\n\n")
    assert result["decision"] == "reject"


async def test_english_branch_requests_ignore_japanese_override(monkeypatch):
    monkeypatch.setattr("tts.pipeline.TTS_OUTPUT_LANGUAGE", "英文")
    baseline, *_ = await _branch_requests(monkeypatch)
    prompts.set_character_prompt(OVERRIDE)
    edited, *_ = await _branch_requests(monkeypatch)
    assert _systems(edited) == _systems(baseline)
    assert prompts.get_character_prompt_config()["main_chat_character_prompt_preview"]["active"] is False


async def test_mira_branch_requests_ignore_saved_kurisu_persona(monkeypatch, tmp_path):
    # Load the same name-only resource as PR-3 in an isolated startup context.
    resource = Path(__file__).parent / "fixtures/mira_character.toml"
    (tmp_path / "mira.toml").write_bytes(resource.read_bytes())
    with monkeypatch.context() as loading:
        loading.setattr(characters, "files", lambda _package: tmp_path)
        character = characters.load("mira")
    monkeypatch.setattr(characters, "_ACTIVE_CHARACTER", character)
    baseline, *_ = await _branch_requests(monkeypatch)
    prompts.set_character_prompt(OVERRIDE)
    edited, *_ = await _branch_requests(monkeypatch)
    assert _systems(edited) == _systems(baseline)
    assert all(OVERRIDE not in system for system in _systems(edited).values())
    preview = prompts.get_character_prompt_config()["main_chat_character_prompt_preview"]
    assert preview["active"] is False and preview["effective"] == OVERRIDE
    assert preview["default"] == prompts.DEFAULT_CHARACTER_PROMPT_JA


def test_vn_work_commentary_and_hybrid_opening_ignore_user_persona(monkeypatch):
    profile = VNProfile(session_id="vn-prf", game_id="synthetic", prompt_pack="base", game_title="Story")
    lanes = ("immediate", "lookahead", "reasoner", "summary", "retrospective")
    vn = {lane: compose_messages(profile, lane, "Synthetic story context.") for lane in lanes}
    hybrid = prompts.get_system_prompt("hybrid_local")
    observer_requests = []

    def create(**kwargs):
        observer_requests.append(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"action":"silent"}'))])

    monkeypatch.setattr(observer, "_client", lambda _provider: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    arguments = dict(provider="deepseek", note={"phase": "Work", "summary": "Synthetic progress."},
        notes=[], recent_chat=[], recent_spoken_updates=[], display_language="english")
    observer._decide_sync(**arguments)
    prompts.set_character_prompt(OVERRIDE)
    observer._decide_sync(**arguments)
    assert observer_requests[0] == observer_requests[1]
    assert {lane: compose_messages(profile, lane, "Synthetic story context.") for lane in lanes} == vn
    assert prompts.get_system_prompt("hybrid_local") == hybrid
