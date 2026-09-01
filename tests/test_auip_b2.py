from __future__ import annotations

import asyncio
from unittest.mock import patch

from core.chat_runtime import ChatRuntime, _TurnState
from server.auip_action_candidates import (
    AuipActionCandidate,
    compile_auip_action_candidates,
)
from server.auip_b2 import AuipB2Coordinator, b2_runtime_unavailable_reason
from server.auip_b2_role_llm import (
    choose_b2_open_role_action,
    choose_b2_role_action,
)
from server.auip_contract import AuipProtocolError
from server.auip_control_decision import AuipControlDecision
from server.auip_engagement import AuipEngagementCoordinator
from server.auip_runtime import AuipRuntime
from server.event_bus import bus
from server.protocol import Method


def _manifest(*, include_open_action: bool = False) -> dict:
    manifest = {
        "schema": "amadeus.auip/v0",
        "app": {
            "id": "b2-board",
            "title": "B2 Board",
            "objective": "Place one stone.",
            "interactionSummary": "Choose one currently empty coordinate.",
        },
        "events": {
            "game.moved": {"beat": True, "participantOpportunity": True}
        },
        "actions": {
            "game.place": {
                "description": "Place one stone on an empty coordinate.",
                "risk": "local_execution",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                    },
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
                "preconditions": [
                    {
                        "kind": "action_available/v1",
                        "statePath": "actionAvailability",
                    },
                    {
                        "kind": "grid_cell_empty/v1",
                        "statePath": "board",
                        "xField": "x",
                        "yField": "y",
                    }
                ],
            },
            "game.mode": {
                "description": "Select a bounded mode.",
                "risk": "local_execution",
                "inputSchema": {
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                    "required": ["mode"],
                    "additionalProperties": False,
                },
            },
            "game.reset": {
                "description": "Reset.",
                "risk": "local_execution",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        "stances": ["spectator", "participant"],
        "situationKinds": ["action_availability/v1", "grid/v1"],
    }
    if include_open_action:
        manifest["actions"]["game.open"] = {
            "description": "An unbounded action that B2 cannot compile.",
            "risk": "local_execution",
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }
    return manifest


def _runtime(*, include_open_action: bool = False) -> tuple[AuipRuntime, dict]:
    runtime = AuipRuntime(role_branch_mode="b2")
    registered = runtime.register(
        manifest=_manifest(include_open_action=include_open_action),
        conversation_id="b2-chat",
    )
    runtime.set_engagement_mode(
        app_session_id=registered["app_session_id"],
        mode="collaborate",
    )
    runtime.publish_state(
        app_session_id=registered["app_session_id"],
        bridge_token=registered["bridge_token"],
        revision=1,
        state={
            "actionAvailability": {
                "kind": "action_availability/v1",
                "actionTypes": ["game.place"],
                "availableActionTypes": ["game.place"],
            },
            "board": {
                "kind": "grid/v1",
                "width": 2,
                "height": 2,
                "empty": ".",
                "legend": {"B": "black"},
                "rows": ["B.", ".."],
            }
        },
    )
    return runtime, registered


def test_b2_readiness_is_a_capability_fact_not_a_startup_requirement() -> None:
    assert b2_runtime_unavailable_reason(
        role_branch_mode="b2",
        control_decision_available=True,
        role_model_available=False,
    ) == "b2_role_model_unavailable"
    assert b2_runtime_unavailable_reason(
        role_branch_mode="b2",
        control_decision_available=False,
        role_model_available=True,
    ) == "b2_control_decision_unavailable"
    assert b2_runtime_unavailable_reason(
        role_branch_mode="b2",
        control_decision_available=True,
        role_model_available=True,
    ) == ""
    assert b2_runtime_unavailable_reason(
        role_branch_mode="off",
        control_decision_available=False,
        role_model_available=False,
    ) == ""


def test_candidate_compiler_keeps_exact_payloads_and_omits_open_schema() -> None:
    runtime, registered = _runtime(include_open_action=True)
    compilation = compile_auip_action_candidates(
        runtime,
        registered["app_session_id"],
    )
    candidates = list(compilation.candidates.values())

    grid_payloads = {
        tuple(sorted(item.payload.items()))
        for item in candidates
        if item.action_type == "game.place"
    }
    assert grid_payloads == {
        (("x", 1), ("y", 0)),
        (("x", 0), ("y", 1)),
        (("x", 1), ("y", 1)),
    }
    assert {
        item.payload["mode"]
        for item in candidates
        if item.action_type == "game.mode"
    } == {"a", "b"}
    assert any(
        item.action_type == "game.reset" and item.payload == {}
        for item in candidates
    )
    assert not any(item.action_type == "game.open" for item in candidates)
    assert compilation.uncovered_action_types == ("game.open",)
    assert compilation.complete is False
    assert all(item.revision == 1 for item in candidates)


def test_b2_yields_an_incomplete_candidate_space_before_role_choice() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime(include_open_action=True)
        sid = registered["app_session_id"]
        staged: list[tuple[str, object]] = []
        chooser_called = False

        class Decider:
            def capture(self, **_kwargs):
                async def resolve():
                    return AuipControlDecision(
                        status="ok",
                        action="step",
                        instruction="set the open value",
                        work_relation="subsumed",
                        app_session_id=sid,
                    )

                return resolve()

        async def choose(**_kwargs):
            nonlocal chooser_called
            chooser_called = True
            raise AssertionError("an incomplete candidate space must not reach the role")

        coordinator = AuipB2Coordinator(
            runtime=runtime,
            control_decider=Decider(),
            role_chooser=choose,
            stage_decision=lambda turn, decision: staged.append((turn, decision)),
            receipt_timeout_s=1,
        )
        routed = await coordinator.try_route_user_message(
            text="set a custom value",
            session_id="b2-chat",
            turn_id="turn-open",
        )

        assert routed is None
        assert chooser_called is False
        assert len(staged) == 1
        assert staged[0][0] == "turn-open"
        assert runtime.get(sid)["operator_status"] == "idle"

        automatic = await coordinator.execute_automatic_step(
            app_session_id=sid,
            instruction="use the assigned open action",
            trigger="collaborate_participant_opportunity",
        )
        assert automatic == {
            "status": "unavailable",
            "reason": "candidate_space_incomplete",
            "uncovered_action_types": ["game.open"],
        }
        assert chooser_called is False

    asyncio.run(scenario())


def test_b2_open_sidepath_holds_one_role_payload_until_accepted_receipt() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime(include_open_action=True)
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        staged: list[tuple[str, object]] = []
        requested: list[dict] = []
        requested_relations: list[str] = []

        class Decider:
            def capture(self, **_kwargs):
                async def resolve():
                    return AuipControlDecision(
                        status="ok",
                        action="step",
                        instruction="set a custom value",
                        work_relation="subsumed",
                        app_session_id=sid,
                    )

                return resolve()

        async def open_choose(**kwargs):
            assert kwargs["uncovered_action_types"] == ("game.open",)
            return {
                "action_type": "game.open",
                "payload": {"value": "custom"},
                "instruction_relation": "follows",
                "choice_reason": "The declared open action matches the request.",
                "speech": "その値にするわ。",
            }

        async def app_receipt(_method: str, payload: dict) -> None:
            action = dict(payload["action"])
            requested.append(action)
            requested_relations.append(
                str(payload.get("instruction_relation") or "")
            )
            resolved = runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=action["action_id"],
                accepted=True,
                resulting_revision=2,
                state={
                    "actionAvailability": {
                        "kind": "action_availability/v1",
                        "actionTypes": ["game.place"],
                        "availableActionTypes": ["game.place"],
                    },
                    "board": {
                        "kind": "grid/v1",
                        "width": 2,
                        "height": 2,
                        "empty": ".",
                        "legend": {"B": "black"},
                        "rows": ["B.", ".."],
                    },
                },
            )
            await bus.emit(Method.AUIP_UPDATED, resolved)

        bus.on(Method.AUIP_ACTION_REQUESTED, app_receipt)
        try:
            coordinator = AuipB2Coordinator(
                runtime=runtime,
                control_decider=Decider(),
                role_chooser=lambda **_kwargs: {},
                open_role_chooser=open_choose,
                open_payload_mode="candidate",
                stage_decision=lambda turn, decision: staged.append(
                    (turn, decision)
                ),
                receipt_timeout_s=1,
            )
            routed = await coordinator.try_route_user_message(
                text="set a custom value",
                session_id="b2-chat",
                turn_id="turn-open",
            )

            assert routed is not None
            assert routed["route_kind"] == "auip_b2_step"
            assert routed["decision_path"] == "b2"
            assert routed["selection_source"] == "open_schema_role"
            assert routed["instruction_relation"] == "follows"
            assert routed["display_text"] == "その値にするわ。"
            assert routed["receipt"]["accepted"] is True
            assert requested[0]["type"] == "game.open"
            assert requested[0]["payload"] == {"value": "custom"}
            assert requested[0]["proposal_id"].startswith("b2f:")
            assert requested_relations == ["follows"]
            assert routed["candidate_id"].startswith("open_")
            assert runtime.get(sid)["latest_delivered_narration"] is None
            await routed["delivery_observer"]({"visible": True})
            assert runtime.get(sid)["latest_delivered_narration"]["text"] == (
                "その値にするわ。"
            )
            assert staged == []
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, app_receipt)

    asyncio.run(scenario())


