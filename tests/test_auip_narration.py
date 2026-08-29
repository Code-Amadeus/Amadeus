"""Simulated AUIP Observer/Narrator ownership and delivery receipt journey."""

from __future__ import annotations

import asyncio

from server.auip_contract import AUIP_SCHEMA
from server.auip_narration import AuipNarrationAdapter, AuipNarrationProfile
from server.auip_runtime import AuipRuntime


def _manifest() -> dict:
    return {
        "schema": AUIP_SCHEMA,
        "app": {
            "id": "board-game",
            "title": "Board Game",
            "version": "0.1.0",
            "interactionSummary": (
                "Take one legal board action. Examples: 'take center' maps to "
                "a legal center move; 'block it' maps to one defensive move."
            ),
        },
        "events": {
            "game.move": {"beat": True},
            "game.finished": {"beat": True, "importance": "important", "terminal": True},
        },
        "actions": {},
        "stances": ["spectator"],
    }


def test_auip_profile_observer_narrator_and_receipt_keep_their_owners() -> None:
    async def run() -> None:
        # This contract intentionally exercises the retained split
        # Observer -> Narrator rollback path. The promoted B2 default owns an
        # AppSession branch and therefore adds its collapsed capsule to Main
        # Chat context; that is a different presentation topology.
        runtime = AuipRuntime(role_branch_mode="off")
        registered = runtime.register(
            manifest=_manifest(),
            conversation_id="conversation-1",
        )
        app_session_id = registered["app_session_id"]
        token = registered["bridge_token"]
        observer_inputs: list[dict] = []
        narrator_inputs: list[dict] = []
        sink_payloads: list[dict] = []

        async def observer(payload: dict) -> dict:
            observer_inputs.append(payload)
            return {
                "action": "speak",
                "fact_brief": f"revision {payload['revision']} is strategically relevant",
                # Any prose here is deliberately ignored by the adapter.
                "display_text": "untrusted observer prose",
            }

        async def narrator(payload: dict) -> dict:
            narrator_inputs.append(payload)
            return {"display_text": "ここは少し面白い局面ね。", "emotion": "thinking"}

        async def sink(payload: dict) -> dict:
            sink_payloads.append(payload)
            return {"status": "queued", "sentence_id": f"sentence-{len(sink_payloads)}"}

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=observer,
            narrator=narrator,
            sink=sink,
            profile=AuipNarrationProfile(normal_beat_stride=2),
            recent_chat=lambda _session: [
                {"role": "user", "content": "この手はどう思う？"},
            ],
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL MAIN CHAT PERSONA",
        )

        runtime.publish_state(
            app_session_id=app_session_id,
            bridge_token=token,
            revision=1,
            state={"turn": "user", "moves": 1},
        )
        first = runtime.publish_event(
            app_session_id=app_session_id,
            bridge_token=token,
            event_id="move-1",
            type="game.move",
            actor="user",
            revision=1,
            payload={"x": 1, "y": 1},
        )
        filtered = await adapter.handle_update("auip.updated", first)
        assert filtered and filtered["status"] == "profile_filtered"
        assert observer_inputs == []

        runtime.publish_state(
            app_session_id=app_session_id,
            bridge_token=token,
            revision=2,
            state={"turn": "app", "moves": 2},
        )
        second = runtime.publish_event(
            app_session_id=app_session_id,
            bridge_token=token,
            event_id="move-2",
            type="game.move",
            actor="user",
            revision=2,
            payload={"x": 2, "y": 2},
        )
        spoken = await adapter.handle_update("auip.updated", second)
        assert spoken and spoken["status"] == "queued" and spoken["retained"] is True
        assert observer_inputs[0]["event"]["event_id"] == "move-2"
        checkpoint = observer_inputs[0]["conversation_checkpoint"]
        assert checkpoint["recent_messages"][0]["content"] == "この手はどう思う？"
        assert narrator_inputs[0]["fact_brief"] == "revision 2 is strategically relevant"
        assert "take center" in narrator_inputs[0]["app"]["interactionSummary"]
        assert "conversation_checkpoint" not in narrator_inputs[0]
        assert narrator_inputs[0]["recent_delivered_narrations"] == []
        assert narrator_inputs[0]["system_prompt"].startswith("CANONICAL MAIN CHAT PERSONA")
        assert "same assistant" in narrator_inputs[0]["system_prompt"]
        assert "no more than 96 Unicode characters" in narrator_inputs[0]["system_prompt"]
        assert "display_text" not in narrator_inputs[0]
        assert sink_payloads[0]["source"] == "auip_narrator"
        assert sink_payloads[0]["voice_text_ja"] == "ここは少し面白い局面ね。"
        assert runtime.get(app_session_id)["latest_delivered_narration"][
            "event_id"
        ] == "move-2"
        active_context = runtime.render_main_chat_context("conversation-1")
        assert "recent_delivered_narration=" in active_context
        assert "ここは少し面白い局面ね。" in active_context

        runtime.publish_state(
            app_session_id=app_session_id,
            bridge_token=token,
            revision=3,
            state={"winner": "user", "moves": 3},
        )
        terminal = runtime.publish_event(
            app_session_id=app_session_id,
            bridge_token=token,
            event_id="finish-1",
            type="game.finished",
            actor="app",
            revision=3,
            payload={"winner": "user"},
        )
        finished = await adapter.handle_update("auip.updated", terminal)
        assert finished and finished["status"] == "queued" and finished["retained"] is True
        context = runtime.render_main_chat_context("conversation-1")
        assert context.count("ここは少し面白い局面ね。") == 2
        assert "game.finished" in context
        assert narrator_inputs[1]["recent_delivered_narrations"] == [
            {"text": "ここは少し面白い局面ね。", "terminal": False}
        ]

        trailing = runtime.publish_event(
            app_session_id=app_session_id,
            bridge_token=token,
            event_id="move-after-finish",
            type="game.move",
            actor="app",
            revision=3,
            payload={"note": "same committed outcome"},
        )
        superseded = await adapter.handle_update("auip.updated", trailing)
        assert superseded and superseded["status"] == "superseded_by_terminal"
        assert len(sink_payloads) == 2

        duplicate = await adapter.handle_update("auip.updated", terminal)
        assert duplicate and duplicate["status"] == "duplicate"
        assert len(sink_payloads) == 2

    asyncio.run(run())


