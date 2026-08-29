"""T2 control proposals commit at transport completion, not role-turn end."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.chat_runtime import ChatRuntime, _TurnState
from llm.delegate_tool import ToolCallAccumulator
from server.control_proposal import seal_control_proposals


def _action(**attrs):
    return {"type": "DELEGATE", "attrs": attrs, "raw": "[DELEGATE ...]"}


def _call(index: int, name: str | None, arguments: str):
    return SimpleNamespace(
        index=index,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_snapshot_is_immutable_and_decision_view_hides_proposed_control() -> None:
    action = _action(
        provider="locus",
        intent="amend",
        project_id="role_guess",
        task="edit README",
    )
    batch = seal_control_proposals(
        [action],
        turn_id="turn-1",
        session_id="session-1",
        user_text="修改 README",
        transport="inline_tag",
    )
    action["attrs"]["intent"] = "execute"

    assert batch.commit_point == "delegate_tag_closed"
    assert batch.proposals[0]["intent"] == "amend"
    assert batch.decision_payloads() == ({"task": "edit README"},)
    try:
        batch.proposals[0]["intent"] = "report"
    except TypeError:
        pass
    else:
        raise AssertionError("shadow observer could mutate the dispatch snapshot")


def test_inline_snapshot_and_dispatch_happen_when_the_tag_closes() -> None:
    async def run() -> None:
        runtime = ChatRuntime()
        observed = []

        async def observe(batch) -> None:
            observed.append(batch)

        runtime._control_proposal_observer = observe
        st = _TurnState(
            gui_callback=None,
            turn_id="turn-inline",
            question="创建 hello.txt",
            session_id="session-inline",
        )
        with patch("core.chat_runtime.record_actions", return_value="dispatch-now") as record:
            cleaned = runtime._consume_stream_chunk(
                st,
                '先做这件事。[DELEGATE provider="locus" intent="execute" '
                'task="create hello.txt"]',
            )
            assert cleaned == "先做这件事。"
            record.assert_called_once()
            assert len(st.control_proposal_batches) == 1
            assert st.control_proposal_batches[0].commit_point == "delegate_tag_closed"
            # The role stream has not emitted an end event; the snapshot and
            # production dispatch already exist. The observer itself is async.
            assert observed == []
            await asyncio.sleep(0)
            assert len(observed) == 1

        runtime.configure(control_proposal_observer=None)
        assert runtime._control_proposal_observer is None

    asyncio.run(run())


def test_native_tool_snapshot_remains_stream_final() -> None:
    runtime = ChatRuntime()
    st = _TurnState(
        gui_callback=None,
        turn_id="turn-tool",
        question="创建 hello.txt",
        session_id="session-tool",
    )
    accumulator = ToolCallAccumulator()
    accumulator.feed(
        [_call(0, "delegate", '{"provider":"locus","task":"create hello.txt"}')]
    )
    assert st.control_proposal_batches == []
    with patch("core.chat_runtime.record_actions") as record:
        runtime._dispatch_tool_delegates(st, accumulator)
    record.assert_called_once()
    assert len(st.control_proposal_batches) == 1
    assert st.control_proposal_batches[0].commit_point == "response_stream_finished"


def test_compound_shadow_runs_beside_immediate_single_action_dispatch() -> None:
    async def run() -> None:
        baseline = []
        compound = []

        class Observer:
            def capture(self, batch):
                async def observe():
                    baseline.append(batch)

                return observe()

            def capture_compound_shadow(self, batch):
                async def observe():
                    compound.append(batch)

                return observe()

        runtime = ChatRuntime()
        runtime._control_proposal_observer = Observer()
        st = _TurnState(
            gui_callback=None,
            turn_id="turn-compound-shadow",
            question="创建 hello.txt",
            session_id="session-compound-shadow",
        )
        with patch("core.chat_runtime.record_actions", return_value="dispatch-now") as record:
            runtime._consume_stream_chunk(
                st,
                '[DELEGATE provider="locus" intent="execute" task="create hello.txt"]',
            )
            record.assert_called_once()
            assert baseline == []
            assert compound == []
            await asyncio.sleep(0)
            assert len(baseline) == 1
            assert len(compound) == 1
            assert baseline[0] is compound[0]

    asyncio.run(run())


def test_inline_transport_rejects_a_fake_multi_action_batch() -> None:
    try:
        seal_control_proposals(
            [_action(provider="locus", task="one"), _action(provider="browser", task="two")],
            turn_id="turn-many",
            session_id="session-many",
            user_text="do both",
            transport="inline_tag",
        )
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("inline transport pretended to support multiple DELEGATE tags")


if __name__ == "__main__":
    test_snapshot_is_immutable_and_decision_view_hides_proposed_control()
    print("ok: immutable proposal snapshot")
    test_inline_snapshot_and_dispatch_happen_when_the_tag_closes()
    print("ok: inline proposal commits before role-turn end")
    test_native_tool_snapshot_remains_stream_final()
    print("ok: native tool proposal commits at stream end")
    test_compound_shadow_runs_beside_immediate_single_action_dispatch()
    print("ok: compound shadow never delays immediate dispatch")
    test_inline_transport_rejects_a_fake_multi_action_batch()
    print("ok: inline transport does not pretend to batch")
