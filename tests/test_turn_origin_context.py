"""Real Session-file interleavings at the Chat admission boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core import session_manager as sm
from core.chat_runtime import ChatRuntime, _TurnState
from core.turn_coordinator import TurnCoordinator
from server import app
from server.handlers.chat_handler import ChatHandler
from server.turn_admission import capture_turn_admission
from server.turn_decision_shadow import TurnDecisionShadowObserver


@pytest.fixture
def session_files(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sm, "_CURRENT_SESSION_ID", None)
    monkeypatch.setattr(sm.conversation_history, "dialog", [])
    for sid, marker in (("origin-A", "ONLY_A"), ("active-B", "ONLY_B")):
        sm.create_session(sid)
        sm.conversation_history.add_user(marker)
        sm.save_session(sid, enable_conversation=True)
    assert sm.load_session("origin-A")[0]

    def read(sid):
        return json.loads(Path(sm._session_path(sid)).read_text(encoding="utf-8"))

    return read


def test_chat_session_switch_stops_when_previous_history_cannot_be_saved(session_files):
    async def run():
        handler = ChatHandler()
        model = AsyncMock()
        handler.configure(stream_llm_query=model, pending_sentence_items=None)
        before = sm.conversation_history.snapshot()
        with (
            patch.object(sm, "save_session", return_value=False),
            patch.object(handler, "_open_turn", side_effect=AssertionError("no new admission")) as admission,
        ):
            with pytest.raises(RuntimeError, match="save the active Session"):
                await handler._handle_send({"text": "hello", "turn_id": "new", "session_id": "active-B"})
        admission.assert_not_called()
        model.assert_not_called()
        assert sm.get_current_session_id() == "origin-A"
        assert sm.conversation_history.dialog == before.dialog

    asyncio.run(run())


@pytest.mark.parametrize("switch_at", ["none", "visual", "model"])
def test_handler_turn_keeps_origin_across_session_switch(session_files, switch_at):
    async def run():
        before_a = session_files("origin-A")
        before_b = session_files("active-B")
        observer = TurnDecisionShadowObserver(enabled=True)
        runtime = ChatRuntime()
        queue = asyncio.Queue()
        runtime.configure(pending_sentence_items=queue, playback_manager=None, provider="local")
        runtime._ensure_clients = lambda _provider: None
        seen = []

        async def model(st, *_args):
            if switch_at == "model":
                assert sm.load_session("active-B")[0]
            history = st.history_snapshot
            seen.append({
                "session_id": st.session_id,
                "prior_messages": list(st.control_prior_messages),
                "messages": history.build_deepseek_messages("system", "current"),
                "admission": getattr(st, "turn_admission", None),
                "scope": st.interaction_branch_routing_lease,
            })
            st.full_response = "A reply."
            st.history_response = "A reply."

        runtime._run_local = model
        handler = ChatHandler()
        handler.configure(stream_llm_query=app._stream_llm_query_adapter, pending_sentence_items=queue)

        async def visual(**_kwargs):
            if switch_at == "visual":
                assert sm.load_session("active-B")[0]
            return None

        handler._prepare_visual_context = visual
        with (
            patch("server.turn_decision_shadow.observer", observer),
            patch("core.turn_coordinator.get_turn_coordinator", return_value=TurnCoordinator()),
            patch("core.chat_runtime.get_chat_runtime", return_value=runtime),
            patch("server.task_lookup.pre_turn_resolve", new=AsyncMock()),
            patch("core.chat_runtime._turn_has_live_auip_control_scope", return_value=False),
            patch.object(runtime, "_start_auip_decision", return_value=False),
            patch.object(handler, "_capture_interaction_branch_routing_lease", side_effect=lambda sid: {"state": "absent", "parent_session_id": sid}),
            patch.object(app, "pending_sentence_items", queue),
            patch.object(app, "playback_manager", None),
            patch("core.chat_runtime.reset_all_expressions"),
            patch("server.handlers.chat_handler.bus.emit", new=AsyncMock()),
            patch("core.chat_runtime.remote_llm_query", side_effect=AssertionError("no model calls")) as remote,
            patch("core.chat_runtime.local_llm_query", side_effect=AssertionError("no model calls")) as local,
        ):
            await handler._handle_send({
                "text": "hello from A", "turn_id": "origin-turn",
                "utterance_id": "origin-utterance", "session_id": "origin-A", "provider": "local",
            })
            await handler._stream_task
            remote.assert_not_called()
            local.assert_not_called()

        assert len(seen) == 1
        captured = seen[0]
        assert captured["session_id"] == "origin-A"
        assert captured["prior_messages"] == before_a["dialog"]
        assert captured["messages"] == [
            {"role": "system", "content": "system"},
            *before_a["dialog"],
            {"role": "user", "content": "current"},
        ]
        admission = captured["admission"]
        assert admission is not None and admission.session_id == "origin-A"
        assert admission.utterance_id == "origin-utterance"
        assert admission.chat_epoch == 1
        assert admission.root_id == observer.admission_for_turn("origin-turn").root_id
        assert captured["scope"]["parent_session_id"] == "origin-A"
        assert session_files("active-B")["dialog"] == before_b["dialog"]
        if switch_at == "none":
            assert session_files("origin-A")["dialog"] == [
                *before_a["dialog"],
                {"role": "user", "content": "hello from A"},
                {"role": "assistant", "content": "A reply.", "turn_id": "origin-turn"},
            ]
        else:
            assert session_files("origin-A")["dialog"] == before_a["dialog"]
            assert sm.conversation_history.dialog == before_b["dialog"]

    asyncio.run(run())


def _admission(**kwargs):
    return capture_turn_admission(**{
        "utterance_id": "source-utterance", "turn_id": "source-turn",
        "session_id": "origin-A", "transcript": "hello", "chat_epoch": 7,
        **kwargs,
    })


def test_capture_and_observation_keep_time_alias_and_epoch_distinct():
    evidence = {"n_best_hashes": ["first"], "asr_confidence": 0.7, "raw_audio": "omit"}
    with (
        patch("server.turn_admission.time.time", return_value=100.0),
        patch("server.turn_admission.time.monotonic", return_value=10.0),
    ):
        first = _admission(source_evidence=evidence)
    evidence["n_best_hashes"].append("later mutation")
    assert first.source_evidence == {"n_best_hashes": ["first"], "asr_confidence": 0.7}
    observer = TurnDecisionShadowObserver(enabled=True)
    with patch("server.turn_admission.time.time", return_value=200.0):
        observer.observe_admission(first)
    retry = _admission(turn_id="retry-turn", chat_epoch=8)
    canonical = observer.observe_admission(retry)
    assert retry.root_id == first.root_id == canonical.root_id
    assert (retry.turn_id, retry.chat_epoch) == ("retry-turn", 8)
    assert (canonical.turn_id, canonical.chat_epoch) == ("source-turn", 7)
    row = observer.snapshot()["recent"][0]
    assert row["admission"]["admitted_at"] == 100.0
    assert row["admission"]["admitted_at_monotonic"] == 10.0
    assert row["events"][0]["arrived_at"] == 200.0
    assert observer.admission_for_turn("retry-turn").root_id == first.root_id
    assert _admission(utterance_id="repeat-words").root_id != first.root_id


def test_disabled_shadow_does_not_remove_host_capture():
    with patch("server.turn_decision_shadow.get_enabled_turn_decision_shadow_observer", return_value=None):
        captured = ChatHandler._capture_turn_admission(
            utterance_id="voice-u", turn_id="voice-t", session_id="origin-A",
            text="yes", source="wake", chat_epoch=3, pending=True,
            source_evidence={"tts_overlap": True},
            utterance_identity_source="explicit_utterance_id",
        )
        ChatHandler._observe_turn_admission(captured)
    assert captured is not None and captured.chat_epoch == 3 and captured.pending
    assert captured.source_evidence == {
        "tts_overlap": True, "utterance_identity_source": "explicit_utterance_id",
    }
    assert captured.authority_mode == "source_witness_v1"
    assert capture_turn_admission(utterance_id="", turn_id="", session_id="", transcript="host note") is None


def test_history_snapshot_is_deep_and_save_cannot_retarget_loaded_history(session_files):
    before_a = session_files("origin-A")
    sm.conversation_history.dialog[0]["nested"] = {"values": ["original"]}
    snapshot = sm.conversation_history.snapshot()
    sm.conversation_history.dialog[0]["nested"]["values"].append("changed")
    assert snapshot.dialog[0]["nested"]["values"] == ["original"]
    assert sm.load_session("active-B")[0]
    sm.conversation_history.add_user("new B message")
    assert sm.save_session("origin-A", enable_conversation=True) is False
    assert session_files("origin-A") == before_a
    assert sm.save_session("active-B", enable_conversation=True) is True
    assert session_files("active-B")["dialog"][-1]["content"] == "new B message"


def test_headless_capture_precedes_audio_warmup_without_fabricating_epoch(session_files):
    async def run():
        observer = TurnDecisionShadowObserver(enabled=True)
        seen = []

        def initialize(_sample_rate):
            admission = observer.admission_for_turn("headless-turn")
            assert admission is not None and admission.chat_epoch is None
            assert admission.session_id == "origin-A"
            assert sm.load_session("active-B")[0]

        runtime = ChatRuntime()
        runtime.configure(
            pending_sentence_items=asyncio.Queue(), provider="local",
            playback_manager=SimpleNamespace(
                pending_audio={}, player_is_ready=asyncio.Event(), next_seq_to_play=1,
                player=SimpleNamespace(initialize=initialize),
            ),
        )
        runtime._ensure_clients = lambda _provider: None

        async def model(st, *_args):
            seen.append(st)

        runtime._run_local = model
        with (
            patch("server.turn_decision_shadow.observer", observer),
            patch.dict("os.environ", {"AMADEUS_E2E_NO_TTS": "0"}),
        ):
            await runtime.stream_llm_query("hello", turn_id="headless-turn", preserve_emotion=True, enable_conversation=True)
        assert len(seen) == 1
        assert seen[0].session_id == "origin-A"
        assert seen[0].history_snapshot.dialog == [{"role": "user", "content": "ONLY_A"}]
        assert seen[0].turn_admission.chat_epoch is None

    asyncio.run(run())


def test_admission_without_its_origin_history_cannot_read_another_session(session_files):
    async def run():
        admission = _admission()
        assert sm.load_session("active-B")[0]
        runtime = ChatRuntime()
        queue = asyncio.Queue()
        queue.put_nowait("preserve until valid admission")
        runtime.configure(pending_sentence_items=queue, provider="local")
        with pytest.raises(ValueError, match="originating history snapshot"):
            await runtime.stream_llm_query("hello", turn_admission=admission)
        assert queue.qsize() == 1
        with pytest.raises(ValueError, match="presentation turn"):
            await runtime.stream_llm_query("hello", turn_id="wrong-turn", turn_admission=admission)

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["local", "openai", "gemini", "bedrock", "hybrid3"])
def test_each_provider_builds_messages_from_the_same_turn_snapshot(session_files, provider):
    class PromptCaptured(Exception):
        pass

    async def run():
        runtime = ChatRuntime()
        snapshot = sm.conversation_history.snapshot()
        st = _TurnState(gui_callback=None, session_id="origin-A", history_snapshot=snapshot)
        assert sm.load_session("active-B")[0]
        observed = []
        original = getattr(sm.ConversationHistory, "build_gemini_full_prompt" if provider == "gemini" else "build_deepseek_messages")

        def build(history, *args, **kwargs):
            observed.append((history, original(history, *args, **kwargs)))
            raise PromptCaptured

        with (
            patch.object(sm.ConversationHistory, "build_gemini_full_prompt" if provider == "gemini" else "build_deepseek_messages", build),
            patch("core.chat_runtime._turn_system_prompt", return_value="system"),
            patch("core.chat_runtime._turn_role_grounding", return_value="facts"),
            patch("core.chat_runtime._turn_uses_conversation_history", return_value=True),
            patch("core.chat_runtime.RAG_ENABLED_FOR_LOCAL", False),
            pytest.raises(PromptCaptured),
        ):
            if provider == "local":
                await runtime._run_local(st, "current", None, True, provider)
            elif provider == "openai":
                await runtime._run_deepseek_openai(st, "current", None, True, provider)
            elif provider == "gemini":
                await runtime._run_gemini(st, "current", None, True)
            elif provider == "bedrock":
                await runtime._run_bedrock(st, "current", "current", "current", True)
            else:
                await runtime._run_hybrid(st, "current", "current", None, True, provider)
        assert len(observed) == 1 and observed[0][0] is snapshot
        rendered = json.dumps(observed[0][1], ensure_ascii=False)
        assert "ONLY_A" in rendered and "ONLY_B" not in rendered

    asyncio.run(run())


@pytest.mark.parametrize("switch", [False, True])
def test_bedrock_early_success_uses_the_shared_history_projection(session_files, switch):
    async def run():
        runtime = ChatRuntime()
        runtime.configure(pending_sentence_items=asyncio.Queue(), playback_manager=None)
        st = _TurnState(gui_callback=None, turn_id="bedrock-turn", session_id="origin-A")
        response = {"type": "content_block_delta", "delta": {"text": "A reply."}}
        client = SimpleNamespace(invoke_model_with_response_stream=lambda **_kwargs: {
            "body": [{"chunk": {"bytes": json.dumps(response).encode()}}],
        })

        async def consume(state, content, **_kwargs):
            state.full_response += content
            state.history_response += content

        async def control(_state):
            if switch:
                assert sm.load_session("active-B")[0]

        with (
            patch.dict("sys.modules", {"boto3": SimpleNamespace()}),
            patch("llm.client.bedrock_runtime_client", client, create=True),
            patch("core.chat_runtime.AWS_BEDROCK_AUTH_MODE", "boto3"),
            patch("core.chat_runtime._turn_system_prompt", return_value="system"),
            patch("core.chat_runtime._turn_role_grounding", return_value=""),
            patch.object(runtime, "_accept_role_stream_text", side_effect=consume),
            patch.object(runtime, "_wait_for_control_authority", side_effect=control),
        ):
            assert await runtime._run_bedrock(st, "hello", "hello", "hello", True)
        if switch:
            assert sm.conversation_history.dialog == [{"role": "user", "content": "ONLY_B"}]
            assert session_files("origin-A")["dialog"] == [{"role": "user", "content": "ONLY_A"}]
        else:
            assert sm.conversation_history.dialog == [
                {"role": "user", "content": "ONLY_A"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "A reply.", "turn_id": "bedrock-turn"},
            ]

    asyncio.run(run())


@pytest.mark.parametrize("path", ["proposal", "authority", "resend", "sentence", "auip_inline", "auip_decision"])
def test_control_outlets_retain_host_admission_separately_from_attrs(session_files, path):
    async def run():
        admission = _admission()
        state = _TurnState(
            gui_callback=None, session_id="origin-A", turn_id="source-turn", question="do it",
            turn_admission=admission, control_prior_messages=sm.conversation_history.dialog,
        )
        action = {"type": "DELEGATE", "attrs": {"intent": "execute", "task": "do it"}}
        runtime = ChatRuntime()
        runtime.configure(pending_sentence_items=asyncio.Queue())
        if path == "authority":
            async def decide():
                return SimpleNamespace(
                    decision_status="ok", outcome="agree", notes=(), reason="",
                    canonical_actions=({"intent": "execute", "task": "do it"},),
                )
            runtime.configure(control_proposal_observer=SimpleNamespace(capture=lambda _batch: decide()), control_proposal_authority=True)
        calls = []

        def record(actions, **kwargs):
            calls.append((actions, kwargs))

        runtime.configure(auip_control_callback=record)
        with (
            patch("core.chat_runtime.record_actions", side_effect=record),
            patch.object(ChatRuntime, "_ground_unique_active_amendment"),
            patch.object(ChatRuntime, "_ground_present_provider_delegate"),
        ):
            if path in {"proposal", "authority"}:
                runtime._record_delegate_proposals(state, [action], transport="inline_tag")
                await runtime._wait_for_control_authority(state)
            elif path == "resend":
                await runtime._dispatch_delegate_resend(state, [action], state.question, session_id=state.session_id)
            elif path == "sentence":
                await runtime._process_sentence(state, 'hello [DELEGATE intent="execute" task="do it"]', translation=False)
            elif path == "auip_inline":
                await runtime._schedule_auip_control(state, {"attrs": {"action": "leave"}})
            else:
                await runtime._dispatch_auip_attrs(state, {"action": "leave"})
        assert len(calls) == 1 and calls[0][1]["turn_admission"] is admission

    asyncio.run(run())


def test_real_delegate_boundary_uses_host_origin_not_model_attrs(session_files):
    from server.host_action_dispatcher import record_actions

    async def run():
        admission = _admission()
        assert sm.load_session("active-B")[0]
        seen = []

        async def stop_before_execution(_task, attrs, **_kwargs):
            seen.append(dict(attrs))
            return True

        with patch("server.app._consume_captured_interaction_branch_intent", side_effect=stop_before_execution):
            await record_actions(
                [{"type": "DELEGATE", "attrs": {
                    "task": "do it", "_host_turn_id": "forged-turn",
                    "_host_admitted_session_id": "active-B",
                    "_host_source_context_scope": "auip:child-A",
                }}],
                delegate_handler=app._handle_delegate, turn_admission=admission,
            )
        assert len(seen) == 1
        assert seen[0]["_host_admitted_session_id"] == "origin-A"
        assert seen[0]["_host_turn_id"] == "source-turn"
        assert seen[0]["_host_source_context_scope"] == "auip:child-A"
        assert admission.dialogue_source_scope == "chat:origin-A"

    asyncio.run(run())


@pytest.mark.parametrize("target", ["active-B", "new-C"])
def test_explicit_session_selection_loads_history_after_old_turn_interrupt(session_files, target):
    async def run():
        captured = []
        interrupted = []

        async def stream(_text, **kwargs):
            captured.append(kwargs)
            return ""

        async def interrupt():
            interrupted.append((sm.get_current_session_id(), sm.conversation_history.snapshot().dialog))

        handler = ChatHandler()
        handler.configure(stream_llm_query=stream, pending_sentence_items=None)
        handler._active_turn_id = "old-turn"
        handler._interrupt_superseded_turn = interrupt
        with (
            patch("core.turn_coordinator.get_turn_coordinator", return_value=TurnCoordinator()),
            patch("core.chat_runtime.get_chat_runtime", return_value=SimpleNamespace(enable_conversation=False)),
            patch("server.handlers.chat_handler.bus.emit", new=AsyncMock()),
            patch.object(handler, "_prepare_visual_context", new=AsyncMock(return_value=None)),
        ):
            await handler._handle_send({"text": "hello", "turn_id": "new-turn", "session_id": target})
            await handler._stream_task
        assert interrupted == [("origin-A", [{"role": "user", "content": "ONLY_A"}])]
        assert captured[0]["turn_admission"].session_id == target
        expected = [{"role": "user", "content": "ONLY_B"}] if target == "active-B" else []
        assert captured[0]["history_snapshot"].dialog == expected
        assert sm.get_current_session_id() == target
        assert session_files("origin-A")["dialog"] == [{"role": "user", "content": "ONLY_A"}]

    asyncio.run(run())


@pytest.mark.parametrize("provider", ["browser", "auip"])
def test_direct_branch_receives_same_host_admission_without_role_call(session_files, provider):
    async def run():
        captured = []

        async def router(**kwargs):
            captured.append(kwargs)
            return {"handled": True, "provider": provider, "speak": False, "save_history": False, "display_text": "done"}

        role = AsyncMock(side_effect=AssertionError("direct branch must not call role"))
        handler = ChatHandler()
        handler.configure(stream_llm_query=role, pending_sentence_items=None, interaction_branch_router=router)
        with (
            patch("core.turn_coordinator.get_turn_coordinator", return_value=TurnCoordinator()),
            patch("core.chat_runtime.get_chat_runtime", return_value=SimpleNamespace(enable_conversation=False)),
            patch("server.handlers.chat_handler.bus.emit", new=AsyncMock()),
        ):
            await handler._handle_send({"text": "continue", "turn_id": "direct-turn", "session_id": "origin-A"})
            await handler._stream_task
        role.assert_not_called()
        admission = captured[0]["turn_admission"]
        assert admission.session_id == captured[0]["session_id"] == "origin-A"
        assert admission.turn_id == captured[0]["turn_id"] == "direct-turn"
        assert admission.chat_epoch == 1

    asyncio.run(run())