def test_profile_rejects_long_scene_prose_instead_of_cutting_a_sentence() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(manifest=_manifest(), conversation_id="conversation-long")
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        delivered: list[dict] = []
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"phase": "active"},
        )
        event = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="long-comment",
            type="game.move",
            actor="user",
            revision=1,
            payload={},
        )
        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda _payload: {"action": "speak", "fact_brief": "a routine change"},
            narrator=lambda _payload: {"display_text": "長" * 97},
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=1, max_spoken_chars=96),
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL MAIN CHAT PERSONA",
        )

        result = await adapter.handle_update("auip.updated", event)

        assert result and result["status"] == "narration_too_long"
        assert delivered == []
        assert runtime.focused_projection("conversation-long")[
            "recent_delivered_narrations"
        ] == []

    asyncio.run(run())


def test_verified_self_action_reaches_observer_without_forcing_commentary() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        manifest = _manifest()
        manifest["actions"] = {
            "game.move": {
                "description": "Place one legal move.",
                "risk": "local_execution",
                "inputSchema": {
                    "type": "object",
                    "properties": {"position": {"type": "integer"}},
                    "required": ["position"],
                    "additionalProperties": False,
                },
            }
        }
        manifest["stances"] = ["spectator", "participant"]
        registered = runtime.register(
            manifest=manifest,
            conversation_id="conversation-self-action",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"turn": "kurisu"},
        )
        requested = runtime.invoke_action(
            app_session_id=sid,
            actor="kurisu",
            type="game.move",
            payload={"position": 7},
            expected_revision=1,
        )
        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=token,
            action_id=requested["action"]["action_id"],
            accepted=True,
            resulting_revision=2,
            state={"turn": "user", "last_move": 7},
            effects={"placed": "center"},
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="verified-kurisu-move",
            type="game.move",
            actor="kurisu",
            revision=2,
            payload={"position": 7},
        )
        observed: list[dict] = []
        delivered: list[dict] = []
        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda payload: observed.append(payload)
            or {"action": "silent", "fact_brief": "Kurisu placed one move."},
            narrator=lambda _payload: {"display_text": "should not run"},
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=99),
        )

        result = await adapter.handle_update("auip.updated", update)

        assert result and result["status"] == "silent"
        assert [item["event"]["event_id"] for item in observed] == [
            "verified-kurisu-move"
        ]
        assert delivered == []

    asyncio.run(run())


