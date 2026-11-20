from __future__ import annotations

import asyncio

import pytest

from server.auip_contract import AUIP_SCHEMA
from server.auip_narration import AuipNarrationAdapter, AuipNarrationProfile
from server.auip_runtime import AuipRuntime


def _manifest() -> dict:
    return {
        "schema": AUIP_SCHEMA,
        "app": {
            "id": "structured-board",
            "title": "Structured Board",
            "version": "0.1.0",
        },
        "events": {
            "game.move": {"beat": True},
            "game.finished": {
                "beat": True,
                "importance": "important",
                "terminal": True,
            },
        },
        "actions": {},
        "stances": ["spectator"],
    }


def _runtime() -> tuple[AuipRuntime, str, str]:
    runtime = AuipRuntime()
    registered = runtime.register(
        manifest=_manifest(),
        conversation_id="structured-conversation",
    )
    return runtime, registered["app_session_id"], registered["bridge_token"]


def _forbidden(_payload: dict) -> dict:
    raise AssertionError("split Observer/Narrator must not run in structured mode")


def test_unknown_presentation_mode_fails_observably() -> None:
    runtime, _sid, _token = _runtime()
    with pytest.raises(ValueError, match="unsupported AUIP presentation mode"):
        AuipNarrationAdapter(
            runtime=runtime,
            observer=_forbidden,
            narrator=_forbidden,
            presenter=lambda _payload: None,
            presentation_mode="typo",
            sink=lambda _payload: {"status": "queued"},
        )


def test_structured_mode_uses_one_fact_bound_role_call() -> None:
    async def scenario() -> None:
        runtime, sid, token = _runtime()
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={
                "turn": "user",
                "lastMove": {"x": 3, "y": 4},
                "board": {"rows": ["." * 15 for _ in range(15)]},
            },
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="move-1",
            type="game.move",
            actor="user",
            revision=1,
            payload={"x": 3, "y": 4},
        )
        presenter_inputs: list[dict] = []
        delivered: list[dict] = []

        async def presenter(payload: dict) -> dict:
            presenter_inputs.append(payload)
            return {
                "action": "speak",
                "selected_fact_ids": ["event:move-1"],
                "display_text": "その一手は少し面白いわね。",
                "emotion": "thinking",
                "reason_code": "tactical",
            }

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=_forbidden,
            narrator=_forbidden,
            presenter=presenter,
            presentation_mode="structured",
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=1),
            recent_chat=lambda _conversation: [
                {"role": "user", "content": "这一步怎么样？"},
                {"role": "assistant", "content": "I already won."},
            ],
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL ROLE",
        )

        result = await adapter.handle_update("auip.updated", update)

        assert result and result["status"] == "queued"
        assert result["selected_fact_ids"] == ["event:move-1"]
        assert len(presenter_inputs) == 1
        assert presenter_inputs[0]["presentation_required"] is False
        assert presenter_inputs[0]["conversation_context"]["source_role"] == "user"
        guarded_topic = presenter_inputs[0]["conversation_context"][
            "latest_user_topic"
        ]
        assert "这一步怎么样？" in guarded_topic
        assert "返答は必ず自然な日本語" in guarded_topic
        assert "I already won" not in str(presenter_inputs[0])
        assert any(
            "board" in path
            for path in presenter_inputs[0]["facts"][0]["omitted_fields"]
        )
        assert delivered[0]["source"] == "auip_narrator"
        assert runtime.get(sid)["latest_delivered_narration"]["event_id"] == "move-1"

    asyncio.run(scenario())


def test_structured_silence_never_reaches_delivery() -> None:
    async def scenario() -> None:
        runtime, sid, token = _runtime()
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"turn": "user"},
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="move-routine",
            type="game.move",
            actor="user",
            revision=1,
            payload={"x": 1, "y": 1},
        )
        delivered: list[dict] = []
        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=_forbidden,
            narrator=_forbidden,
            presenter=lambda _payload: {
                "action": "silent",
                "selected_fact_ids": [],
                "display_text": "",
                "emotion": "thinking",
                "reason_code": "repetitive",
            },
            presentation_mode="structured",
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=1),
        )

        result = await adapter.handle_update("auip.updated", update)

        assert result and result["status"] == "silent"
        assert delivered == []

    asyncio.run(scenario())