def test_dormant_open_action_does_not_disable_a_complete_candidate_space() -> None:
    manifest = {
        "schema": "amadeus.auip/v0",
        "app": {
            "id": "bounded-plus-dormant-open",
            "title": "Bounded Plus Dormant Open",
            "objective": "Choose a mode.",
            "interactionSummary": "Choose a mode while custom input is disabled.",
        },
        "events": {"game.changed": {"beat": True}},
        "actions": {
            "game.mode": {
                "description": "Choose a bounded mode.",
                "risk": "local_execution",
                "inputSchema": {
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
                    "required": ["mode"],
                    "additionalProperties": False,
                },
            },
            "game.open": {
                "description": "Supply custom input only when advertised.",
                "risk": "local_execution",
                "inputSchema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "preconditions": [
                    {
                        "kind": "action_available/v1",
                        "statePath": "availability",
                    }
                ],
            },
        },
        "stances": ["spectator", "participant"],
        "situationKinds": ["action_availability/v1"],
    }
    runtime = AuipRuntime(role_branch_mode="b2")
    registered = runtime.register(manifest=manifest, conversation_id="dormant-open")
    runtime.set_engagement_mode(
        app_session_id=registered["app_session_id"],
        mode="collaborate",
    )

    def publish(revision: int, available: list[str]) -> None:
        runtime.publish_state(
            app_session_id=registered["app_session_id"],
            bridge_token=registered["bridge_token"],
            revision=revision,
            state={
                "availability": {
                    "kind": "action_availability/v1",
                    "actionTypes": ["game.open"],
                    "availableActionTypes": available,
                }
            },
        )

    publish(1, [])
    dormant = compile_auip_action_candidates(runtime, registered["app_session_id"])
    assert dormant.complete is True
    assert {item.action_type for item in dormant.candidates.values()} == {"game.mode"}

    publish(2, ["game.open"])
    active = compile_auip_action_candidates(runtime, registered["app_session_id"])
    assert active.complete is False
    assert active.uncovered_action_types == ("game.open",)