def test_first_verified_controller_effect_uses_post_ledger_narrator_fast_lane() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        manifest = {
            "schema": AUIP_SCHEMA,
            "app": {"id": "reactive-drone", "title": "Reactive Drone"},
            "events": {
                "drone.controller_effect": {
                    "beat": True,
                    "importance": "important",
                    "controllerEffect": True,
                }
            },
            "actions": {
                "drone.set_policy": {
                    "description": "Set one sustained local flight policy.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"mode": {"type": "string"}},
                        "required": ["mode"],
                        "additionalProperties": False,
                    },
                }
            },
            "stances": ["spectator", "participant"],
            "situationKinds": ["controller/v1"],
            "controller": {
                "policyActions": ["drone.set_policy"],
                "leaseDurationMs": 30_000,
                "maxActionRateHz": 12,
                "takeover": "immediate",
            },
        }
        registered = runtime.register(
            manifest=manifest,
            conversation_id="controller-fast-lane",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={
                "controller": {
                    "kind": "controller/v1",
                    "status": "idle",
                    "policyRevision": None,
                    "policyAction": None,
                    "policySummary": "",
                }
            },
        )
        request = runtime.invoke_action(
            app_session_id=sid,
            actor="kurisu",
            type="drone.set_policy",
            payload={"mode": "follow"},
            expected_revision=1,
            proposal_id="b2f:r1:follow",
        )
        lease = request["action"]["controller_lease"]
        active_state = {
            "controller": {
                "kind": "controller/v1",
                "status": "active",
                "policyRevision": lease["policy_revision"],
                "policyAction": "drone.set_policy",
                "policySummary": "Follow the player",
            }
        }
        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=token,
            action_id=request["action"]["action_id"],
            accepted=True,
            resulting_revision=2,
            state=active_state,
            effects={"mode": "follow"},
        )
        runtime.record_delivered_narration(
            app_session_id=sid,
            text="追従方針で動くわ。",
            event_id=request["action"]["action_id"],
        )
        first = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="follow-effect-1",
            type="drone.controller_effect",
            actor="app",
            revision=2,
            payload={"command": "follow", "distance": 40},
        )
        # Continuous local execution may advance again before narration. The
        # accepted historical effect remains true and must not become stale.
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=3,
            state=active_state | {"distance": 25},
        )
        observer_inputs: list[dict] = []
        narrator_inputs: list[dict] = []
        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda payload: observer_inputs.append(payload)
            or {"action": "silent"},
            narrator=lambda payload: narrator_inputs.append(payload)
            or {"display_text": "追従を始めて距離を詰めたわ。", "emotion": "confident"},
            sink=lambda _payload: {"status": "queued"},
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL MAIN CHAT PERSONA",
        )

        delivered = await adapter.handle_update("auip.updated", first)

        assert delivered and delivered["status"] == "queued"
        assert observer_inputs == []
        assert "active local Controller policy" in narrator_inputs[0]["fact_brief"]
        assert '"command":"follow"' in narrator_inputs[0]["fact_brief"]
        assert runtime.get(sid)["latest_delivered_narration"]["event_id"] == (
            "follow-effect-1"
        )

        second = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="follow-effect-2",
            type="drone.controller_effect",
            actor="app",
            revision=3,
            payload={"command": "follow", "distance": 25},
        )
        sparse = await adapter.handle_update("auip.updated", second)
        assert sparse and sparse["status"] == "silent"
        assert observer_inputs[-1]["event"]["event_id"] == "follow-effect-2"

    asyncio.run(run())


