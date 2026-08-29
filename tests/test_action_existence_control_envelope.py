from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config.settings as settings
from core.chat_runtime import ChatRuntime, _TurnState
from llm.action_existence_protocol import (
    as_control_tag_text,
    decode_control_envelope,
)
from llm.stream_parser import StreamTagParser


def _state() -> _TurnState:
    return _TurnState(
        gui_callback=None,
        turn_id="turn_control",
        question="do the work",
        session_id="session_control",
    )


def test_default_parser_keeps_legacy_behavior() -> None:
    parser = StreamTagParser()
    cleaned, actions = parser.process_chunk(
        'before[CONTROL delegate="true" provider="locus" intent="execute" task="x"]after'
    )
    assert cleaned == "beforeafter"
    assert actions == []


def test_enabled_parser_emits_one_control_and_closes_the_control_stream() -> None:
    parser = StreamTagParser(control_envelope_enabled=True)
    cleaned, actions = parser.process_chunk(
        'before[CONTROL delegate="true" provider="locus" intent="execute" task="x"]after'
    )
    assert cleaned == "before"
    assert len(actions) == 1
    assert actions[0]["type"] == "CONTROL"
    assert actions[0]["attrs"]["delegate"] == "true"
    assert parser.process_chunk("later") == ("", [])


def test_decoder_has_only_one_executable_shape() -> None:
    true_result = decode_control_envelope(
        {
            "type": "CONTROL",
            "attrs": {
                "delegate": "true",
                "provider": "locus",
                "intent": "execute",
                "task": "create x",
            },
            "raw": "raw-control",
        }
    )
    false_result = decode_control_envelope(
        {"type": "CONTROL", "attrs": {"delegate": "false"}, "raw": "raw-false"}
    )
    invalid = decode_control_envelope(
        {
            "type": "CONTROL",
            "attrs": {"delegate": "false", "provider": "locus"},
        }
    )
    assert true_result.valid and true_result.action["type"] == "DELEGATE"
    assert true_result.action["attrs"]["provider"] == "locus"
    assert false_result.valid and false_result.delegate is False
    assert false_result.action is None
    assert not invalid.valid and invalid.action is None


def test_false_outcome_is_history_only_and_never_dispatches() -> None:
    runtime = ChatRuntime.__new__(ChatRuntime)
    with patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True):
        state = _state()
        with patch("core.chat_runtime.record_actions") as record:
            visible = runtime._consume_stream_chunk(
                state,
                '了解。[CONTROL delegate="false"]',
            )
    assert visible == "了解。"
    assert not record.called
    assert state.control_outcome_seen is True
    assert state.control_outcome_valid is True
    assert state.delegate_seen is False
    assert state.history_response.endswith('[CONTROL delegate="false"]')


def test_true_outcome_reuses_existing_delegate_dispatch() -> None:
    runtime = ChatRuntime.__new__(ChatRuntime)
    runtime._control_proposal_authority = False
    runtime._control_proposal_observer = None
    runtime._control_proposal_observer_tasks = set()
    with patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True):
        state = _state()
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                state,
                'やるわ。[CONTROL delegate="true" provider="locus" '
                'intent="execute" task="create x"]',
            )
    assert record.call_count == 1
    dispatched = record.call_args.args[0]
    assert len(dispatched) == 1
    assert dispatched[0]["type"] == "DELEGATE"
    assert dispatched[0]["attrs"]["intent"] == "execute"
    assert state.control_outcome_seen is True
    assert state.control_outcome_valid is True
    assert state.delegate_seen is True
    assert "[CONTROL" in state.history_response


def test_authority_history_uses_control_only_when_canary_is_on() -> None:
    action = {"attrs": {"provider": "locus", "intent": "focus"}}
    with patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", False):
        assert ChatRuntime._control_history_tag(action).startswith("[DELEGATE")
    with patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True):
        rendered = ChatRuntime._control_history_tag(action)
    assert rendered == as_control_tag_text(action["attrs"])
    assert rendered.startswith('[CONTROL delegate="true"')


def test_control_envelope_survives_authority_without_a_second_dispatch_path() -> None:
    import asyncio

    async def run() -> None:
        class Observer:
            def capture(self, _batch):
                async def accept():
                    return SimpleNamespace(
                        decision_status="ok",
                        outcome="agree",
                        canonical_actions=(
                            {
                                "provider": "locus",
                                "intent": "execute",
                                "task": "create x",
                            },
                        ),
                        notes=(),
                        reason="",
                    )

                return accept()

        runtime = ChatRuntime()
        runtime.configure(
            control_proposal_observer=Observer(),
            control_proposal_authority=True,
            control_proposal_authority_timeout_s=1.0,
        )
        with patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True):
            state = _state()
            dispatched = []
            with patch(
                "core.chat_runtime.record_actions",
                side_effect=lambda actions: dispatched.extend(actions),
            ):
                runtime._consume_stream_chunk(
                    state,
                    'やるわ。[CONTROL delegate="true" provider="locus" '
                    'intent="execute" task="create x"]',
                )
                assert dispatched == []
                await runtime._wait_for_control_authority(state)
        assert len(dispatched) == 1
        assert dispatched[0]["type"] == "DELEGATE"
        assert "[CONTROL" in state.history_response
        assert "[DELEGATE" not in state.history_response
        assert "\x00CONTROL_AUTHORITY" not in state.history_response

    asyncio.run(run())


def test_legacy_delegate_remains_accepted_when_canary_is_on() -> None:
    runtime = ChatRuntime.__new__(ChatRuntime)
    runtime._control_proposal_authority = False
    runtime._control_proposal_observer = None
    runtime._control_proposal_observer_tasks = set()
    with patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True):
        state = _state()
        with patch("core.chat_runtime.record_actions") as record:
            runtime._consume_stream_chunk(
                state,
                '[DELEGATE provider="locus" intent="execute" task="legacy"]',
            )
    assert record.call_count == 1
    assert state.control_outcome_seen is True
    assert state.control_outcome_valid is True