def test_candidate_compiler_removes_an_unavailable_action_family_before_role_choice() -> None:
    runtime, registered = _runtime()
    runtime.publish_state(
        app_session_id=registered["app_session_id"],
        bridge_token=registered["bridge_token"],
        revision=2,
        state={
            "actionAvailability": {
                "kind": "action_availability/v1",
                "actionTypes": ["game.place"],
                "availableActionTypes": [],
            },
            "board": {
                "kind": "grid/v1",
                "width": 2,
                "height": 2,
                "empty": ".",
                "legend": {"B": "black"},
                "rows": ["B.", ".."],
            },
        },
    )

    compilation = compile_auip_action_candidates(
        runtime,
        registered["app_session_id"],
    )
    assert not any(
        candidate.action_type == "game.place"
        for candidate in compilation.candidates.values()
    )
    try:
        runtime.check_action_preconditions(
            app_session_id=registered["app_session_id"],
            type="game.place",
            payload={"x": 1, "y": 1},
            expected_revision=2,
        )
    except AuipProtocolError as exc:
        assert exc.code == "action_precondition_failed"
    else:
        raise AssertionError("unavailable action families must fail before role choice")


def test_b2_releases_one_line_only_after_accepted_receipt_and_visible_delivery() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        staged: list[tuple[str, object]] = []

        class Decider:
            def capture(self, **_kwargs):
                async def resolve():
                    return AuipControlDecision(
                        status="ok",
                        action="step",
                        instruction="右下に置いて",
                        work_relation="subsumed",
                        app_session_id=sid,
                    )

                return resolve()

        async def choose(**kwargs):
            candidate = next(
                item
                for item in kwargs["candidates"].values()
                if item.action_type == "game.place"
                and item.payload == {"x": 1, "y": 1}
            )
            return {
                "candidate_id": candidate.candidate_id,
                "instruction_relation": "follows",
                "choice_reason": "the requested point is empty",
                "speech": "右下の(1,1)に置いたわ。",
            }

        async def app_receipt(_method: str, payload: dict) -> None:
            action = payload["action"]
            resolved = runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=action["action_id"],
                accepted=True,
                resulting_revision=2,
                state={
                    "actionAvailability": {
                        "kind": "action_availability/v1",
                        "actionTypes": ["game.place"],
                        "availableActionTypes": ["game.place"],
                    },
                    "board": {
                        "kind": "grid/v1",
                        "width": 2,
                        "height": 2,
                        "empty": ".",
                        "legend": {"B": "black"},
                        "rows": ["B.", ".B"],
                    },
                },
            )
            await bus.emit(Method.AUIP_UPDATED, resolved)

        bus.on(Method.AUIP_ACTION_REQUESTED, app_receipt)
        try:
            coordinator = AuipB2Coordinator(
                runtime=runtime,
                control_decider=Decider(),
                role_chooser=choose,
                stage_decision=lambda turn, decision: staged.append((turn, decision)),
                receipt_timeout_s=1,
            )
            routed = await coordinator.try_route_user_message(
                text="你能下一手吗",
                session_id="b2-chat",
                turn_id="turn-b2",
            )
            assert routed is not None and routed["handled"] is True
            assert routed["route_kind"] == "auip_b2_step"
            assert routed["receipt"]["accepted"] is True
            assert routed["receipt"]["proposal_id"] == routed["proposal_id"]
            assert routed["proposal_id"].startswith("b2f:")
            assert routed["proposal_id"].endswith(routed["candidate_id"])
            before_delivery = runtime.get(sid)
            assert before_delivery["latest_delivered_narration"] is None
            branch = runtime.recent_role_branch_messages("b2-chat")
            assert branch is not None
            assert branch[0] == {"role": "user", "content": "你能下一手吗"}
            assert "Verified AUIP receipt" in branch[1]["content"]

            await routed["delivery_observer"]({"visible": True, "voice": {}})
            delivered = runtime.get(sid)["latest_delivered_narration"]
            assert delivered["text"] == "右下の(1,1)に置いたわ。"
            assert delivered["event_id"] == routed["action_id"]
            assert staged == []
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, app_receipt)

    asyncio.run(scenario())


def test_b2_controller_policy_rebinds_across_role_choice_latency() -> None:
    async def scenario() -> None:
        manifest = {
            "schema": "amadeus.auip/v0",
            "app": {
                "id": "b2-reactive-heat",
                "title": "B2 Reactive Heat",
                "objective": "Keep heat inside the safe range.",
                "interactionSummary": "Choose one declared control policy.",
            },
            "events": {"heat.changed": {"beat": True}},
            "actions": {
                "heat.set_policy": {
                    "description": "Keep heat inside the visible safe range.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "strategy": {
                                "type": "string",
                                "enum": ["maintain_safe_range"],
                            }
                        },
                        "required": ["strategy"],
                        "additionalProperties": False,
                    },
                }
            },
            "stances": ["spectator", "participant"],
            "situationKinds": ["controller/v1"],
            "controller": {
                "policyActions": ["heat.set_policy"],
                "leaseDurationMs": 30_000,
                "maxActionRateHz": 4,
                "takeover": "immediate",
            },
        }
        runtime = AuipRuntime(role_branch_mode="b2")
        registered = runtime.register(
            manifest=manifest,
            conversation_id="b2-controller-chat",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")

        def state(value: float, *, active: bool = False, revision: int | None = None) -> dict:
            return {
                "temperature": value,
                "controller": {
                    "kind": "controller/v1",
                    "status": "active" if active else "idle",
                    "policyRevision": revision,
                    "policyAction": "heat.set_policy" if active else None,
                    "policySummary": "Maintain safe range" if active else "",
                },
            }

        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state=state(90.0),
        )

        class Decider:
            def capture(self, **_kwargs):
                async def resolve():
                    return AuipControlDecision(
                        status="ok",
                        action="step",
                        instruction="安全範囲を維持して",
                        work_relation="subsumed",
                        app_session_id=sid,
                    )

                return resolve()

        async def choose(**kwargs):
            candidate = next(iter(kwargs["candidates"].values()))
            assert candidate.revision == 1
            # Sparse telemetry advanced while the role selected the same
            # Host-compiled policy; its payload and legality did not change.
            runtime.publish_state(
                app_session_id=sid,
                bridge_token=token,
                revision=2,
                state=state(91.0),
            )
            return {
                "candidate_id": candidate.candidate_id,
                "instruction_relation": "follows",
                "choice_reason": "the declared policy remains legal",
                "speech": "安全範囲を維持するように切り替えたわ。",
            }

        requested: list[dict] = []

        async def app_receipt(_method: str, payload: dict) -> None:
            action = payload["action"]
            requested.append(dict(action))
            resolved = runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=action["action_id"],
                accepted=True,
                resulting_revision=3,
                state=state(
                    89.0,
                    active=True,
                    revision=action["controller_lease"]["policy_revision"],
                ),
            )
            await bus.emit(Method.AUIP_UPDATED, resolved)

        bus.on(Method.AUIP_ACTION_REQUESTED, app_receipt)
        try:
            coordinator = AuipB2Coordinator(
                runtime=runtime,
                control_decider=Decider(),
                role_chooser=choose,
                stage_decision=lambda *_args: None,
                receipt_timeout_s=1,
            )
            routed = await coordinator.try_route_user_message(
                text="保持安全范围",
                session_id="b2-controller-chat",
                turn_id="turn-b2-controller",
            )

            assert routed is not None and routed["route_kind"] == "auip_b2_step"
            assert routed["receipt"]["accepted"] is True
            assert len(requested) == 1
            assert requested[0]["proposal_revision"] == 1
            assert requested[0]["expected_revision"] == 2
            assert requested[0]["payload"] == {"strategy": "maintain_safe_range"}
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, app_receipt)

    asyncio.run(scenario())