def test_new_controller_generation_cancels_undelivered_old_policy_narration() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        manifest = {
            "schema": AUIP_SCHEMA,
            "app": {"id": "reactive-race", "title": "Reactive Race"},
            "events": {
                "race.controller_effect": {
                    "beat": True,
                    "importance": "important",
                    "controllerEffect": True,
                }
            },
            "actions": {
                "race.set_policy": {
                    "description": "Set one sustained local policy.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"mode": {"type": "string"}},
                        "required": ["mode"],
                        "additionalProperties": False,
                    },
                }
            },
            "stances": ["spectator", "participant"],
            "situationKinds": ["controller/v1"],
            "controller": {
                "policyActions": ["race.set_policy"],
                "leaseDurationMs": 30_000,
                "maxActionRateHz": 12,
                "takeover": "immediate",
            },
        }
        registered = runtime.register(
            manifest=manifest,
            conversation_id="controller-generation-race",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={
                "controller": {
                    "kind": "controller/v1",
                    "status": "idle",
                    "policyRevision": None,
                    "policyAction": None,
                    "policySummary": "",
                }
            },
        )

        def activate(mode: str, revision: int) -> tuple[dict, dict]:
            request = runtime.invoke_action(
                app_session_id=sid,
                actor="kurisu",
                type="race.set_policy",
                payload={"mode": mode},
                expected_revision=revision,
            )
            lease = request["action"]["controller_lease"]
            state = {
                "controller": {
                    "kind": "controller/v1",
                    "status": "active",
                    "policyRevision": lease["policy_revision"],
                    "policyAction": "race.set_policy",
                    "policySummary": mode,
                }
            }
            accepted = runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=request["action"]["action_id"],
                accepted=True,
                resulting_revision=revision + 1,
                state=state,
                effects={"mode": mode},
            )
            return accepted, state

        first_receipt, _ = activate("attack", 1)
        first_event = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="attack-before-change",
            type="race.controller_effect",
            actor="app",
            revision=2,
            payload={"mode": "attack", "command": "fire"},
        )
        first_narrator_started = asyncio.Event()
        hold_first_narrator = asyncio.Event()
        delivered: list[dict] = []

        async def narrator(payload: dict) -> dict:
            if '"mode":"attack"' in payload["fact_brief"]:
                first_narrator_started.set()
                await hold_first_narrator.wait()
                return {"display_text": "攻撃に切り替えたわ。"}
            return {"display_text": "回避に切り替えたわ。"}

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda _payload: {"action": "silent"},
            narrator=narrator,
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            display_language=lambda: "japanese",
        )
        await adapter.enqueue_update("auip.updated", first_receipt)
        await adapter.enqueue_update("auip.updated", first_event)
        await asyncio.wait_for(first_narrator_started.wait(), timeout=1.0)

        second_receipt, _ = activate("evade", 2)
        await adapter.enqueue_update("auip.updated", second_receipt)
        second_event = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="evade-after-change",
            type="race.controller_effect",
            actor="app",
            revision=3,
            payload={"mode": "evade", "command": "dodge"},
        )
        await adapter.enqueue_update("auip.updated", second_event)
        await asyncio.wait_for(adapter.wait_for_idle(), timeout=1.0)

        assert [item["event_id"] for item in delivered] == ["evade-after-change"]
        assert runtime.get(sid)["latest_delivered_narration"]["event_id"] == (
            "evade-after-change"
        )
        await adapter.close()

    asyncio.run(run())


def test_unverified_kurisu_label_does_not_bypass_sparse_profile() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(
            manifest=_manifest(),
            conversation_id="conversation-unverified-self-label",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"turn": "user"},
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="unverified-kurisu-label",
            type="game.move",
            actor="kurisu",
            revision=1,
            payload={"position": 3},
        )
        observed: list[dict] = []
        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda payload: observed.append(payload) or {"action": "silent"},
            narrator=lambda _payload: {},
            sink=lambda _payload: {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=99),
        )

        result = await adapter.handle_update("auip.updated", update)

        assert result and result["status"] == "profile_filtered"
        assert observed == []

    asyncio.run(run())