def test_missing_outcome_is_observed_but_never_repaired_here() -> None:
    with patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True):
        state = _state()
        with patch("core.chat_runtime.logger.warning") as warning:
            ChatRuntime._finalize_action_existence_outcome(state)
    assert warning.call_count == 1
    assert warning.call_args.args[0].startswith("[ACTION-EXISTENCE]")


def test_control_envelope_disables_legacy_omission_repair() -> None:
    import asyncio

    with (
        patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True),
        patch.object(settings, "LLM_DELEGATE_TOOL_CALLS", False),
        patch("core.chat_runtime._load_conversation_resolution_roster") as roster,
    ):
        repaired = asyncio.run(
            ChatRuntime._repair_missing_delegate(
                _state(),
                "Modify README.md now.",
                session_id="session_control",
            )
        )
    assert repaired is False
    assert not roster.called


def test_prompt_switch_is_reversible_and_uses_one_output_vocabulary() -> None:
    from llm.prompts import get_system_prompt

    with patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", False):
        baseline = get_system_prompt("with_delegate")
    with patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True):
        candidate = get_system_prompt("with_delegate")
        bedrock_candidate = get_system_prompt("bedrock")
    assert "[CONTROL OUTCOME CONTRACT]" not in baseline
    assert "[DELEGATE" in baseline
    for prompt in (candidate, bedrock_candidate):
        assert prompt.count("[CONTROL OUTCOME CONTRACT]") == 1
        assert '[CONTROL delegate="false"]' in prompt
        assert '[CONTROL delegate="true"' in prompt
        assert "DELEGATE" not in prompt
        assert 'intent="' in prompt


def test_explicit_control_override_preserves_auip_unresolved_legacy_contract() -> None:
    from llm.prompts import get_system_prompt

    with (
        patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True),
        patch.object(settings, "CONTROL_DECISION_AUTHORITY_ENABLED", True),
        patch.object(settings, "LLM_DELEGATE_TOOL_CALLS", False),
    ):
        candidate = get_system_prompt("with_delegate", control_envelope=True)
        unresolved = get_system_prompt("with_delegate", control_envelope=False)

    assert "DELEGATE" not in candidate
    assert "[CONTROL OUTCOME CONTRACT]" in candidate
    assert "[DELEGATE" in unresolved
    assert "[CONTROL OUTCOME CONTRACT]" not in unresolved


def test_live_prompt_moves_one_control_contract_after_dynamic_context() -> None:
    from types import SimpleNamespace

    from core.chat_runtime import _turn_system_prompt
    from llm.action_existence_protocol import control_envelope_prompt_addon

    state = SimpleNamespace(prompt_variant="")
    addon = control_envelope_prompt_addon(language="ja").strip()
    base = "必ず日本語で回答すること\n\n" + addon
    with (
        patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True),
        patch("core.chat_runtime._get_system_prompt", return_value=base),
        patch(
            "core.chat_runtime._with_active_provider_context",
            return_value=base + "\n\n[Current AUIP app experience]",
        ),
        patch(
            "core.chat_runtime._finalize_system_prompt_language",
            side_effect=lambda value: value,
        ),
    ):
        prompt = _turn_system_prompt(state, "with_delegate")

    assert prompt.count("[CONTROL OUTCOME CONTRACT]") == 1
    assert prompt.index("[Current AUIP app experience]") < prompt.rindex(
        "[CONTROL OUTCOME CONTRACT]"
    )
    assert prompt.endswith(addon)


def test_unresolved_auip_axis_removes_control_without_removing_delegate_contract() -> None:
    from llm.action_existence_protocol import control_envelope_prompt_addon

    state = SimpleNamespace(
        prompt_variant="",
        auip_decision_task=object(),
        auip_decision_result=SimpleNamespace(status="invalid"),
    )
    addon = control_envelope_prompt_addon(language="ja").strip()
    base = "必ず日本語で回答すること\n\nDELEGATE contract\n\n" + addon
    with (
        patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True),
        patch.object(settings, "CONTROL_DECISION_AUTHORITY_ENABLED", True),
        patch("core.chat_runtime._get_system_prompt", return_value=base),
        patch("core.chat_runtime._with_active_provider_context", return_value=base),
        patch(
            "core.chat_runtime._finalize_system_prompt_language",
            side_effect=lambda value: value,
        ),
    ):
        prompt = __import__(
            "core.chat_runtime", fromlist=["_turn_system_prompt"]
        )._turn_system_prompt(state, "with_delegate")

    assert "DELEGATE contract" in prompt
    assert "[CONTROL OUTCOME CONTRACT]" not in prompt


def test_control_envelope_requires_canonical_authority() -> None:
    from llm.action_existence_protocol import control_envelope_enabled

    with (
        patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True),
        patch.object(settings, "CONTROL_DECISION_AUTHORITY_ENABLED", False),
        patch.object(settings, "LLM_DELEGATE_TOOL_CALLS", False),
    ):
        assert control_envelope_enabled() is False


def test_native_tool_transport_takes_precedence_over_control_envelope() -> None:
    from llm.action_existence_protocol import control_envelope_enabled
    from llm.prompts import get_system_prompt

    with (
        patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True),
        patch.object(settings, "LLM_DELEGATE_TOOL_CALLS", True),
    ):
        assert control_envelope_enabled() is False
        assert "[CONTROL OUTCOME CONTRACT]" not in get_system_prompt("with_delegate")


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all action existence control-envelope tests passed")


if __name__ == "__main__":
    _main()