def test_b2_recompiles_once_when_the_app_phase_changes_before_invoke() -> None:
    async def scenario() -> None:
        manifest = {
            "schema": "amadeus.auip/v0",
            "app": {
                "id": "b2-phase-race",
                "title": "B2 Phase Race",
                "objective": "Stay alive and continue the run.",
                "interactionSummary": (
                    "During a run Kurisu may select attack mode. After defeat, "
                    "restart is the supported continuation."
                ),
            },
            "events": {
                "battle.controller_effect": {
                    "beat": True,
                    "importance": "important",
                    "controllerEffect": True,
                }
            },
            "actions": {
                "autopilot.set_policy": {
                    "description": "Select the sustained local battle policy.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string", "enum": ["attack"]}
                        },
                        "required": ["mode"],
                        "additionalProperties": False,
                    },
                },
                "run.restart": {
                    "description": "Restart after the current run has ended.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            },
            "stances": ["spectator", "participant"],
            "situationKinds": ["choice/v1", "controller/v1"],
            "controller": {
                "policyActions": ["autopilot.set_policy"],
                "leaseDurationMs": 30_000,
                "maxActionRateHz": 12,
                "takeover": "immediate",
            },
        }
        runtime = AuipRuntime(role_branch_mode="b2")
        registered = runtime.register(
            manifest=manifest,
            conversation_id="b2-phase-race-chat",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")

        def state(phase: str) -> dict:
            running = phase == "running"
            return {
                "phase": phase,
                "choices": {
                    "kind": "choice/v1",
                    "actionTypes": ["autopilot.set_policy", "run.restart"],
                    "options": [
                        {
                            "id": "attack",
                            "label": "Attack",
                            "action": "autopilot.set_policy",
                            "payload": {"mode": "attack"},
                            "available": running,
                        },
                        {
                            "id": "restart",
                            "label": "Restart",
                            "action": "run.restart",
                            "payload": {},
                            "available": not running,
                        },
                    ],
                },
                "controller": {
                    "kind": "controller/v1",
                    "status": "idle",
                    "policyRevision": None,
                    "policyAction": None,
                    "policySummary": "",
                },
            }

        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state=state("running"),
        )

        class Decider:
            def capture(self, **_kwargs):
                async def resolve():
                    return AuipControlDecision(
                        status="ok",
                        action="step",
                        instruction="继续跑，改成猛攻。",
                        work_relation="subsumed",
                        app_session_id=sid,
                    )

                return resolve()

        chooser_action_sets: list[set[str]] = []

        async def choose(**kwargs):
            action_types = {
                candidate.action_type for candidate in kwargs["candidates"].values()
            }
            chooser_action_sets.append(action_types)
            if len(chooser_action_sets) == 1:
                candidate = next(iter(kwargs["candidates"].values()))
                assert candidate.action_type == "autopilot.set_policy"
                # The run ends while the role is choosing. The first private
                # line must never reach the user or the application boundary.
                runtime.publish_state(
                    app_session_id=sid,
                    bridge_token=token,
                    revision=2,
                    state=state("gameover"),
                )
                return {
                    "candidate_id": candidate.candidate_id,
                    "instruction_relation": "follows",
                    "choice_reason": "attack was legal in the earlier snapshot",
                    "speech": "猛攻に切り替えるわ。",
                }
            candidate = next(iter(kwargs["candidates"].values()))
            assert candidate.action_type == "run.restart"
            return {
                "candidate_id": candidate.candidate_id,
                "instruction_relation": "safe_alternative",
                "choice_reason": "the run already ended, so restart is required",
                "speech": "もう倒れているから、まず再開するわ。",
            }

        requested: list[dict] = []

        async def app_receipt(_method: str, payload: dict) -> None:
            action = payload["action"]
            requested.append(dict(action))
            assert action["type"] == "run.restart"
            resolved = runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=action["action_id"],
                accepted=True,
                resulting_revision=3,
                state=state("running"),
            )
            await bus.emit(Method.AUIP_UPDATED, resolved)

        staged: list[tuple[str, object]] = []
        bus.on(Method.AUIP_ACTION_REQUESTED, app_receipt)
        try:
            coordinator = AuipB2Coordinator(
                runtime=runtime,
                control_decider=Decider(),
                role_chooser=choose,
                stage_decision=lambda turn, decision: staged.append((turn, decision)),
                receipt_timeout_s=1,
            )
            routed = await coordinator.try_route_user_message(
                text="继续跑，改成猛攻。",
                session_id="b2-phase-race-chat",
                turn_id="turn-phase-race",
            )

            assert routed is not None and routed["route_kind"] == "auip_b2_step"
            assert routed["display_text"] == "もう倒れているから、まず再開するわ。"
            assert routed["proposal_id"].startswith("b2f:r2:")
            assert chooser_action_sets == [
                {"autopilot.set_policy"},
                {"run.restart"},
            ]
            assert len(requested) == 1
            assert requested[0]["type"] == "run.restart"
            assert staged == []

            await routed["delivery_observer"]({"visible": True, "voice": {}})
            branch = runtime.recent_role_branch_messages("b2-phase-race-chat") or []
            assert [row for row in branch if row["role"] == "user"] == [
                {"role": "user", "content": "继续跑，改成猛攻。"}
            ]
            assert not any("猛攻に切り替える" in row["content"] for row in branch)
            assert runtime.get(sid)["latest_delivered_narration"]["text"] == (
                "もう倒れているから、まず再開するわ。"
            )
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, app_receipt)

    asyncio.run(scenario())