def test_commentary_debt_prevents_an_entire_experience_from_staying_silent() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        manifest = _manifest()
        manifest["actions"] = {
            "game.move": {
                "description": "Place one legal move.",
                "risk": "local_execution",
            }
        }
        manifest["stances"] = ["spectator", "participant"]
        registered = runtime.register(
            manifest=manifest,
            conversation_id="conversation-commentary-debt",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"turn": "kurisu"},
        )
        observer_inputs: list[dict] = []
        narrator_inputs: list[dict] = []
        sink_calls: list[dict] = []

        def observer(payload: dict) -> dict:
            observer_inputs.append(payload)
            return {"action": "silent", "fact_brief": ""}

        def narrator(payload: dict) -> dict:
            narrator_inputs.append(payload)
            return {"display_text": "この流れは見逃せないわね。"}

        def sink(payload: dict) -> dict:
            sink_calls.append(payload)
            if len(sink_calls) == 1:
                return {"status": "dropped", "reason": "voice_busy"}
            return {"status": "queued"}

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=observer,
            narrator=narrator,
            sink=sink,
            profile=AuipNarrationProfile(
                normal_beat_stride=99,
                max_silent_self_actions=2,
            ),
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL MAIN CHAT PERSONA",
        )

        async def accepted_move(position: int, revision: int) -> dict:
            requested = runtime.invoke_action(
                app_session_id=sid,
                actor="kurisu",
                type="game.move",
                payload={"position": position},
                expected_revision=revision,
            )
            runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=requested["action"]["action_id"],
                accepted=True,
                resulting_revision=revision + 1,
                state={"turn": "user", "last_move": position},
                effects={"placed": position},
            )
            update = runtime.publish_event(
                app_session_id=sid,
                bridge_token=token,
                event_id=f"self-move-{position}",
                type="game.move",
                actor="kurisu",
                revision=revision + 1,
                payload={"position": position},
            )
            return await adapter.handle_update("auip.updated", update) or {}

        first = await accepted_move(1, 1)
        second = await accepted_move(2, 2)
        third = await accepted_move(3, 3)

        assert first["status"] == "silent"
        assert second["status"] == "dropped" and second["retained"] is False
        assert third["status"] == "queued" and third["retained"] is True
        assert [item["commentary_due"] for item in observer_inputs] == [
            False,
            True,
            True,
        ]
        assert len(narrator_inputs) == 2
        assert "game.move" in narrator_inputs[0]["fact_brief"]
        assert runtime.get(sid)["latest_delivered_narration"]["text"] == (
            "この流れは見逃せないわね。"
        )

    asyncio.run(run())


def test_b2_foreground_suppresses_duplicate_voice_but_automatic_uses_event_lane() -> None:
    async def one_event(
        proposal_prefix: str,
        *,
        event_actor: str = "kurisu",
        causal_app_event: bool = False,
        terminal: bool = False,
        record_foreground_delivery: bool = False,
    ) -> tuple[dict, list[dict]]:
        runtime = AuipRuntime()
        manifest = _manifest()
        manifest["actions"] = {
            "game.move": {
                "description": "Place one legal move.",
                "risk": "local_execution",
            }
        }
        manifest["stances"] = ["spectator", "participant"]
        if terminal:
            manifest["events"]["game.move"]["terminal"] = True
        registered = runtime.register(
            manifest=manifest,
            conversation_id=f"conversation-{proposal_prefix}",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"turn": "kurisu"},
        )
        requested = runtime.invoke_action(
            app_session_id=sid,
            actor="kurisu",
            type="game.move",
            payload={"position": 1},
            expected_revision=1,
            proposal_id=f"{proposal_prefix}:r1:candidate",
        )
        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=token,
            action_id=requested["action"]["action_id"],
            accepted=True,
            resulting_revision=2,
            state={"turn": "user", "last_move": 1},
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id=f"event-{proposal_prefix}",
            type="game.move",
            actor=event_actor,
            revision=2,
            payload={"position": 1},
            caused_by_action_id=(
                requested["action"]["action_id"] if causal_app_event else ""
            ),
        )
        if record_foreground_delivery:
            runtime.record_delivered_narration(
                app_session_id=sid,
                text="前台で結果を伝えたわ。",
                event_id=requested["action"]["action_id"],
            )
        observer_inputs: list[dict] = []

        def observer(payload: dict) -> dict:
            observer_inputs.append(payload)
            return {"action": "silent", "fact_brief": ""}

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=observer,
            narrator=lambda _payload: {"display_text": "unused"},
            sink=lambda _payload: {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=1),
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL MAIN CHAT PERSONA",
        )
        result = await adapter.handle_update("auip.updated", update) or {}
        return result, observer_inputs

    async def run() -> None:
        foreground, foreground_observations = await one_event("b2f")
        causal_foreground, causal_foreground_observations = await one_event(
            "b2f",
            event_actor="app",
            causal_app_event=True,
        )
        automatic, automatic_observations = await one_event(
            "b2a",
            event_actor="app",
            causal_app_event=True,
        )
        delivered_terminal, delivered_terminal_observations = await one_event(
            "b2f",
            event_actor="app",
            causal_app_event=True,
            terminal=True,
            record_foreground_delivery=True,
        )
        undelivered_terminal, undelivered_terminal_observations = await one_event(
            "b2f",
            event_actor="app",
            causal_app_event=True,
            terminal=True,
        )

        assert foreground["status"] == "b2_foreground_owned"
        assert foreground_observations == []
        assert causal_foreground["status"] == "b2_foreground_owned"
        assert causal_foreground_observations == []
        assert automatic["status"] == "silent"
        assert len(automatic_observations) == 1
        assert automatic_observations[0]["latest_verified_self_action"][
            "proposal_id"
        ].startswith("b2a:")
        assert delivered_terminal["status"] == "b2_foreground_terminal_owned"
        assert delivered_terminal_observations == []
        assert undelivered_terminal["status"] == "queued"
        assert undelivered_terminal_observations == []

    asyncio.run(run())