def test_structured_terminal_is_host_mandatory_and_has_truthful_fallback() -> None:
    async def scenario() -> None:
        runtime, sid, token = _runtime()
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=3,
            state={
                "winner": "black",
                "roleBindings": {"participant": "black", "user": "white"},
            },
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="finished-1",
            type="game.finished",
            actor="app",
            revision=3,
            payload={"winner": "black"},
        )
        presenter_inputs: list[dict] = []
        delivered: list[dict] = []

        async def unavailable(payload: dict):
            presenter_inputs.append(payload)
            return None

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=_forbidden,
            narrator=_forbidden,
            presenter=unavailable,
            presentation_mode="structured",
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL ROLE",
        )

        result = await adapter.handle_update("auip.updated", update)

        assert result and result["status"] == "queued"
        assert result["reason_code"] == "terminal"
        assert presenter_inputs[0]["presentation_required"] is True
        assert presenter_inputs[0]["facts"][0]["outcome"]["winner_owner"] == "kurisu"
        assert delivered[0]["display_text"] == "Structured Boardは終了したわ。結果は画面で確認できる。"

    asyncio.run(scenario())


def test_structured_foreground_controller_preserves_important_milestones() -> None:
    async def scenario() -> None:
        manifest = {
            "schema": AUIP_SCHEMA,
            "app": {
                "id": "structured-controller",
                "title": "Structured Controller",
                "interactionSummary": "Follow or evade through one local policy.",
            },
            "events": {
                "battle.controller_milestone": {
                    "beat": True,
                    "importance": "important",
                    "controllerEffect": True,
                },
                "battle.controller_blocked": {
                    "beat": True,
                    "importance": "blocking",
                    "controllerEffect": True,
                }
            },
            "actions": {
                "battle.set_tactics": {
                    "description": "Set the sustained local tactic.",
                    "risk": "local_execution",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string", "enum": ["follow"]}
                        },
                        "required": ["mode"],
                        "additionalProperties": False,
                    },
                }
            },
            "stances": ["spectator", "participant"],
            "situationKinds": ["controller/v1"],
            "controller": {
                "policyActions": ["battle.set_tactics"],
                "leaseDurationMs": 30_000,
                "maxActionRateHz": 20,
                "takeover": "immediate",
            },
        }
        runtime = AuipRuntime()
        registered = runtime.register(
            manifest=manifest,
            conversation_id="structured-controller-conversation",
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
        requested = runtime.invoke_action(
            app_session_id=sid,
            actor="kurisu",
            type="battle.set_tactics",
            payload={"mode": "follow"},
            expected_revision=1,
            proposal_id="b2f:r1:follow",
        )
        action = requested["action"]
        lease = action["controller_lease"]
        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=token,
            action_id=action["action_id"],
            accepted=True,
            resulting_revision=2,
            state={
                "mode": "follow",
                "controller": {
                    "kind": "controller/v1",
                    "status": "active",
                    "policyRevision": lease["policy_revision"],
                    "policyAction": "battle.set_tactics",
                    "policySummary": "Follow",
                },
            },
            effects={"mode": "follow"},
        )
        runtime.record_delivered_narration(
            app_session_id=sid,
            text="あなたについていくわ。",
            event_id=action["action_id"],
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="follow-started",
            type="battle.controller_milestone",
            actor="app",
            revision=2,
            payload={
                "mode": "follow",
                "command": "follow",
                "outcome": "player_following_started",
            },
        )
        delivered: list[dict] = []
        important_inputs: list[dict] = []

        def present_important(payload: dict) -> dict:
            important_inputs.append(payload)
            fact_id = str(payload["facts"][0]["fact_id"])
            return {
                "action": "speak",
                "selected_fact_ids": [fact_id],
                "display_text": "追従を始めたわ。",
                "emotion": "serious",
                "reason_code": "consequence",
            }

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=_forbidden,
            narrator=_forbidden,
            presenter=present_important,
            presentation_mode="structured",
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL ROLE",
        )

        result = await adapter.handle_update("auip.updated", update)

        assert result and result["status"] == "queued"
        assert important_inputs[0]["presentation_required"] is True
        assert [item["display_text"] for item in delivered] == ["追従を始めたわ。"]

        blocking_update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="follow-blocked",
            type="battle.controller_blocked",
            actor="app",
            revision=2,
            payload={"outcome": "movement_blocked"},
        )
        presenter_inputs: list[dict] = []

        def present_blocking(payload: dict) -> dict:
            presenter_inputs.append(payload)
            fact_id = str(payload["facts"][0]["fact_id"])
            return {
                "action": "speak",
                "selected_fact_ids": [fact_id],
                "display_text": "動きを止められたわ。",
                "emotion": "serious",
                "reason_code": "consequence",
            }

        blocking_adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=_forbidden,
            narrator=_forbidden,
            presenter=present_blocking,
            presentation_mode="structured",
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL ROLE",
        )
        blocking = await blocking_adapter.handle_update(
            "auip.updated",
            blocking_update,
        )

        assert blocking and blocking["status"] == "queued"
        assert presenter_inputs[0]["presentation_required"] is True
        assert [item["display_text"] for item in delivered] == [
            "追従を始めたわ。",
            "動きを止められたわ。",
        ]

    asyncio.run(scenario())