def test_b2_rejected_receipt_never_releases_the_private_line() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        sid = registered["app_session_id"]
        token = registered["bridge_token"]

        class Decider:
            def capture(self, **_kwargs):
                async def resolve():
                    return AuipControlDecision(
                        status="ok",
                        action="step",
                        instruction="下一手",
                        work_relation="subsumed",
                        app_session_id=sid,
                    )

                return resolve()

        async def choose(**kwargs):
            candidate = next(iter(kwargs["candidates"].values()))
            return {
                "candidate_id": candidate.candidate_id,
                "instruction_relation": "follows",
                "choice_reason": "bounded test choice",
                "speech": "ここに置いたわ。",
            }

        async def reject(_method: str, payload: dict) -> None:
            action = payload["action"]
            resolved = runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=action["action_id"],
                accepted=False,
                resulting_revision=1,
                reason="application rejected the action",
            )
            await bus.emit(Method.AUIP_UPDATED, resolved)

        bus.on(Method.AUIP_ACTION_REQUESTED, reject)
        try:
            coordinator = AuipB2Coordinator(
                runtime=runtime,
                control_decider=Decider(),
                role_chooser=choose,
                stage_decision=lambda *_args: None,
                receipt_timeout_s=1,
            )
            routed = await coordinator.try_route_user_message(
                text="下一手",
                session_id="b2-chat",
                turn_id="turn-rejected",
            )
            assert routed is not None
            assert routed["route_kind"] == "auip_b2_blocked"
            assert routed["display_text"] == ""
            assert "delivery_observer" not in routed
            assert runtime.get(sid)["latest_delivered_narration"] is None
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, reject)

    asyncio.run(scenario())


def test_b2_stages_non_step_decision_for_the_ordinary_chat_path() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        staged: list[tuple[str, object]] = []

        class Decider:
            def capture(self, **_kwargs):
                async def resolve():
                    return AuipControlDecision(
                        status="ok",
                        action="none",
                        work_relation="independent",
                    )

                return resolve()

        coordinator = AuipB2Coordinator(
            runtime=runtime,
            control_decider=Decider(),
            role_chooser=lambda **_kwargs: {},
            stage_decision=lambda turn, decision: staged.append((turn, decision)),
        )
        routed = await coordinator.try_route_user_message(
            text="帮我查一下 Paxos 论文",
            session_id="b2-chat",
            turn_id="turn-work",
        )
        assert routed is None
        assert staged == [("turn-work", staged[0][1])]
        assert staged[0][1].work_relation == "independent"
        assert runtime.get(registered["app_session_id"])["pending_action"] is None

    asyncio.run(scenario())


def test_b2_bypasses_a_closed_appsession_without_calling_any_model_lane() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        runtime.host_leave(app_session_id=registered["app_session_id"])
        staged: list[tuple[str, object]] = []

        class Decider:
            def capture(self, **_kwargs):
                raise AssertionError("closed AppSession must bypass B2 decision")

        async def choose(**_kwargs):
            raise AssertionError("closed AppSession must bypass B2 role choice")

        coordinator = AuipB2Coordinator(
            runtime=runtime,
            control_decider=Decider(),
            role_chooser=choose,
            stage_decision=lambda turn, decision: staged.append((turn, decision)),
        )
        routed = await coordinator.try_route_user_message(
            text="聊点别的。",
            session_id="b2-chat",
            turn_id="turn-after-close",
        )

        assert routed is None
        assert staged == []

    asyncio.run(scenario())


def test_b2_role_schema_exposes_candidate_id_but_no_action_payload_authority() -> None:
    async def scenario() -> None:
        captured: dict = {}
        candidate = AuipActionCandidate(
            candidate_id="cand-fixed",
            action_type="game.place",
            payload={"x": 1, "y": 1},
            semantic_label="Coordinate (1,1)",
            revision=3,
            decision_generation=2,
            source="grid_cell_empty/v1",
        )

        async def fake_call(**kwargs):
            captured.update(kwargs)
            return {
                "candidate_id": "cand-fixed",
                "instruction_relation": "follows",
                "choice_reason": "the requested point is empty",
                "speech": "[EMO preset=serious_speaking dur=4s] そこに置いたわ。",
            }

        with (
            patch("server.auip_b2_role_llm.call_auip_schema", fake_call),
            patch("server.auip_b2_role_llm.settings.AUIP_ACTION_PROVIDER", "openai"),
            patch("server.auip_b2_role_llm.settings.AUIP_ACTION_MODEL", "gpt-5.6-terra"),
            patch("server.auip_b2_role_llm.settings.AUIP_ACTION_REASONING_EFFORT", "low"),
            patch("server.auip_b2_role_llm.settings.AUIP_ACTION_SERVICE_TIER", "fast"),
        ):
            result = await choose_b2_role_action(
                context={
                    "revision": 3,
                    "app": {"title": "Board"},
                    "state": {
                        "board": {
                            "kind": "grid/v1",
                            "width": 2,
                            "height": 2,
                            "empty": ".",
                            "rows": ["..", ".."],
                        }
                    },
                },
                candidates={candidate.candidate_id: candidate},
                user_instruction="放那里",
                branch_messages=[],
                trigger="explicit_step",
            )

        assert result["candidate_id"] == "cand-fixed"
        assert result["speech"] == "そこに置いたわ。"
        assert result["emotion"] == "serious_speaking"
        parameters = captured["schema"]
        assert set(parameters["properties"]) == {
            "candidate_id",
            "instruction_relation",
            "choice_reason",
            "speech",
        }
        assert "payload" not in parameters["properties"]
        assert "action_type" not in parameters["properties"]
        assert captured["service_tier"] == "fast"
        assert captured["reasoning_effort"] == "low"

    asyncio.run(scenario())