def test_dropped_auip_line_never_enters_branch_capsule() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(manifest=_manifest(), conversation_id="conversation-2")
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"winner": "app"},
        )
        terminal = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="finish-drop",
            type="game.finished",
            actor="app",
            revision=1,
            payload={"winner": "app"},
        )
        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda _payload: {"action": "speak", "fact_brief": "the game ended"},
            narrator=lambda _payload: {"display_text": "これは届かない。"},
            sink=lambda _payload: {"status": "dropped", "reason": "queue_full"},
            profile=AuipNarrationProfile(normal_beat_stride=99),
        )

        result = await adapter.handle_update("auip.updated", terminal)

        assert result and result["status"] == "dropped" and result["retained"] is False
        assert "これは届かない。" not in runtime.render_main_chat_context("conversation-2")

    asyncio.run(run())


def test_verified_terminal_outcome_cannot_be_silenced_by_observer_variance() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(manifest=_manifest(), conversation_id="terminal-chat")
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"status": "won", "score": 4},
        )
        terminal = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="finish-mandatory",
            type="game.finished",
            actor="app",
            revision=1,
            payload={"winner": "kurisu"},
        )
        narrator_inputs: list[dict] = []
        delivered: list[dict] = []
        observer_calls: list[dict] = []
        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda payload: observer_calls.append(payload)
            or {"action": "silent", "fact_brief": ""},
            narrator=lambda payload: narrator_inputs.append(payload)
            or {"display_text": "勝負はついたわ。"},
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=99),
        )

        result = await adapter.handle_update("auip.updated", terminal)

        assert result and result["status"] == "queued"
        assert observer_calls == []
        assert delivered and delivered[0]["terminal"] is True
        assert narrator_inputs[0]["request_id"] == delivered[0]["line_id"]
        assert narrator_inputs[0]["terminal"] is True
        assert narrator_inputs[0]["delivery_source"] == "auip_narrator"
        assert "game.finished" in narrator_inputs[0]["fact_brief"]
        assert '"score":4' in narrator_inputs[0]["fact_brief"]
        assert runtime.get(sid)["latest_delivered_narration"]["terminal"] is True

    asyncio.run(run())


def test_terminal_narration_has_a_truthful_local_fallback() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(manifest=_manifest(), conversation_id="fallback-chat")
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"status": "won"},
        )
        terminal = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="finish-fallback",
            type="game.finished",
            actor="app",
            revision=1,
            payload={"winner": "app"},
        )
        delivered: list[dict] = []
        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda _payload: (_ for _ in ()).throw(
                AssertionError("terminal admission must not call the Observer")
            ),
            narrator=lambda _payload: None,
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=99),
            display_language=lambda: "japanese",
        )

        result = await adapter.handle_update("auip.updated", terminal)

        assert result and result["status"] == "queued"
        assert delivered[0]["terminal"] is True
        assert "終了したわ" in delivered[0]["display_text"]
        assert "winner" not in delivered[0]["display_text"]

    asyncio.run(run())