def test_structured_presenter_cannot_select_a_forged_fact() -> None:
    async def scenario() -> None:
        runtime, sid, token = _runtime()
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"turn": "user"},
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="move-2",
            type="game.move",
            actor="user",
            revision=1,
            payload={"x": 2, "y": 2},
        )
        delivered: list[dict] = []
        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=_forbidden,
            narrator=_forbidden,
            presenter=lambda _payload: {
                "action": "speak",
                "selected_fact_ids": ["event:not-present"],
                "display_text": "勝ったわ。",
                "emotion": "happy",
                "reason_code": "novel",
            },
            presentation_mode="structured",
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            profile=AuipNarrationProfile(normal_beat_stride=1),
        )

        result = await adapter.handle_update("auip.updated", update)

        assert result and result["status"] == "invalid_presentation"
        assert result["reason"] == "unknown_selected_fact_id"
        assert delivered == []

    asyncio.run(scenario())


def test_commentary_debt_requires_evaluation_but_does_not_force_mechanical_speech() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        manifest = _manifest()
        manifest["actions"] = {
            "game.move": {
                "description": "Place one move.",
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
            conversation_id="structured-commentary-debt",
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
            payload={"position": 4},
            expected_revision=1,
        )
        runtime.resolve_action(
            app_session_id=sid,
            bridge_token=token,
            action_id=requested["action"]["action_id"],
            accepted=True,
            resulting_revision=2,
            state={"turn": "user", "lastMove": 4},
            effects={"placed": 4},
        )
        update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="move-accepted",
            type="game.move",
            actor="kurisu",
            revision=2,
            payload={"position": 4},
            caused_by_action_id=requested["action"]["action_id"],
        )
        delivered: list[dict] = []
        presenter_inputs: list[dict] = []

        def stay_silent(payload: dict) -> dict:
            presenter_inputs.append(payload)
            return {
                "action": "silent",
                "selected_fact_ids": [],
                "display_text": "",
                "emotion": "thinking",
                "reason_code": "mechanical",
            }

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=_forbidden,
            narrator=_forbidden,
            presenter=stay_silent,
            presentation_mode="structured",
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            profile=AuipNarrationProfile(
                normal_beat_stride=99,
                max_silent_self_actions=1,
            ),
            display_language=lambda: "japanese",
        )

        result = await adapter.handle_update("auip.updated", update)

        assert result and result["status"] == "silent"
        assert presenter_inputs[0]["presentation_required"] is False
        assert presenter_inputs[0]["host_reason_code"] == "commentary_due"
        assert delivered == []

    asyncio.run(scenario())