def test_automatic_b2_role_schema_owns_choice_but_not_presentation() -> None:
    async def scenario() -> None:
        captured: dict = {}
        candidate = AuipActionCandidate(
            candidate_id="cand-auto",
            action_type="game.place",
            payload={"x": 0, "y": 1},
            semantic_label="Coordinate (0,1)",
            revision=3,
            decision_generation=2,
            source="grid_cell_empty/v1",
        )

        async def fake_call(**kwargs):
            captured.update(kwargs)
            return {
                "candidate_id": "cand-auto",
                "choice_reason": "one legal continuation",
            }

        with patch("server.auip_b2_role_llm.call_auip_schema", fake_call):
            result = await choose_b2_role_action(
                context={
                    "revision": 3,
                    "app": {"title": "Board"},
                    "state": {
                        "board": {
                            "kind": "grid/v1",
                            "width": 2,
                            "height": 2,
                            "empty": ".",
                            "rows": ["..", ".."],
                        }
                    },
                },
                candidates={candidate.candidate_id: candidate},
                user_instruction="",
                branch_messages=[],
                trigger="participant_opportunity",
                speech_required=False,
            )

        assert result["candidate_id"] == "cand-auto"
        assert result["instruction_relation"] == "not_applicable"
        assert result["speech"] == ""
        assert "speech" not in captured["schema"]["properties"]
        assert "speech" not in captured["schema"]["required"]
        assert "instruction_relation" not in captured["schema"]["properties"]
        assert "instruction_relation" not in captured["schema"]["required"]
        assert "separate sparse cadence owner" in captured["payload"][
            "presentation_contract"
        ]

    asyncio.run(scenario())


def test_b2_open_role_can_choose_schema_bound_payload_or_locked_candidate() -> None:
    async def scenario() -> None:
        calls: list[dict] = []
        candidate = AuipActionCandidate(
            candidate_id="cand-reset",
            action_type="reactor.reset",
            payload={},
            semantic_label="Restart at 85 C and rising",
            revision=4,
            decision_generation=2,
            source="choice/v1",
        )
        context = {
            "revision": 4,
            "decision_generation": 2,
            "app": {
                "title": "Reactor",
                "objective": "Return to the safe 45-55 C range.",
            },
            "state": {"temperature": 85, "trend": "rising"},
            "available_actions": {
                "reactor.set_policy": {
                    "description": "Regulate the current run without resetting.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "target": {
                                "type": "number",
                                "minimum": 45,
                                "maximum": 55,
                            }
                        },
                        "required": ["target"],
                        "additionalProperties": False,
                    },
                },
                "reactor.reset": {
                    "description": "Restart at 85 C and rising.",
                },
            },
        }

        async def fake_call(**kwargs):
            calls.append(kwargs)
            return "auip_b2_open_action_0", {
                "payload": {"target": 50},
                "instruction_relation": "follows",
                "choice_reason": "50 C is inside the safe range.",
                "speech": "[EMO preset=serious_speaking dur=4s] 50度に保つわ。",
            }

        with patch("server.auip_b2_role_llm.call_auip_tool", fake_call):
            open_result = await choose_b2_open_role_action(
                context=context,
                candidates={candidate.candidate_id: candidate},
                uncovered_action_types=("reactor.set_policy",),
                user_instruction="稳住它",
                branch_messages=[],
                trigger="explicit_step",
            )

        assert open_result == {
            "action_type": "reactor.set_policy",
            "payload": {"target": 50},
            "instruction_relation": "follows",
            "choice_reason": "50 C is inside the safe range.",
            "speech": "50度に保つわ。",
            "emotion": "serious_speaking",
        }
        tools = calls[0]["tools"]
        assert [tool["function"]["name"] for tool in tools] == [
            "auip_b2_select_candidate",
            "auip_b2_open_action_0",
        ]
        open_schema = tools[1]["function"]["parameters"]
        assert open_schema["properties"]["payload"] == context[
            "available_actions"
        ]["reactor.set_policy"]["inputSchema"]

        async def choose_locked(**_kwargs):
            return "auip_b2_select_candidate", {
                "candidate_id": "cand-reset",
                "instruction_relation": "follows",
                "choice_reason": "The user explicitly requested a restart.",
                "speech": "再開するわ。",
            }

        with patch("server.auip_b2_role_llm.call_auip_tool", choose_locked):
            locked_result = await choose_b2_open_role_action(
                context=context,
                candidates={candidate.candidate_id: candidate},
                uncovered_action_types=("reactor.set_policy",),
                user_instruction="重开",
                branch_messages=[],
                trigger="explicit_step",
            )

        assert locked_result["candidate_id"] == "cand-reset"
        assert "payload" not in locked_result

    asyncio.run(scenario())