def test_enqueued_narration_never_blocks_app_ack_and_keeps_source_order() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(manifest=_manifest(), conversation_id="conversation-3")
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        observed: list[str] = []
        release_first = asyncio.Event()
        first_started = asyncio.Event()

        async def observer(payload: dict) -> dict:
            event_id = str(payload["event"]["event_id"])
            observed.append(event_id)
            if event_id == "move-slow":
                first_started.set()
                await release_first.wait()
            return {"action": "silent", "fact_brief": event_id}

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=observer,
            narrator=lambda _payload: {},
            sink=lambda _payload: {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=1),
        )
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"moves": 1},
        )
        first = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="move-slow",
            type="game.move",
            actor="user",
            revision=1,
            payload={"move": 1},
        )
        await asyncio.wait_for(adapter.enqueue_update("auip.updated", first), timeout=0.05)
        await asyncio.wait_for(first_started.wait(), timeout=1.0)

        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=2,
            state={"moves": 2},
        )
        second = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="move-after",
            type="game.move",
            actor="user",
            revision=2,
            payload={"move": 2},
        )
        await asyncio.wait_for(adapter.enqueue_update("auip.updated", second), timeout=0.05)
        await asyncio.sleep(0)
        assert observed == ["move-slow"]

        release_first.set()
        await asyncio.wait_for(
            asyncio.gather(*tuple(adapter._background_tasks)),
            timeout=1.0,
        )
        assert observed == ["move-slow", "move-after"]
        await adapter.close()

    asyncio.run(run())


def test_one_action_event_burst_presents_only_its_most_important_consequence() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        manifest = _manifest()
        manifest["events"]["game.result"] = {
            "beat": True,
            "importance": "important",
        }
        registered = runtime.register(
            manifest=manifest,
            conversation_id="conversation-action-burst",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"winner": "black"},
        )
        move = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="burst-move",
            type="game.move",
            actor="kurisu",
            revision=1,
            payload={"position": 4},
            caused_by_action_id="action-burst",
        )
        result = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="burst-result",
            type="game.result",
            actor="app",
            revision=1,
            payload={"winner": "black"},
            caused_by_action_id="action-burst",
        )
        observed: list[str] = []
        delivered: list[str] = []

        def observer(payload: dict) -> dict:
            event_type = str(payload["event"]["type"])
            observed.append(event_type)
            return {"action": "speak", "fact_brief": event_type}

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=observer,
            narrator=lambda payload: {
                "display_text": f"{payload['fact_brief']} なのね。"
            },
            sink=lambda payload: delivered.append(payload["display_text"])
            or {"status": "queued"},
            profile=AuipNarrationProfile(
                normal_beat_stride=1,
                action_event_coalesce_s=0.01,
            ),
        )

        await adapter.enqueue_update("auip.updated", move)
        await adapter.enqueue_update("auip.updated", result)
        await asyncio.wait_for(adapter.wait_for_idle(), timeout=1.0)

        assert observed == ["game.result"]
        assert delivered == ["game.result なのね。"]
        assert runtime.get(sid)["latest_delivered_narration"]["event_id"] == (
            "burst-result"
        )
        await adapter.close()

    asyncio.run(run())


def test_terminal_fact_supersedes_undelivered_old_commentary() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(manifest=_manifest(), conversation_id="conversation-4")
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        slow_started = asyncio.Event()
        delivered: list[str] = []

        async def observer(payload: dict) -> dict:
            if payload["event"]["type"] == "game.move":
                slow_started.set()
                await asyncio.Event().wait()
            return {"action": "speak", "fact_brief": payload["event"]["type"]}

        async def narrator(payload: dict) -> dict:
            assert "game.finished" in payload["fact_brief"]
            return {"display_text": "game.finished なのね。"}

        async def sink(payload: dict) -> dict:
            delivered.append(payload["display_text"])
            return {"status": "queued"}

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=observer,
            narrator=narrator,
            sink=sink,
            profile=AuipNarrationProfile(normal_beat_stride=1),
        )
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"moves": 1},
        )
        move = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="move-before-terminal",
            type="game.move",
            actor="user",
            revision=1,
            payload={"move": 1},
        )
        await adapter.enqueue_update("auip.updated", move)
        await asyncio.wait_for(slow_started.wait(), timeout=1.0)

        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=2,
            state={"winner": "app"},
        )
        terminal = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="terminal-priority",
            type="game.finished",
            actor="app",
            revision=2,
            payload={"winner": "app"},
        )
        await adapter.enqueue_update("auip.updated", terminal)
        await asyncio.wait_for(
            asyncio.gather(*tuple(adapter._background_tasks), return_exceptions=True),
            timeout=1.0,
        )

        assert delivered == ["game.finished なのね。"]
        capsule = runtime.get(sid)["experience_capsule"]
        assert capsule["delivered_narration"] == ["game.finished なのね。"]
        await adapter.close()

    asyncio.run(run())