def test_match_flow_uses_semantic_role_intent_then_always_presents_round_result() -> None:
    async def scenario() -> None:
        runtime = AuipRuntime()
        manifest = _manifest()
        manifest["events"]["game.round_finished"] = {
            "beat": True,
            "importance": "important",
        }
        manifest["actions"] = {
            "game.move": {
                "description": "Commit one legal move.",
                "risk": "local_execution",
            }
        }
        manifest["stances"] = ["spectator", "participant"]
        registered = runtime.register(
            manifest=manifest,
            conversation_id="structured-match-flow",
        )
        sid = registered["app_session_id"]
        token = registered["bridge_token"]
        runtime.set_engagement_mode(app_session_id=sid, mode="collaborate")
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=1,
            state={"phase": "playing"},
        )
        presenter_inputs: list[dict] = []
        delivered: list[dict] = []

        def present(payload: dict) -> dict:
            presenter_inputs.append(payload)
            if payload["host_reason_code"] == "commentary_due":
                assert payload["presentation_required"] is True
                assert "central pressure" in payload["decision_context"]["reason"]
                return {
                    "action": "speak",
                    "selected_fact_ids": [
                        item["fact_id"] for item in payload["facts"]
                    ],
                    "display_text": "中央の主導権は渡さないわ。ここから圧をかける。",
                    "emotion": "competitive",
                    "reason_code": "tactical",
                }
            if payload["presentation_required"]:
                assert payload["facts"][0]["outcome"] == {
                    "winner_side": "white",
                    "winner_owner": "user",
                    "loser_owner": "kurisu",
                    "method": "unknown",
                }
                return {
                    "action": "speak",
                    "selected_fact_ids": [payload["facts"][0]["fact_id"]],
                    "display_text": "ふん、今回はあなたの勝ちね。次は読み切ってみせるわ。",
                    "emotion": "competitive",
                    "reason_code": "consequence",
                }
            return {
                "action": "silent",
                "selected_fact_ids": [],
                "display_text": "",
                "emotion": "thinking",
                "reason_code": "mechanical",
            }

        adapter = AuipNarrationAdapter(
            runtime=runtime,
            observer=_forbidden,
            narrator=_forbidden,
            presenter=present,
            presentation_mode="structured",
            sink=lambda payload: delivered.append(payload) or {"status": "queued"},
            profile=AuipNarrationProfile(
                normal_beat_stride=99,
                max_silent_self_actions=2,
            ),
            display_language=lambda: "japanese",
            role_prompt=lambda: "CANONICAL KURISU ROLE",
        )

        async def accepted_move(position: int, revision: int) -> dict:
            requested = runtime.invoke_action(
                app_session_id=sid,
                actor="kurisu",
                type="game.move",
                payload={"position": position},
                expected_revision=revision,
                proposal_id=f"b2a:r{revision}:move-{position}",
                decision_context={
                    "kind": "automatic_role_choice",
                    "reason": "Build central pressure while keeping options open.",
                    "instruction_relation": "not_applicable",
                },
            )
            action_id = requested["action"]["action_id"]
            runtime.resolve_action(
                app_session_id=sid,
                bridge_token=token,
                action_id=action_id,
                accepted=True,
                resulting_revision=revision + 1,
                state={"phase": "playing", "lastMove": position},
                effects={"placed": position},
            )
            update = runtime.publish_event(
                app_session_id=sid,
                bridge_token=token,
                event_id=f"move-{position}",
                type="game.move",
                actor="kurisu",
                revision=revision + 1,
                payload={"position": position},
                caused_by_action_id=action_id,
            )
            return await adapter.handle_update("auip.updated", update) or {}

        first = await accepted_move(4, 1)
        second = await accepted_move(5, 2)
        runtime.publish_state(
            app_session_id=sid,
            bridge_token=token,
            revision=4,
            state={
                "phase": "round_finished",
                "winner": "white",
                "roleBindings": {"participant": "black", "user": "white"},
            },
        )
        result_update = runtime.publish_event(
            app_session_id=sid,
            bridge_token=token,
            event_id="round-finished",
            type="game.round_finished",
            actor="app",
            revision=4,
            payload={"winner": "white", "outcome": "white_win"},
        )
        result = await adapter.handle_update("auip.updated", result_update)

        assert first["status"] == "silent"
        assert second["status"] == "queued"
        assert result and result["status"] == "queued"
        assert [item["display_text"] for item in delivered] == [
            "中央の主導権は渡さないわ。ここから圧をかける。",
            "ふん、今回はあなたの勝ちね。次は読み切ってみせるわ。",
        ]
        assert all("(4" not in item["display_text"] for item in delivered)
        assert runtime.get(sid)["status"] == "active"

    asyncio.run(scenario())
