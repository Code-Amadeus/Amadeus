"""Byte-exact pre-migration snapshots of model-visible character prompts.

Verification never updates the expected data. Explicit baseline recording:
  uv run --locked --no-sync python -X utf8 tests/test_character_prompt_snapshots.py --record

Capture runs in a bounded fresh process. Only configuration and external I/O
boundaries are substituted; production prompt builders and message assembly run
normally. The TTS module substitute supplies only the language authority, without
importing audio/model stacks. No user settings, .env, model, or network is used.
The existing VN fixture supplies synthetic inputs and remains untouched; these
snapshots additionally freeze the current exact transport spelling of its lanes.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import itertools
import json
from pathlib import Path, PureWindowsPath
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/character_prompt_snapshots.json"
VN_FIXTURE = ROOT / "tests/fixtures/vn_prompt_messages.json"
BASELINE = "e56faa15890af54e66e15f465ed66449e1ee125b"
PROVIDERS = ("browser", "codex", "openclaw")
SYNTHETIC_ENV = {
    "WORK_CODING_PROVIDER": "codex",
    "WORK_EXECUTION_PROVIDER": "openclaw",
    "AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA": "",
    "DEEPSEEK_API_KEY": "snapshot-test-key",
    "DEEPSEEK_BASE_URL": "https://example.invalid/v1",
    "DEEPSEEK_MODEL_NAME": "snapshot-model",
    "VN_TTS_TRANSLATE_PROVIDER": "deepseek",
    "VN_SUBTITLE_TRANSLATE_PROVIDER": "deepseek",
    "TTS_DEVICE": "cpu",
    "LOCALAPPDATA": "C:/snapshot/appdata",
    "USERPROFILE": "C:/snapshot/home",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


class _CaptureComplete(BaseException):
    """Stop at the model boundary, before response handling or execution."""


def _capture_prompts() -> dict[str, str]:
    # This must run only in the isolated --capture process: callers in pytest
    # retain their real module identities and settings after capture terminates.
    sys.path.insert(0, str(ROOT))
    import config.environment as environment
    import tts

    language = ModuleType("tts.pipeline")
    language.TTS_OUTPUT_LANGUAGE = "日文"
    settings_reader = environment.EnvironmentReader(SYNTHETIC_ENV)
    output: dict[str, str] = {}

    def forbidden_io(*_args, **_kwargs):
        raise AssertionError("prompt snapshot attempted external I/O")

    with asyncio.Runner() as runner, ExitStack() as stack:
        # Windows creates a private socketpair for the event loop. Initialize
        # it before denying connections; all production I/O remains guarded.
        runner.get_loop()
        stack.enter_context(patch.dict("os.environ", SYNTHETIC_ENV, clear=True))
        stack.enter_context(patch.object(environment, "load_project_environment",
                                        return_value=settings_reader))
        stack.enter_context(patch.dict(sys.modules, {"tts.pipeline": language}))
        stack.enter_context(patch.object(tts, "pipeline", language, create=True))
        stack.enter_context(patch("socket.socket.connect", forbidden_io))
        stack.enter_context(patch("socket.create_connection", forbidden_io))
        stack.enter_context(patch("subprocess.Popen", forbidden_io))
        stack.enter_context(patch("platform.system", return_value="Windows"))

        from config import settings
        from llm import prompts
        from agent_host.provider_runtime import runtime
        from server import cooperative_provider_loop as cooperative
        from server import inherited_role_prompt as inherited
        from server import auip_narration as narration
        from server import auip_narration_llm as auip_llm
        from server import auip_b2_role_llm as b2
        from server import auip_role_authorizer_llm as authorizer
        from server import browser_branch_planner as browser
        from server import work_observer_llm as observer
        from server import vn_tts_bridge as vn_tts
        from llm import llama_server
        from openclaw import client as openclaw
        from agent_host import provider_authoring, provider_identity
        from server.auip_action_candidates import AuipActionCandidate
        from server.auip_structured_presentation import compile_auip_host_facts
        from vn_player import prompts as vn_prompts
        from vn_player import mystery_policy
        from vn_player.runtime import VNPlayerRuntime
        from vn_player.schemas import VNProfile

        stack.enter_context(patch.object(runtime, "list_providers", return_value=PROVIDERS))
        # Empty override is already the synthetic startup value. Do not patch
        # prompt values or their renderers: migration must exercise those paths.
        assert prompts.get_character_prompt_config()[prompts.CHARACTER_PROMPT_SETTING] == ""

        for lang, tts_language in (("ja", "日文"), ("en", "英文")):
            language.TTS_OUTPUT_LANGUAGE = tts_language
            for intent, amend, retract in itertools.product((False, True), repeat=3):
                settings.DELEGATE_INTENT_ATTRIBUTE = intent
                settings.DELEGATE_AMEND_INTENT = amend
                settings.DELEGATE_RETRACT_INTENT = retract
                switches = f"intent={int(intent)},amend={int(amend)},retract={int(retract)}"
                for variant in ("base", "with_delegate", "bedrock", "hybrid_local"):
                    output[f"main/{lang}/{variant}/{switches}"] = prompts.get_system_prompt(variant)
                output[f"structured_control/{lang}/{switches}"] = prompts.get_structured_control_prompt()

            # Every branch uses the same fixed representative Host switches.
            settings.DELEGATE_INTENT_ATTRIBUTE = True
            settings.DELEGATE_AMEND_INTENT = True
            settings.DELEGATE_RETRACT_INTENT = True
            output[f"inherited/{lang}/base"] = inherited.inherited_main_role_prompt("base")
            output[f"inherited/{lang}/with_delegate"] = inherited.inherited_main_role_prompt("with_delegate")
            with patch.object(prompts, "get_system_prompt", side_effect=RuntimeError("synthetic load failure")):
                output[f"inherited/{lang}/fallback"] = inherited.inherited_main_role_prompt()

            async def unused_query(_messages):
                raise AssertionError("cooperative snapshot must not submit work")

            for inline, mode in ((False, "json"), (True, "inline")):
                loop = cooperative.CooperativeProviderLoop(
                    None, unused_query, forbidden_io, provider="unused",
                    context_requirements={}, owns_runtime=False,
                    persona=lambda: prompts.get_system_prompt("base"),
                    work_proposals_only=inline,
                )
                output[f"cooperative/{lang}/{mode}/combined"] = loop.system
                output[f"cooperative/{lang}/presentation/combined"] = loop.presentation_system
                output[f"cooperative/{mode}/raw"] = cooperative._role_coordination_contract(inline)
            output["cooperative/presentation/raw"] = cooperative.PRESENTATION_CONTRACT

            inherited_prompt = inherited.inherited_main_role_prompt("base")
            output[f"auip/{lang}/narrator"] = narration._narrator_system_prompt(
                inherited_prompt, max_spoken_chars=120)
            output[f"auip/{lang}/narrator/json_transport"] = auip_llm._system_prompt_with_json_contract(
                output[f"auip/{lang}/narrator"])
            for required in (False, True):
                system = narration._structured_presenter_system_prompt(
                    inherited_prompt, max_spoken_chars=120,
                    presentation_required=required,
                    display_language="japanese" if lang == "ja" else "english")
                output[f"auip/{lang}/presenter/required={int(required)}"] = system
                output[f"auip/{lang}/presenter/required={int(required)}/json_transport"] = (
                    auip_llm._system_prompt_with_json_contract(system))

            async def capture_call(**kwargs):
                output[capture_key] = kwargs["system_prompt"]
                raise _CaptureComplete

            async def run_at_boundary(awaitable):
                try:
                    await awaitable
                except _CaptureComplete:
                    pass
                else:
                    raise AssertionError("expected one captured model boundary")

            candidate = AuipActionCandidate("candidate-1", "game.move", {"position": 3},
                                            "Choose position 3", 1, 1, "choice/v1")
            arguments = dict(context={"revision": 1, "state": {}, "available_actions": {
                "game.note": {"description": "Set a note.", "inputSchema": {
                    "type": "object", "properties": {"text": {"type": "string"}}}}}},
                candidates={candidate.candidate_id: candidate}, user_instruction="",
                branch_messages=[], trigger="automatic", speech_required=False)
            capture_key = f"auip/{lang}/b2/closed"
            with patch.object(b2, "call_auip_schema", capture_call):
                runner.run(run_at_boundary(b2.choose_b2_role_action(**arguments)))
            capture_key = f"auip/{lang}/b2/open"
            with patch.object(b2, "call_auip_tool", capture_call):
                runner.run(run_at_boundary(b2.choose_b2_open_role_action(
                    **arguments, uncovered_action_types=("game.note",))))
            capture_key = f"auip/{lang}/authorizer"
            with patch.object(authorizer, "call_auip_tool", capture_call):
                runner.run(run_at_boundary(authorizer.authorize_with_main_role({})))

            async def capture_narrator(payload):
                output[f"auip/{lang}/blocked_operator/fact_brief"] = payload["fact_brief"]
                output[f"auip/{lang}/blocked_operator/system"] = payload["system_prompt"]
                raise _CaptureComplete

            adapter = narration.AuipNarrationAdapter(
                runtime=SimpleNamespace(get=lambda _session: {
                    "operator_status": "error", "operator_error": "synthetic blocker",
                    "conversation_id": "synthetic-session", "revision": 2,
                    "app": {"id": "synthetic-app", "title": "Counter"}}),
                observer=unused_query, narrator=capture_narrator, sink=unused_query)
            runner.run(run_at_boundary(adapter._handle_operator_outcome(
                {"app_session_id": "synthetic-app-session"},
                {"status": "blocked", "outcome_id": "synthetic-outcome", "reason": "No exposed action."})))

            captured: dict = {}

            def capture_request(**kwargs):
                captured.update(kwargs)
                raise _CaptureComplete

            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=capture_request)))
            with (patch.object(browser, "_client", return_value=client),
                  patch.object(browser, "_provider", return_value="deepseek"),
                  patch.object(browser, "_model", return_value="snapshot-model")):
                try:
                    browser._decide_sync({})
                except _CaptureComplete:
                    output[f"browser/{lang}/system"] = captured["messages"][0]["content"]
            assert f"browser/{lang}/system" in output

        output["auip/narrator/raw"] = narration.AUIP_NARRATOR_SYSTEM_PROMPT
        output["auip/presenter/raw"] = auip_llm.AUIP_STRUCTURED_PRESENTATION_PROMPT
        output["auip/required_presenter/raw"] = auip_llm.AUIP_STRUCTURED_REQUIRED_PROMPT
        output["auip/b2/closed/raw"] = b2.B2_ROLE_ADDON
        output["auip/b2/open/raw"] = b2.B2_OPEN_ROLE_ADDON
        output["auip/authorizer/raw"] = authorizer.AUIP_ROLE_AUTHORIZER_SYSTEM_PROMPT
        output["auip/observer/raw"] = auip_llm.AUIP_OBSERVER_SYSTEM_PROMPT

        for repair in (False, True):
            captured = {}
            mode = "repair" if repair else "decision"
            note = {"summary": "Synthetic result: the counter supports reset.", "phase": "Result"}
            with (patch.object(observer, "_client", return_value=client),
                  patch.object(observer, "_model", return_value="snapshot-model")):
                try:
                    if repair:
                        observer._repair_terminal_report(client=client, provider="deepseek",
                            model="snapshot-model", note=note, display_language="english")
                    else:
                        observer._decide_sync(provider="deepseek", note=note, notes=[],
                            recent_chat=[], recent_spoken_updates=[], display_language="english")
                except _CaptureComplete:
                    payload = json.loads(captured["messages"][1]["content"])
                    output[f"work_observer/{mode}/system"] = captured["messages"][0]["content"]
                    output[f"work_observer/{mode}/schema"] = _json(payload["output_schema"])
                    output[f"work_observer/{mode}/payload"] = captured["messages"][1]["content"]
            assert f"work_observer/{mode}/system" in output

        async def capture_translation():
            async for _piece in vn_tts._stream_translate_zh_to_ja("先确认一下显示的事实。"):
                raise AssertionError("translation must stop before receiving model output")

        captured = {}
        with patch.object(vn_tts, "_get_client", return_value=client):
            try:
                runner.run(capture_translation())
            except _CaptureComplete:
                output["vn_tts/zh_to_ja/system"] = captured["messages"][0]["content"]
        assert "vn_tts/zh_to_ja/system" in output

        captured = {}
        with patch.object(openclaw, "_get_openclaw_client", return_value=client):
            try:
                runner.run(openclaw.ask_openclaw("Summarize the supplied synthetic facts."))
            except _CaptureComplete:
                output["openclaw/execution/system"] = captured["messages"][0]["content"]
        assert "openclaw/execution/system" in output

        output["provider_authoring/optional"] = provider_authoring.with_host_authoring_capabilities(
            "Build a local counter.", source_user_text="Let the main assistant play with me.",
            source_user_context='User: "Create a counter."',
            authoring_skill_path="/snapshot/workspace/.amadeus/skills/auip-authoring/SKILL.md")

        class SyntheticWindowsPath(PureWindowsPath):
            def resolve(self):
                return self

        # Required authoring includes host executable and resolved bundle paths.
        # Supply a synthetic Windows filesystem boundary instead of rewriting
        # any rendered text or depending on the machine running this test.
        with (patch.object(provider_authoring, "Path", SyntheticWindowsPath),
              patch.object(sys, "executable", "C:/snapshot/runtime/python.exe"),
              patch.object(sys, "platform", "win32")):
            for required_mode in ("", "observe", "collaborate", "delegate"):
                output[f"provider_authoring/required/{required_mode or 'unspecified'}"] = (
                    provider_authoring.with_host_authoring_capabilities(
                        "Build a local counter.", source_user_text="Let the main assistant play with me.",
                        require_auip_preparation=True, required_auip_mode=required_mode,
                        authoring_skill_path="C:/snapshot/workspace/.amadeus/skills/auip-authoring/SKILL.md"))
        output["provider_identity/role_reference"] = provider_identity.with_main_role_reference(
            "Research yourself, then write a short profile.",
            metadata={"main_role_name": inherited.MAIN_CONVERSATION_ROLE_NAME},
            execution_provider="codex")

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return {}

        class FakeSession(FakeResponse):
            def __init__(self, **_kwargs):
                pass

            def post(self, _url, *, json):
                output["llama/preheat/messages"] = _json(json["messages"])
                return FakeResponse()

        with (patch.object(llama_server, "should_manage_local_server", return_value=True),
              patch.object(llama_server.aiohttp, "ClientSession", FakeSession)):
            runner.run(llama_server.warmup_local_llm_cache())
        assert "llama/preheat/messages" in output

        observation = {"revision": 2, "state": {"winner": "black", "roleBindings": {
            "participant": "black", "user": "white"}}, "event": {
                "event_id": "synthetic-win", "type": "game.finished", "actor": "kurisu",
                "revision": 2, "payload": {"winner": "black"}, "terminal": True},
            "latest_verified_self_action": {"action_id": "synthetic-action", "type": "game.move",
                "payload": {"position": 3}, "accepted": True, "resulting_revision": 2,
                "effects": {"placed": 3}}}
        output["auip/structured_facts/correlated"] = _json(compile_auip_host_facts(observation))
        observation["latest_verified_self_action"]["resulting_revision"] = 1
        output["auip/structured_facts/uncorrelated"] = _json(compile_auip_host_facts(observation))

        # Freeze current messages without applying the older fixture's semantic
        # transport normalization (which deliberately permits a streaming change).
        vn_fixture = json.loads(VN_FIXTURE.read_text(encoding="utf-8"))
        for case in vn_fixture["cases"]:
            profile = VNProfile(**case["profile"])
            for lane in ("immediate", "lookahead", "reasoner", "summary", "retrospective"):
                output[f"vn/{profile.prompt_pack}/{lane}/messages"] = _json(
                    getattr(vn_prompts, lane + "_prompt")(profile, vn_fixture["context"]))
        output["vn/mystery/retrospective_bias"] = _json(mystery_policy._rule_retrospective_bias(
            {"recent_lines": [{"text": "复活秘术"}], "hypotheses": [
                {"id": "synthetic-hypothesis", "claim": "Current line may reveal a condition."}]},
            10, 40, lambda raw, *_args, **_kwargs: raw))

        # Capture Host-generated role-labelled VN input too, rather than only
        # the older fixture's hand-written context. Avoid runtime initialization
        # (which owns stores and providers); this pure builder reads these facts.
        vn_runtime = object.__new__(VNPlayerRuntime)
        vn_runtime.profile = VNProfile(session_id="synthetic-session", game_id="synthetic-vn",
                                       game_title="Synthetic VN", prompt_pack="base")
        vn_runtime._retrospective_window_lines = 40
        vn_runtime.store = SimpleNamespace(
            recent_reactions=lambda _limit: [], story_summary_log=lambda: [],
            scene_summary=lambda: {}, characters=lambda: {}, hypotheses=lambda: [],
            evidence_nodes=lambda: [], verifier_feedback=lambda: [], retrospective_bias=lambda: {})
        line = {"script_id": "synthetic-line", "text": "A displayed line."}
        pack = vn_runtime._build_retrospective_pack(line, {}, recent_lines=[line])
        output["vn/runtime/retrospective/messages"] = _json(vn_prompts.retrospective_prompt(vn_runtime.profile, pack))

    assert output and all(isinstance(value, str) for value in output.values())
    return output


def _isolated_capture() -> dict[str, str]:
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--capture"],
        cwd=ROOT, capture_output=True, encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def captured_prompts():
    return _isolated_capture()


def test_character_prompts_match_pre_migration_utf8_bytes(captured_prompts):
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))["prompts"]
    assert captured_prompts.keys() == expected.keys(), "snapshot coverage changed"
    for name, value in captured_prompts.items():
        assert value.encode("utf-8") == expected[name].encode("utf-8"), name


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--record", action="store_true", help="explicitly replace the committed baseline")
    args = parser.parse_args()
    if args.capture:
        sys.stdout.write(_json(_capture_prompts()))
    else:
        document = {"baseline": BASELINE, "inputs": {
            "providers": list(PROVIDERS), "coding_provider": "codex", "execution_provider": "openclaw",
            "character_override": "", "vn_inputs": "vn_prompt_messages.json"},
            "prompts": _isolated_capture()}
        FIXTURE.write_bytes((_json(document) + "\n").encode("utf-8"))
        print(f"Recorded {len(document['prompts'])} prompt snapshots in {FIXTURE.name}")