def test_operator_blocked_reason_is_narrated_without_claiming_an_app_action() -> None:
    async def run() -> None:
        runtime = AuipRuntime()
        registered = runtime.register(
            manifest={
                "schema": AUIP_SCHEMA,
                "app": {
                    "id": "role-binding-game",
                    "title": "Role Binding Game",
                    "version": "0.1.0",
                },
                "events": {"game.changed": {"beat": True}},
                "actions": {
                    "game.move": {
                        "description": "Move only for the currently bound role.",
                        "risk": "local_execution",
                    }
                },
                "stances": ["spectator", "participant"],
            },
            conversation_id="conversation-blocked-narration",
        )
        sid = registered["app_session_id"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        blocked = runtime.set_operator_status(
            app_session_id=sid,
            status="error",
            error="participant_blocked",
            error_detail="The participant controls White, but it is Black's turn.",
        )
        narrator_inputs: list[dict] = []
        delivered: list[dict] = []

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=lambda _payload: (_ for _ in ()).throw(
                AssertionError("a Host operator outcome must not call the app Observer")
            ),
            narrator=lambda payload: narrator_inputs.append(payload)
            or {
                "display_text": "今は白番しか担当できないから、先手では打てないわ。",
                "emotion": "thinking",
            },
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL MAIN CHAT PERSONA",
        )
        payload = {
            **blocked,
            "operator_outcome": {
                "status": "blocked",
                "proposal_id": "proposal-blocked-1",
                "instruction": "Take the first turn.",
                "reason": "The participant controls White, but it is Black's turn.",
            },
        }

        result = await adapter.handle_update("auip.updated", payload)

        assert result and result["status"] == "queued"
        assert "was not confirmed as performed" in narrator_inputs[0]["fact_brief"]
        assert "not a failure by the user" in narrator_inputs[0]["fact_brief"]
        assert "never blame, scold, hurry" in narrator_inputs[0]["system_prompt"]
        assert "Black's turn" in narrator_inputs[0]["fact_brief"]
        assert "No accepted execution receipt" in narrator_inputs[0]["fact_brief"]
        assert delivered[0]["source"] == "auip_operator_outcome"
        assert delivered[0]["terminal"] is False
        assert runtime.get(sid)["latest_delivered_narration"]["text"] == (
            "今は白番しか担当できないから、先手では打てないわ。"
        )

    asyncio.run(run())


def test_wait_for_idle_retires_completed_tasks_without_callback_progress() -> None:
    async def run() -> None:
        adapter = AuipNarrationAdapter(
            runtime=AuipRuntime(),
            observer=lambda _payload: None,
            narrator=lambda _payload: None,
            sink=lambda _payload: None,
        )
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        adapter._background_tasks.add(task)
        adapter._background_task_sessions[task] = "app-completed"

        await asyncio.wait_for(adapter.wait_for_idle(), timeout=0.5)

        assert not adapter._background_tasks
        assert task not in adapter._background_task_sessions

    asyncio.run(run())


if __name__ == "__main__":
    test_auip_profile_observer_narrator_and_receipt_keep_their_owners()
    test_profile_rejects_long_scene_prose_instead_of_cutting_a_sentence()
    test_verified_self_action_reaches_observer_without_forcing_commentary()
    test_unverified_kurisu_label_does_not_bypass_sparse_profile()
    test_commentary_debt_prevents_an_entire_experience_from_staying_silent()
    test_b2_foreground_suppresses_duplicate_voice_but_automatic_uses_event_lane()
    test_dropped_auip_line_never_enters_branch_capsule()
    test_verified_terminal_outcome_cannot_be_silenced_by_observer_variance()
    test_terminal_narration_has_a_truthful_local_fallback()
    test_enqueued_narration_never_blocks_app_ack_and_keeps_source_order()
    test_one_action_event_burst_presents_only_its_most_important_consequence()
    test_terminal_fact_supersedes_undelivered_old_commentary()
    test_operator_blocked_reason_is_narrated_without_claiming_an_app_action()
    test_wait_for_idle_retires_completed_tasks_without_callback_progress()
    print("ok: AUIP observation, role prose, delivery, and retention remain separate")