def test_staged_leave_reuses_the_original_immediate_dispatch_semantics() -> None:
    async def scenario() -> None:
        dispatched: list[dict] = []

        async def route(attrs, **_kwargs):
            dispatched.append(dict(attrs))

        chat = ChatRuntime()
        chat.configure(auip_control_callback=route)
        chat.stage_auip_decision(
            "turn-leave",
            AuipControlDecision(
                status="ok",
                action="leave",
                work_relation="subsumed",
                app_session_id="app-b2",
            ),
        )
        state = _TurnState(
            gui_callback=None,
            turn_id="turn-leave",
            question="关掉游戏",
            session_id="chat-b2",
        )
        assert chat._start_auip_decision(state) is True
        assert state.auip_decision_result is not None
        assert state.auip_decision_result.action == "leave"
        assert state.auip_decision_ready.is_set() is True
        assert dispatched == []
        await state.auip_decision_task

        assert dispatched == [
            {
                "action": "leave",
                "_host_app_session_id": "app-b2",
            }
        ]
        assert state.auip_decision_dispatched is True

    asyncio.run(scenario())


def test_automatic_b2_opportunity_uses_same_candidate_owner_then_yields_presentation() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        chooser_calls: list[dict] = []

        async def choose(**kwargs):
            chooser_calls.append(dict(kwargs))
            if len(chooser_calls) == 1:
                raise AuipProtocolError("b2_role_decision_unavailable")
            candidate = next(
                item
                for item in kwargs["candidates"].values()
                if item.action_type == "game.place"
                and item.payload == {"x": 1, "y": 0}
            )
            return {
                "candidate_id": candidate.candidate_id,
                "instruction_relation": "not_applicable",
                "choice_reason": "the opportunity assigns one move",
            }

        async def app_receipt(_method: str, payload: dict) -> None:
            action = payload["action"]
            resolved = runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=action["action_id"],
                accepted=True,
                resulting_revision=2,
                state={
                    "actionAvailability": {
                        "kind": "action_availability/v1",
                        "actionTypes": ["game.place"],
                        "availableActionTypes": ["game.place"],
                    },
                    "board": {
                        "kind": "grid/v1",
                        "width": 2,
                        "height": 2,
                        "empty": ".",
                        "legend": {"B": "black"},
                        "rows": ["BB", ".."],
                    },
                },
            )
            await bus.emit(Method.AUIP_UPDATED, resolved)

        bus.on(Method.AUIP_ACTION_REQUESTED, app_receipt)
        try:
            coordinator = AuipB2Coordinator(
                runtime=runtime,
                control_decider=object(),
                role_chooser=choose,
                stage_decision=lambda *_args: None,
                receipt_timeout_s=1,
            )
            result = await coordinator.execute_automatic_step(
                app_session_id=sid,
                instruction="Use the accepted participant opportunity.",
                trigger="collaborate_participant_opportunity",
            )

            assert result["status"] == "accepted"
            assert result["presentation_owner"] == "verified_event_lane"
            assert result["proposal_id"].startswith("b2a:")
            assert "decision_context" not in result["receipt"]
            assert len(chooser_calls) == 2
            assert chooser_calls[0]["speech_required"] is False
            snapshot = runtime.get(sid)
            assert snapshot["latest_delivered_narration"] is None
            assert "decision_context" not in snapshot["latest_verified_self_action"]
            runtime.publish_event(
                app_session_id=sid,
                bridge_token=token,
                event_id="automatic-move-result",
                type="game.moved",
                actor="kurisu",
                revision=2,
                payload={"x": 1, "y": 0},
                caused_by_action_id=result["action_id"],
            )
            observation = runtime.narration_observation(
                app_session_id=sid,
                event_id="automatic-move-result",
            )
            assert observation["latest_verified_self_action"]["decision_context"] == {
                "kind": "automatic_role_choice",
                "reason": "the opportunity assigns one move",
                "instruction_relation": "not_applicable",
            }
            branch = runtime.recent_role_branch_messages("b2-chat")
            assert branch is not None
            assert any("Verified AUIP receipt" in row["content"] for row in branch)
            assert not any("次は(1,0)に置いたわ。" in row["content"] for row in branch)
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, app_receipt)

    asyncio.run(scenario())


def test_engagement_routes_automatic_opportunity_to_b2_without_split_participant() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        sid = registered["app_session_id"]
        calls: list[dict] = []

        class B2:
            async def execute_automatic_step(self, **kwargs):
                calls.append(dict(kwargs))
                return {"status": "accepted"}

        split_calls = 0

        async def split_controller(_context):
            nonlocal split_calls
            split_calls += 1
            return {"action": "wait", "private_note": "unused"}

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=split_controller,
            role_authorizer=lambda _context: {"decision": "approve"},
            b2_coordinator=B2(),
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=registered["bridge_token"],
            event_id="opportunity-1",
            type="game.moved",
            actor="user",
            revision=1,
            payload={"turn": "kurisu"},
        )
        await engagement.on_update(Method.AUIP_UPDATED, update)
        await engagement.wait_for_idle(sid)

        assert len(calls) == 1
        assert calls[0]["app_session_id"] == sid
        assert calls[0]["trigger"] == "collaborate_participant_opportunity"
        assert split_calls == 0
        await engagement.close()

    asyncio.run(scenario())


