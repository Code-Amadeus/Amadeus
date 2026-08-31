"""Browser immediate steering is latest-wins and safe at action boundaries."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_host.adapters.browser_branch import BrowserBranchAdapter
from agent_host.provider_contract import ProviderCapabilities, ProviderManifest
from agent_host.provider_identity import (
    PARENT_CONTEXT_DELIVERED_EVENT,
    PARENT_CONTEXT_DELIVERY_METADATA_KEY,
)
from agent_host.provider_runtime import ProviderRuntime
from agent_host.provider_types import (
    ProviderEvent,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderSteerRequest,
)
from server.event_bus import bus
from server.handlers.chat_handler import ChatHandler
from server.handlers.work_activity_handler import WorkActivityCoordinator
from server.interaction_branch import InteractionBranchCoordinator, InteractionBranchState
from server.protocol import Method
from server.provider_branch import ProviderBranchStore


class _SteerEngine:
    provider_id = "browser"
    engine_id = "mid-run-steer-test"

    def __init__(self) -> None:
        self.session_id = "browser_mid_run_steer"
        self.executed: list[str] = []
        self.first_action_started = asyncio.Event()
        self.release_first_action = asyncio.Event()

    async def run(self, request, _run_id, _emit) -> ProviderRunResult:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        action = str(metadata.get("browser_action") or request.mode or "observe")
        if action == "open":
            self.first_action_started.set()
            await self.release_first_action.wait()
        elif action == "click_ref":
            ref = str(metadata.get("ref") or "")
            self.executed.append(ref)
            if ref == "old_1":
                self.first_action_started.set()
                await self.release_first_action.wait()
        return ProviderRunResult(
            status="done",
            result="ok",
            metadata={
                "browser": {
                    "browser_session_id": self.session_id,
                    "current_url": f"https://example.test/{self.executed[-1] if self.executed else 'home'}",
                    "title": self.executed[-1] if self.executed else "Home",
                }
            },
        )

    async def inspect_session(self, _session_id: str, *, include_dom: bool = True) -> dict[str, Any]:
        current = self.executed[-1] if self.executed else "home"
        refs = [
            {
                "ref": ref,
                "kind": "button",
                "role": "button",
                "label": ref,
                "selector": f"#{ref}",
            }
            for ref in ("old_1", "old_2", "newest")
        ]
        return {
            "browser_session_id": self.session_id,
            "url": f"https://example.test/{current}",
            "title": current,
            "text": current,
            "dom": "<main>test</main>" if include_dom else "",
            "interaction_refs": refs,
        }

    async def cancel(self, _run_id: str) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def test_latest_steer_replans_after_current_atomic_action() -> None:
    async def run() -> None:
        engine = _SteerEngine()
        planner_instructions: list[str] = []
        events: list[dict[str, Any]] = []

        async def planner(context):
            instruction = str(context.get("latest_user_instruction") or "")
            planner_instructions.append(instruction)
            if instruction == "use the newest target":
                return {
                    "actions": [{"action": "click_ref", "ref": "newest"}],
                    "final_report": "Used the newest target.",
                }
            return {
                "actions": [
                    {"action": "click_ref", "ref": "old_1"},
                    {"action": "click_ref", "ref": "old_2"},
                ],
                "final_report": "Used both old targets.",
            }

        async def emit(event: ProviderEvent) -> None:
            events.append(event.to_dict())

        with tempfile.TemporaryDirectory(prefix="browser_mid_run_steer_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=engine,
                store=ProviderBranchStore(Path(root)),
                branch_planner=planner,
            )
            task = asyncio.create_task(
                adapter.run(
                    ProviderRunRequest(
                        provider="browser",
                        task="use the old targets",
                        mode="observe",
                        metadata={
                            "source": "llm_delegate",
                            "provider_branch": True,
                            "browser_action": "observe",
                            "browser_session_id": engine.session_id,
                            "branch_user_message": "use the old targets",
                            "max_branch_actions": 3,
                            "turn_id": "chat-turn-initial",
                            "source_user_text": "use the old targets",
                            "source_context_scope": "chat:chat-browser",
                            "source_context_mode": "snapshot",
                        },
                    ),
                    "browser_mid_run",
                    emit,
                )
            )
            await asyncio.wait_for(engine.first_action_started.wait(), timeout=2.0)
            first = await adapter.steer(
                "browser_mid_run",
                ProviderSteerRequest(
                    task="intermediate instruction",
                    revision=1,
                    metadata={"branch_user_message": "intermediate instruction"},
                ),
            )
            latest = await adapter.steer(
                "browser_mid_run",
                ProviderSteerRequest(
                    task="use the newest target",
                    revision=2,
                    metadata={
                        "branch_user_message": "use the newest target",
                        "turn_id": "chat-turn-steer",
                        "source_user_text": "use the newest target",
                        "source_context_scope": "chat:chat-browser",
                        "source_context_mode": "delta",
                    },
                ),
            )
            assert first["accepted"] is True
            assert latest["accepted"] is True
            engine.release_first_action.set()
            result = await asyncio.wait_for(task, timeout=3.0)

        assert engine.executed == ["old_1", "newest"]
        assert "old_2" not in engine.executed
        assert planner_instructions == ["use the old targets", "use the newest target"]
        applied = [
            item["payload"]["revision"]
            for item in events
            if item["type"] == "run.status"
            and item["payload"].get("stage") == "steer_applied"
        ]
        assert applied == [2]
        delivered = [
            item["metadata"]["turn_id"]
            for item in events
            if item["type"] == PARENT_CONTEXT_DELIVERED_EVENT
        ]
        assert delivered == ["chat-turn-initial", "chat-turn-steer"]
        assert result.metadata["steering"]["applied_revisions"] == [2]
        actions = result.metadata["provider_branch"]["actions"]
        assert [item["ref"] for item in actions] == ["old_1", "newest"]
        assert [item["instruction_revision"] for item in actions] == [0, 2]

    asyncio.run(run())


def test_direct_action_with_pending_steer_promotes_same_run_to_branch() -> None:
    async def run() -> None:
        engine = _SteerEngine()

        async def planner(context):
            assert context["latest_user_instruction"] == "use the newest target"
            return {
                "actions": [{"action": "click_ref", "ref": "newest"}],
                "final_report": "Used the newest target.",
            }

        async def emit(_event: ProviderEvent) -> None:
            return None

        with tempfile.TemporaryDirectory(prefix="browser_direct_promote_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=engine,
                store=ProviderBranchStore(Path(root)),
                branch_planner=planner,
            )
            task = asyncio.create_task(
                adapter.run(
                    ProviderRunRequest(
                        provider="browser",
                        task="open the home page",
                        mode="open",
                        metadata={
                            "source": "llm_delegate",
                            "browser_action": "open",
                            "url": "https://example.test/home",
                        },
                    ),
                    "browser_direct_promote",
                    emit,
                )
            )
            await asyncio.wait_for(engine.first_action_started.wait(), timeout=2.0)
            outcome = await adapter.steer(
                "browser_direct_promote",
                ProviderSteerRequest(
                    task="use the newest target",
                    revision=1,
                    metadata={"branch_user_message": "use the newest target"},
                ),
            )
            assert outcome["accepted"] is True
            engine.release_first_action.set()
            result = await asyncio.wait_for(task, timeout=3.0)

        assert engine.executed == ["newest"]
        assert result.metadata["steering"]["applied_revisions"] == [1]
        assert result.metadata["provider_branch"]["actions"][0]["ref"] == "newest"

    asyncio.run(run())


def test_steer_during_planning_discards_stale_plan_before_first_action() -> None:
    async def run() -> None:
        engine = _SteerEngine()
        planner_started = asyncio.Event()
        release_planner = asyncio.Event()
        calls = 0

        async def planner(context):
            nonlocal calls
            calls += 1
            if calls == 1:
                planner_started.set()
                await release_planner.wait()
                return {
                    "actions": [{"action": "click_ref", "ref": "old_1"}],
                    "final_report": "Used the old target.",
                }
            assert context["latest_user_instruction"] == "use the newest target"
            return {
                "actions": [{"action": "click_ref", "ref": "newest"}],
                "final_report": "Used the newest target.",
            }

        async def emit(_event: ProviderEvent) -> None:
            return None

        with tempfile.TemporaryDirectory(prefix="browser_planning_steer_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=engine,
                store=ProviderBranchStore(Path(root)),
                branch_planner=planner,
            )
            task = asyncio.create_task(
                adapter.run(
                    ProviderRunRequest(
                        provider="browser",
                        task="use the old target",
                        mode="observe",
                        metadata={
                            "source": "llm_delegate",
                            "provider_branch": True,
                            "browser_action": "observe",
                            "browser_session_id": engine.session_id,
                            "branch_user_message": "use the old target",
                        },
                    ),
                    "browser_planning_steer",
                    emit,
                )
            )
            await asyncio.wait_for(planner_started.wait(), timeout=2.0)
            outcome = await adapter.steer(
                "browser_planning_steer",
                ProviderSteerRequest(
                    task="use the newest target",
                    revision=1,
                    metadata={"branch_user_message": "use the newest target"},
                ),
            )
            assert outcome["accepted"] is True
            release_planner.set()
            result = await asyncio.wait_for(task, timeout=3.0)

        assert calls == 2
        assert engine.executed == ["newest"]
        assert result.metadata["steering"]["applied_revisions"] == [1]

    asyncio.run(run())


class _RuntimeSteerAdapter:
    provider_id = "steer-test"
    manifest = ProviderManifest(
        provider_id=provider_id,
        display_name="Steer test",
        capabilities=ProviderCapabilities(steering="immediate"),
    )

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.received: list[ProviderSteerRequest] = []
        self.emit = None

    async def run(self, _request, _run_id, emit) -> ProviderRunResult:
        self.emit = emit
        self.started.set()
        await self.release.wait()
        return ProviderRunResult(status="done", result="done")

    async def steer(self, _run_id: str, request: ProviderSteerRequest) -> dict[str, Any]:
        self.received.append(request)
        return {"accepted": True, "safe_boundary": "next_atomic_boundary"}

    async def cancel(self, _run_id: str) -> None:
        return None


def test_runtime_enforces_and_audits_immediate_steer() -> None:
    async def run() -> None:
        runtime = ProviderRuntime()
        adapter = _RuntimeSteerAdapter()
        runtime.register(adapter)
        record = await runtime.start(
            ProviderRunRequest(provider=adapter.provider_id, task="original")
        )
        await asyncio.wait_for(adapter.started.wait(), timeout=2.0)
        outcome = await runtime.steer(
            record.run_id,
            ProviderSteerRequest(
                task="replacement",
                revision=1,
                metadata={
                    "turn_id": "turn-2",
                    "source_user_text": "Apply the replacement.",
                    "source_user_context": 'Main Chat: "I will replace it."',
                    "source_context_mode": "delta",
                    "source_context_scope": "chat:session-1",
                },
            ),
        )
        assert outcome["accepted"] is True
        assert record.task == "original"
        assert adapter.received[0].task == "replacement"
        queued = [
            item
            for item in record.events
            if item["type"] == "run.status"
            and item["payload"].get("stage") == "steer_queued"
        ]
        assert queued and queued[-1]["payload"]["revision"] == 1
        assert record.metadata["steering"]["turn_id"] == "turn-2"
        assert PARENT_CONTEXT_DELIVERY_METADATA_KEY not in record.metadata
        assert "source_context_cursor_turn_id" not in record.metadata

        assert adapter.emit is not None
        await adapter.emit(
            ProviderEvent(
                provider=adapter.provider_id,
                run_id=record.run_id,
                type=PARENT_CONTEXT_DELIVERED_EVENT,
                metadata=dict(adapter.received[0].metadata),
            )
        )

        assert record.metadata["source_user_text"] == "Apply the replacement."
        assert record.metadata["source_user_context"] == (
            'Main Chat: "I will replace it."'
        )
        assert record.metadata["source_context_mode"] == "delta"
        assert record.metadata["source_context_cursor_turn_id"] == "turn-2"
        assert record.metadata[PARENT_CONTEXT_DELIVERY_METADATA_KEY][
            "source_scope"
        ] == "chat:session-1"
        adapter.release.set()
        await asyncio.wait_for(record.task_handle, timeout=2.0)

    asyncio.run(run())


def test_context_delivery_receipt_stays_out_of_public_run_and_result() -> None:
    async def run() -> None:
        runtime = ProviderRuntime()
        adapter = _RuntimeSteerAdapter()
        runtime.register(adapter)
        captured_events: list[dict[str, Any]] = []
        captured_results: list[dict[str, Any]] = []

        async def capture_event(_method: str, params: dict[str, Any]) -> None:
            if params.get("provider") == adapter.provider_id:
                captured_events.append(dict(params))

        async def capture_result(_method: str, params: dict[str, Any]) -> None:
            if params.get("provider") == adapter.provider_id:
                captured_results.append(dict(params))

        bus.on(Method.PROVIDER_EVENT, capture_event)
        bus.on(Method.PROVIDER_RESULT, capture_result)
        try:
            record = await runtime.start(
                ProviderRunRequest(provider=adapter.provider_id, task="original")
            )
            await asyncio.wait_for(adapter.started.wait(), timeout=2.0)
            public_updated_at = runtime.list_runs()[0]["updated_at"]
            assert adapter.emit is not None
            await adapter.emit(
                ProviderEvent(
                    provider=adapter.provider_id,
                    run_id=record.run_id,
                    type=PARENT_CONTEXT_DELIVERED_EVENT,
                    metadata={
                        "turn_id": "turn-public-projection",
                        "source_user_text": "Keep this receipt internal.",
                        "source_context_scope": "chat:session-public-projection",
                        "source_context_mode": "snapshot",
                    },
                )
            )
            assert PARENT_CONTEXT_DELIVERY_METADATA_KEY in record.metadata
            assert not any(
                event.get("type") == PARENT_CONTEXT_DELIVERED_EVENT
                for event in record.events
            )

            listed = runtime.list_runs()[0]
            assert listed["updated_at"] == public_updated_at
            assert PARENT_CONTEXT_DELIVERY_METADATA_KEY not in listed["metadata"]
            assert listed["metadata"].get("source_context_scope") is None
            assert listed["metadata"].get("source_context_cursor_turn_id") is None
            assert all(
                event.get("type") != PARENT_CONTEXT_DELIVERED_EVENT
                for event in listed["events"]
            )
            assert all(
                PARENT_CONTEXT_DELIVERY_METADATA_KEY
                not in (event.get("metadata") or {})
                for event in listed["events"]
            )

            adapter.release.set()
            await asyncio.wait_for(record.task_handle, timeout=2.0)
            terminal_event = next(
                event
                for event in reversed(captured_events)
                if event.get("type") == "run.finished"
            )
            assert PARENT_CONTEXT_DELIVERY_METADATA_KEY not in terminal_event["metadata"]
            assert terminal_event["metadata"].get("source_context_scope") is None
            assert captured_results
            public_result = captured_results[-1]
            assert PARENT_CONTEXT_DELIVERY_METADATA_KEY not in public_result["metadata"]
            assert all(
                event.get("type") != PARENT_CONTEXT_DELIVERED_EVENT
                for event in public_result["events"]
            )
            assert all(
                PARENT_CONTEXT_DELIVERY_METADATA_KEY
                not in (event.get("metadata") or {})
                for event in public_result["events"]
            )
        finally:
            bus.off(Method.PROVIDER_EVENT, capture_event)
            bus.off(Method.PROVIDER_RESULT, capture_result)

    asyncio.run(run())


def test_run_created_branch_steers_same_run_before_terminal_result() -> None:
    async def run() -> None:
        provider_runs: list[dict[str, Any]] = []
        steer_calls: list[dict[str, Any]] = []

        async def provider_run(params):
            provider_runs.append(params)
            return {"run": {"run_id": "unexpected_second_run", "status": "running"}}

        async def provider_steer(params):
            steer_calls.append(params)
            return {
                "accepted": True,
                "run": {
                    "run_id": params["run_id"],
                    "provider": "browser",
                    "status": "running",
                },
            }

        with tempfile.TemporaryDirectory(prefix="branch_preterminal_steer_") as root:
            coordinator = InteractionBranchCoordinator(
                provider_run=provider_run,
                provider_steer=provider_steer,
                root=root,
            )
            await coordinator._on_provider_event(
                "provider.event",
                {
                    "provider": "browser",
                    "run_id": "browser_active",
                    "type": "run.created",
                    "payload": {"task": "open the old page"},
                    "metadata": {
                        "source": "llm_delegate",
                        "session_id": "session-1",
                    },
                },
            )
            branch = coordinator.active_branch_for_session("session-1")
            assert branch is not None
            assert branch.active_run_id == "browser_active"
            assert branch.browser_session_id == ""

            current = await coordinator.continue_from_delegate(
                session_id="session-1",
                task="use the new instruction",
                turn_id="turn-2",
            )
            assert current is not None and current["run_id"] == "browser_active"
            assert provider_runs == []
            assert steer_calls[0]["run_id"] == "browser_active"
            assert steer_calls[0]["revision"] == 1
            assert (
                steer_calls[0]["metadata"]["branch_user_message"]
                == "use the new instruction"
            )
            assert steer_calls[0]["metadata"]["branch_instruction_revision"] == 1

    asyncio.run(run())


def test_chat_overlap_emits_only_latest_steered_turn() -> None:
    async def run() -> None:
        import agent_host.provider_runtime as runtime_module

        engine = _SteerEngine()
        planner_started = asyncio.Event()
        release_planner = asyncio.Event()
        planner_calls: list[str] = []

        async def planner(context):
            instruction = str(context.get("latest_user_instruction") or "")
            planner_calls.append(instruction)
            if len(planner_calls) == 1:
                planner_started.set()
                await release_planner.wait()
                return {
                    "actions": [{"action": "click_ref", "ref": "old_1"}],
                    "final_report": "Used the old target.",
                }
            assert "newest" in instruction
            return {
                "actions": [{"action": "click_ref", "ref": "newest"}],
                "final_report": "Used the newest target.",
            }

        with tempfile.TemporaryDirectory(prefix="chat_browser_steer_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=engine,
                store=ProviderBranchStore(Path(root) / "provider"),
                branch_planner=planner,
            )
            runtime = ProviderRuntime()
            runtime.register(adapter)
            record = await runtime.start(
                ProviderRunRequest(
                    provider="browser",
                    task="use the old target",
                    mode="observe",
                    metadata={
                        "source": "llm_delegate",
                        "provider_branch": True,
                        "browser_action": "observe",
                        "browser_session_id": engine.session_id,
                        "session_id": "chat-steer-session",
                        "interaction_branch_id": "branch-chat-steer",
                        "branch_user_message": "use the old target",
                    },
                )
            )
            await asyncio.wait_for(planner_started.wait(), timeout=2.0)

            provider_runs: list[dict[str, Any]] = []

            async def provider_run(params):
                provider_runs.append(params)
                raise AssertionError("an active Browser run must be steered, not duplicated")

            async def provider_steer(params):
                return await runtime.steer(
                    str(params.get("run_id") or ""),
                    ProviderSteerRequest(
                        task=str(params.get("task") or ""),
                        revision=int(params.get("revision") or 0),
                        metadata=dict(params.get("metadata") or {}),
                    ),
                )

            coordinator = InteractionBranchCoordinator(
                provider_run=provider_run,
                provider_steer=provider_steer,
                root=Path(root) / "interaction",
            )
            branch = InteractionBranchState(
                # The production run.created event owns branch identity. This
                # coordinator is attached after the held run began, so mirror it.
                branch_id=record.run_id,
                parent_session_id="chat-steer-session",
                provider="browser",
                status="active",
                goal="use a target on the current site",
                browser_session_id=engine.session_id,
                title="Home",
                url="https://example.test/home",
                active_run_id=record.run_id,
            )
            coordinator._active_by_session[branch.parent_session_id] = branch

            unexpected_llm_calls: list[str] = []

            async def unexpected_llm(text, **_kwargs):
                unexpected_llm_calls.append(str(text))
                raise AssertionError("same-site structural continuation must bypass main LLM")

            spoken: list[dict[str, Any]] = []

            async def voice_sink(payload):
                spoken.append(dict(payload))

            handler = ChatHandler()
            handler.configure(
                unexpected_llm,
                asyncio.Queue(),
                interaction_branch_router=coordinator.try_route_user_message,
                assistant_voice_sink=voice_sink,
            )
            next_epoch = 0

            def open_turn(**_kwargs):
                nonlocal next_epoch
                next_epoch += 1
                return {"chat_epoch": next_epoch}

            handler._open_turn = open_turn  # type: ignore[method-assign]
            saved_turns: list[dict[str, Any]] = []
            completed: list[dict[str, Any]] = []
            tokens: list[dict[str, Any]] = []

            async def capture_complete(_method, params):
                completed.append(dict(params))

            async def capture_token(_method, params):
                tokens.append(dict(params))

            async def visible(_turn_id: str) -> bool:
                return True

            bus.on(Method.CHAT_COMPLETE, capture_complete)
            bus.on(Method.CHAT_TOKEN, capture_token)
            try:
                with (
                    patch.object(runtime_module, "runtime", runtime),
                    patch.object(
                        ChatHandler,
                        "_turn_allows_visible_emit",
                        new=staticmethod(visible),
                    ),
                    patch.object(
                        ChatHandler,
                        "_save_direct_turn",
                        new=staticmethod(lambda **kwargs: saved_turns.append(dict(kwargs))),
                    ),
                    patch.object(
                        ChatHandler,
                        "_notify_coordinator_finished",
                        new=staticmethod(lambda *_args, **_kwargs: None),
                    ),
                    patch("core.session_manager.set_current_session_id"),
                    patch(
                        "core.chat_runtime.get_chat_runtime",
                        return_value=SimpleNamespace(enable_conversation=False),
                    ),
                ):
                    await handler.send_text(
                        "Open https://example.test/intermediate",
                        session_id="chat-steer-session",
                        turn_id="turn-1",
                    )
                    first_task = handler._stream_task
                    assert first_task is not None
                    await _wait_for_steer_revision(record, 1)

                    await handler.send_text(
                        "Open https://example.test/newest",
                        session_id="chat-steer-session",
                        turn_id="turn-2",
                    )
                    second_task = handler._stream_task
                    assert second_task is not None and second_task is not first_task
                    await _wait_for_steer_revision(record, 2)
                    release_planner.set()
                    gathered = await asyncio.wait_for(
                        asyncio.gather(
                            first_task,
                            second_task,
                            return_exceptions=True,
                        ),
                        timeout=4.0,
                    )
                    assert isinstance(gathered[0], asyncio.CancelledError)
                    await asyncio.sleep(0)
            finally:
                bus.off(Method.CHAT_COMPLETE, capture_complete)
                bus.off(Method.CHAT_TOKEN, capture_token)
                await adapter.shutdown()

        assert provider_runs == []
        assert unexpected_llm_calls == []
        assert engine.executed == ["newest"]
        assert record.metadata["steering"]["applied_revisions"] == [2]
        assert [item["turn_id"] for item in completed] == ["turn-2"]
        assert tokens and {item["turn_id"] for item in tokens} == {"turn-2"}
        assert len(spoken) == 1
        assert [item["turn_id"] for item in saved_turns] == ["turn-2"]
        assert handler._last_assistant_turn_id == "turn-2"

    asyncio.run(run())


def test_retarget_stops_remaining_plan_and_tombstones_old_result() -> None:
    async def run() -> None:
        engine = _SteerEngine()

        async def planner(_context):
            return {
                "actions": [
                    {"action": "click_ref", "ref": "old_1"},
                    {"action": "click_ref", "ref": "old_2"},
                ],
                "final_report": "Used both old targets.",
            }

        with tempfile.TemporaryDirectory(prefix="browser_retarget_stop_") as root:
            adapter = BrowserBranchAdapter(
                base_adapter=engine,
                store=ProviderBranchStore(Path(root) / "provider"),
                branch_planner=planner,
            )
            runtime = ProviderRuntime()
            runtime.register(adapter)
            record = await runtime.start(
                ProviderRunRequest(
                    provider="browser",
                    task="use both old targets",
                    mode="observe",
                    metadata={
                        "source": "llm_delegate",
                        "provider_branch": True,
                        "browser_action": "observe",
                        "browser_session_id": engine.session_id,
                        "session_id": "retarget-session",
                        "interaction_branch_id": "branch-retarget",
                        "branch_user_message": "use both old targets",
                    },
                )
            )
            await asyncio.wait_for(engine.first_action_started.wait(), timeout=2.0)

            async def provider_run(_params):
                raise AssertionError("retarget must return to main chat")

            async def provider_steer(params):
                return await runtime.steer(
                    str(params.get("run_id") or ""),
                    ProviderSteerRequest(
                        task=str(params.get("task") or ""),
                        revision=int(params.get("revision") or 0),
                        metadata=dict(params.get("metadata") or {}),
                    ),
                )

            coordinator = InteractionBranchCoordinator(
                provider_run=provider_run,
                provider_steer=provider_steer,
                root=Path(root) / "interaction",
            )
            branch = InteractionBranchState(
                branch_id="branch-retarget",
                parent_session_id="retarget-session",
                provider="browser",
                status="active",
                goal="use both old targets",
                browser_session_id=engine.session_id,
                title="Home",
                url="https://example.test/home",
                active_run_id=record.run_id,
            )
            coordinator._active_by_session[branch.parent_session_id] = branch

            routed = await coordinator.try_route_user_message(
                text="Open https://different.test/new-task",
                session_id="retarget-session",
                turn_id="retarget-turn",
            )
            assert routed is None
            assert coordinator.active_branch_for_session("retarget-session") is None
            await _wait_for_steer_revision(record, 1)
            engine.release_first_action.set()
            assert record.task_handle is not None
            await asyncio.wait_for(record.task_handle, timeout=3.0)

            assert engine.executed == ["old_1"]
            assert record.metadata["steering"]["applied_revisions"] == [1]
            assert record.metadata["browser"]["browser_session_id"] == engine.session_id
            assert coordinator._update_from_run(record.to_dict()) is None
            assert coordinator._update_from_run(record.to_dict()) is None
            assert coordinator.active_branch_for_session("retarget-session") is None
            await adapter.shutdown()

    asyncio.run(run())


def test_steer_progress_is_visible_but_silent_and_buttonless() -> None:
    async def run() -> None:
        activity = WorkActivityCoordinator()
        canvases: list[dict[str, Any]] = []
        notes: list[dict[str, Any]] = []

        async def capture_canvas(_method, params):
            if params.get("title") == "Browser instruction updated":
                canvases.append(dict(params))

        async def capture_note(_method, params):
            if params.get("title") == "Browser instruction updated":
                notes.append(dict(params))

        bus.on(Method.WALLPAPER_CANVAS, capture_canvas)
        bus.on(Method.CHAT_WORK_NOTE, capture_note)
        try:
            base = {
                "provider": "browser",
                "run_id": "browser-steer-ux",
                "type": "run.status",
                "metadata": {"session_id": "steer-ux-session"},
            }
            await activity._on_provider_event(
                Method.PROVIDER_EVENT,
                {
                    **base,
                    "payload": {
                        "status": "running",
                        "stage": "steer_queued",
                        "revision": 2,
                        "safe_boundary": "next_atomic_boundary",
                    },
                },
            )
            await activity._on_provider_event(
                Method.PROVIDER_EVENT,
                {
                    **base,
                    "payload": {
                        "status": "running",
                        "stage": "steer_applied",
                        "revision": 2,
                        "safe_boundary": "after_atomic_action",
                    },
                },
            )
        finally:
            await activity._leave_work("browser-steer-ux", reason="test_complete")
            bus.off(Method.WALLPAPER_CANVAS, capture_canvas)
            bus.off(Method.CHAT_WORK_NOTE, capture_note)

        assert len(canvases) == 2
        assert "received" in canvases[0]["lead"].lower()
        assert "active" in canvases[1]["lead"].lower()
        assert all(item.get("phase") == "Checkpoint" for item in canvases)
        assert all(not item.get("actions") for item in canvases)
        assert [item["metadata"]["steering_revision"] for item in canvases] == [2, 2]
        assert len(notes) == 2
        assert all(item.get("observer_policy") == "silent" for item in notes)
        assert all(item.get("speak") is False for item in notes)

        closed_canvases: list[dict[str, Any]] = []
        closed_notes: list[dict[str, Any]] = []

        async def capture_closed_canvas(_method, params):
            closed_canvases.append(dict(params))

        async def capture_closed_note(_method, params):
            closed_notes.append(dict(params))

        bus.on(Method.WALLPAPER_CANVAS, capture_closed_canvas)
        bus.on(Method.CHAT_WORK_NOTE, capture_closed_note)
        try:
            closing = {
                "provider": "browser",
                "run_id": "browser-close-ux",
                "metadata": {
                    "session_id": "steer-ux-session",
                    "branch_control": "supersede",
                },
            }
            await activity._on_provider_event(
                Method.PROVIDER_EVENT,
                {
                    **closing,
                    "type": "run.status",
                    "payload": {
                        "status": "running",
                        "stage": "steer_applied",
                        "revision": 3,
                        "safe_boundary": "after_atomic_action",
                    },
                },
            )
            await activity._on_provider_event(
                Method.PROVIDER_EVENT,
                {
                    **closing,
                    "type": "run.status",
                    "payload": {"status": "done"},
                },
            )
            await activity._on_provider_result(
                Method.PROVIDER_RESULT,
                {
                    **closing,
                    "status": "done",
                    "result": "Stopped the remaining browser plan.",
                },
            )
        finally:
            bus.off(Method.WALLPAPER_CANVAS, capture_closed_canvas)
            bus.off(Method.CHAT_WORK_NOTE, capture_closed_note)

        assert len(closed_canvases) == 1
        assert "session is still available" in closed_canvases[0]["lead"]
        assert closed_canvases[0].get("phase") == "Checkpoint"
        assert len(closed_notes) == 1
        assert closed_notes[0].get("observer_policy") == "silent"
        assert closed_notes[0].get("speak") is False

    asyncio.run(run())


async def _wait_for_steer_revision(record, revision: int) -> None:
    for _ in range(200):
        if any(
            item.get("type") == "run.status"
            and item.get("payload", {}).get("stage") == "steer_queued"
            and int(item.get("payload", {}).get("revision") or 0) == revision
            for item in record.events
        ):
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"steer revision {revision} was not queued")


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all browser mid-run steer tests passed")


if __name__ == "__main__":
    _main()