def test_engagement_uses_split_participant_when_b2_candidate_space_is_incomplete() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        b2_calls = 0
        split_calls = 0

        class B2:
            async def execute_automatic_step(self, **_kwargs):
                nonlocal b2_calls
                b2_calls += 1
                return {
                    "status": "unavailable",
                    "reason": "candidate_space_incomplete",
                }

        async def split_controller(_context):
            nonlocal split_calls
            split_calls += 1
            return {
                "action": "act",
                "type": "game.place",
                "payload": {"x": 1, "y": 1},
                "private_note": "the full lane supplied the exact payload",
            }

        async def app_receipt(_method: str, payload: dict) -> None:
            action = payload["action"]
            resolved = runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=action["action_id"],
                accepted=True,
                resulting_revision=2,
                state={
                    "actionAvailability": {
                        "kind": "action_availability/v1",
                        "actionTypes": ["game.place"],
                        "availableActionTypes": ["game.place"],
                    },
                    "board": {
                        "kind": "grid/v1",
                        "width": 2,
                        "height": 2,
                        "empty": ".",
                        "legend": {"B": "black"},
                        "rows": ["B.", ".B"],
                    },
                },
            )
            await bus.emit(Method.AUIP_UPDATED, resolved)

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=split_controller,
            role_authorizer=lambda _context: {"decision": "approve"},
            b2_coordinator=B2(),
        )
        bus.on(Method.AUIP_ACTION_REQUESTED, app_receipt)
        try:
            update = runtime.publish_event(
                app_session_id=sid,
                bridge_token=token,
                event_id="opportunity-incomplete",
                type="game.moved",
                actor="user",
                revision=1,
                payload={"turn": "kurisu"},
            )
            await engagement.on_update(Method.AUIP_UPDATED, update)
            await engagement.wait_for_idle(sid)

            assert b2_calls == 1
            assert split_calls == 1
            assert runtime.get(sid)["operator_error"] == ""
            assert runtime.get(sid)["latest_verified_self_action"]["accepted"] is True
        finally:
            bus.off(Method.AUIP_ACTION_REQUESTED, app_receipt)
            await engagement.close()

    asyncio.run(scenario())


def test_required_b2_failure_publishes_a_visible_operator_outcome() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        sid = registered["app_session_id"]
        updates: list[dict] = []

        class B2:
            async def execute_automatic_step(self, **_kwargs):
                return {
                    "status": "blocked",
                    "reason": "b2_role_decision_unavailable",
                }

        async def capture(_method: str, payload: dict) -> None:
            if payload.get("operator_outcome"):
                updates.append(dict(payload))

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=lambda _context: {"action": "wait"},
            role_authorizer=lambda _context: {"decision": "approve"},
            b2_coordinator=B2(),
        )
        bus.on(Method.AUIP_UPDATED, capture)
        try:
            update = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id="opportunity-blocked",
                type="game.moved",
                actor="user",
                revision=1,
                payload={"turn": "kurisu"},
            )
            await engagement.on_update(Method.AUIP_UPDATED, update)
            await engagement.wait_for_idle(sid)

            projection = runtime.get(sid)
            assert projection["operator_error"] == "b2_role_decision_unavailable"
            assert updates[-1]["operator_outcome"]["status"] == "blocked"
            assert "bounded retry" in updates[-1]["operator_outcome"]["reason"]
        finally:
            bus.off(Method.AUIP_UPDATED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_unconfigured_b2_blocks_the_action_without_split_fallback() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        sid = registered["app_session_id"]
        updates: list[dict] = []
        split_calls = 0

        async def split_controller(_context):
            nonlocal split_calls
            split_calls += 1
            return {"action": "wait"}

        async def capture(_method: str, payload: dict) -> None:
            if payload.get("operator_outcome"):
                updates.append(dict(payload))

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=split_controller,
            role_authorizer=lambda _context: {
                "decision": "approve",
                "reason": "unused",
            },
            b2_coordinator=None,
            b2_unavailable_reason="b2_role_model_unavailable",
        )
        bus.on(Method.AUIP_UPDATED, capture)
        try:
            scheduled = engagement.request_step(
                app_session_id=sid,
                instruction="Place one stone.",
                reason="explicit_step",
            )
            assert scheduled["scheduled"] is True
            await engagement.wait_for_idle(sid)

            projection = runtime.get(sid)
            assert split_calls == 0
            assert projection["operator_error"] == "b2_role_model_unavailable"
            assert updates[-1]["operator_outcome"]["status"] == "blocked"
            assert "not configured" in updates[-1]["operator_outcome"]["reason"]
        finally:
            bus.off(Method.AUIP_UPDATED, capture)
            await engagement.close()

    asyncio.run(scenario())


def test_unconfigured_b2_can_report_failure_without_any_model_lane() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        sid = registered["app_session_id"]
        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=None,
            role_authorizer=None,
            b2_coordinator=None,
            b2_unavailable_reason="b2_role_model_unavailable",
        )
        try:
            scheduled = engagement.request_step(
                app_session_id=sid,
                instruction="Place one stone.",
                reason="explicit_step",
            )
            assert scheduled["scheduled"] is True
            await engagement.wait_for_idle(sid)
            assert runtime.get(sid)["operator_error"] == "b2_role_model_unavailable"
        finally:
            await engagement.close()

    asyncio.run(scenario())


def test_required_b2_receipt_rejection_keeps_the_receipt_as_single_outcome_owner() -> None:
    async def scenario() -> None:
        runtime, registered = _runtime()
        sid = registered["app_session_id"]
        synthetic_outcomes: list[dict] = []

        class B2:
            async def execute_automatic_step(self, **_kwargs):
                return {
                    "status": "rejected",
                    "proposal_id": "proposal-rejected-by-app",
                    "reason": "application rejected the action",
                }

        async def capture(_method: str, payload: dict) -> None:
            if payload.get("operator_outcome"):
                synthetic_outcomes.append(dict(payload))

        engagement = AuipEngagementCoordinator(
            app_runtime=runtime,
            controller=lambda _context: {"action": "wait"},
            role_authorizer=lambda _context: {"decision": "approve"},
            b2_coordinator=B2(),
        )
        bus.on(Method.AUIP_UPDATED, capture)
        try:
            update = runtime.publish_event(
                app_session_id=sid,
                bridge_token=registered["bridge_token"],
                event_id="opportunity-rejected",
                type="game.moved",
                actor="user",
                revision=1,
                payload={"turn": "kurisu"},
            )
            await engagement.on_update(Method.AUIP_UPDATED, update)
            await engagement.wait_for_idle(sid)

            assert synthetic_outcomes == []
            assert runtime.get(sid)["operator_error"] == ""
        finally:
            bus.off(Method.AUIP_UPDATED, capture)
            await engagement.close()

    asyncio.run(scenario())
